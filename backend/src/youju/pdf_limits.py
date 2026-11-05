"""PDF 解压上限（防压缩炸弹）。

pdfminer（pdfplumber 取文字用的库）解 PDF 里的数据流时不限长度：FlateDecode 是一次 zlib.decompress
整段解开（解的时候先分块、最后拼成一整段，内存要占解出大小的两倍），11/5 独立审阅实测一个 292KB 的
PDF（一个数据流解压后 300MB）让进程内存从 71MB 涨到 656MB；数据流还能连着压几遍（解出来再解），
LZW、RunLength、CCITT 解码器和预测器也照文件里写的长度、列数分配内存。附件副本的检查
（attachments）、附件文字（attachment_text）、名单解析（lists.parser）、PDF 正文（crawler.pdf）
都是 pdfplumber → pdfminer，解数据流都经过同一个函数 pdfminer.pdftypes.PDFStream.decode。这里把它
换成有上限的写法（_decode，导入本模块时换上），读 PDF 的地方套上 reading()：

- 一个数据流每解一步，解出的数据不超过 STREAM_MAX；
- 一份 PDF（一次 reading() 里）解出的数据合计不超过 DOCUMENT_MAX：pdfminer 把解过的数据流留在
  内存里，整份读完才释放；
- 解码方式只认 FlateDecode、ASCIIHexDecode、ASCII85Decode，图片的几种原样留着（取文字时不解图片，
  和 pdfminer 一样）。LZW、RunLength、CCITTFax 不解，整份按"格式不支持"：pdfminer 的这几个解码器
  内存或时间没有上限，现在的文件只在图片上用（见下面的抽查）；
- 预测器（交叉引用流常用 PNG 预测器）：pdfminer 按文件里写的列数先分配一行，结果存成整数列表
  （每个字节约占 9 字节内存），输入长度和列数都有上限。
不超上限的，解出的数据和 pdfminer 原来的写法一样（坏数据的处理也照抄）。超了、不支持的抛 Refused；
pdfplumber 会把它包进 PdfminerException，用 refused(exc) 找出来。

上限怎么定（2026-11-05 按 11/4 公开数据快照里附件、名单的 PDF 链接下载了 329 份真实文件，把每份的
非图片数据流全部解一遍）：一个数据流解出最大 2.4MB（字体最大 1.5MB）；一份合计最大 35.6MB，中位
1.0MB；文字多的文件解出的大约是文件大小的 10 倍，20MB 的文件整份读完约 220MB。解码方式只有
FlateDecode（14,865 个）和 ASCIIHexDecode（5 个），预测器只有交叉引用流的 PNG 预测器。
15,349 个数据流用这里的写法和 pdfminer 原来的写法解出的逐字节一样。

为什么不像解 rar（attachment_text._rar，PR #451）那样放进限内存的子进程：
1. 本机 macOS 设不了 RLIMIT_AS（setrlimit 报 ValueError，分配照样成功），测试证明不了上限，只有线上
   Linux 管用；
2. 四处调用拿回的东西（分段和出处、表格行、页数、图片面积）都要改成能跨进程传的格式，冒充 pdfplumber
   的测试也要跟着改；
3. 每份 PDF 多起一个 Python 进程、重新导入 pdfplumber（约 0.5 秒）。
限长解压在哪里都一样、测试能直接量内存，四处调用的逻辑不用改。它管不到的：解出来的内容在上限以内，
但让 pdfminer 生成大量对象（比如几十 MB 层层嵌套的方括号）；这种不压缩、20MB 的文件也做得到，
要把整个取文字放进限内存的子进程，另做。
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from pdfminer import settings
from pdfminer.ascii85 import ascii85decode, asciihexdecode
from pdfminer.pdftypes import (
    LITERAL_CRYPT,
    LITERALS_ASCII85_DECODE,
    LITERALS_ASCIIHEX_DECODE,
    LITERALS_CCITTFAX_DECODE,
    LITERALS_DCT_DECODE,
    LITERALS_FLATE_DECODE,
    LITERALS_JBIG2_DECODE,
    LITERALS_JPX_DECODE,
    LITERALS_LZW_DECODE,
    LITERALS_RUNLENGTH_DECODE,
    PDFException,
    PDFNotImplementedError,
    PDFStream,
    int_value,
)
from pdfminer.utils import apply_png_predictor, apply_tiff_predictor

log = logging.getLogger("youju.pdf_limits")
_MB = 1024 * 1024
# 一个数据流解出的数据（每一步；解的时候内存要占两倍）。真实文件最大 2.4MB
STREAM_MAX = 32 * _MB
# 一份 PDF 解出的数据合计。真实文件最大 35.6MB，20MB 的文字文件整份读完约 220MB
DOCUMENT_MAX = 256 * _MB
# 预测器的输入长度、列数（pdfminer 的写法：每个字节约占 9 字节内存，一行按列数先分配）
PREDICTOR_INPUT_MAX = 8 * _MB
PREDICTOR_COLUMNS_MAX = 1 << 20
# ASCII85 的输入长度（Python 的 a85decode 每 4 个字节先存成一个小 bytes 对象，约占 45 字节）
ASCII85_INPUT_MAX = 8 * _MB

_IMAGE_FILTERS = (*LITERALS_DCT_DECODE, *LITERALS_JBIG2_DECODE, *LITERALS_JPX_DECODE)
_REFUSED_FILTERS = (*LITERALS_LZW_DECODE, *LITERALS_RUNLENGTH_DECODE, *LITERALS_CCITTFAX_DECODE)


class Refused(Exception):
    """这份 PDF 不读：reason 用 attachment_text.REASONS 里的代码（too_large：解压后太大；
    format：压缩方式不支持），detail 写具体原因（不带"PDF"两个字，调用方自己接）。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class _Budget:
    left: int


_BUDGET: ContextVar[_Budget | None] = ContextVar("pdf_decode_budget", default=None)
# pdfminer 原来的 PDFStream.decode（测试拿它对照"不超上限的结果一样"）
_ORIGINAL: Callable[[PDFStream], None] | None = None


def install() -> None:
    """把 pdfminer 解数据流的函数换成有上限的写法（重复调用没关系）。"""
    global _ORIGINAL
    if PDFStream.decode is not _decode:
        _ORIGINAL = _ORIGINAL or PDFStream.decode
        PDFStream.decode = _decode


@contextmanager
def reading(on_refused: Callable[[Refused], Exception] | None = None) -> Iterator[None]:
    """读一份 PDF：这里面解出的数据合计不超过 DOCUMENT_MAX。on_refused：把 Refused（常被 pdfplumber
    包在 PdfminerException 里）换成调用方自己的异常。"""
    install()
    token = _BUDGET.set(_Budget(DOCUMENT_MAX))
    try:
        yield
    except Exception as exc:
        hit = refused(exc)
        if hit is None:
            raise
        # 记一条：附件副本的检查只记"取不出文字"，看日志才分得清是不是解压超了
        log.info("PDF 不读：%s", hit.detail)
        if on_refused is None:
            raise
        raise on_refused(hit) from exc
    finally:
        _BUDGET.reset(token)


def refused(exc: BaseException) -> Refused | None:
    """exc 本身或它包着的异常（__cause__、__context__，以及 pdfplumber 的 PdfminerException(原异常)
    放在 args 里的）是 Refused 就返回它。"""
    todo, seen = [exc], set()
    while todo:
        item = todo.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, Refused):
            return item
        todo += [
            x
            for x in (item.__cause__, item.__context__, *item.args)
            if isinstance(x, BaseException)
        ]
    return None


def _too_large(cap: int) -> Refused:
    if cap < STREAM_MAX:  # 剩下的合计额度比单个数据流的上限还小：是整份合计超了
        return Refused("too_large", f"解压后合计超过 {DOCUMENT_MAX // _MB}MB")
    return Refused("too_large", f"数据流解压后超过 {STREAM_MAX // _MB}MB")


def _check(size: int, cap: int) -> None:
    if size > cap:
        raise _too_large(cap)


def _decode(self: PDFStream) -> None:
    """pdfminer 20260107 版 PDFStream.decode 的写法，加上上限（见模块说明）。"""
    assert self.data is None and self.rawdata is not None, str((self.data, self.rawdata))
    data = self.rawdata
    if self.decipher:
        # Handle encryption
        assert self.objid is not None
        assert self.genno is not None
        data = self.decipher(self.objid, self.genno, data, self.attrs)
    # 先取解码方式：它可能指向对象流里的对象，要先解那个对象流（也计入合计），再算这个的额度
    filters = self.get_filters()
    budget = _BUDGET.get()
    cap = STREAM_MAX if budget is None else max(0, min(STREAM_MAX, budget.left))
    for f, params in filters:
        if f in LITERALS_FLATE_DECODE:
            data = _inflate(data, cap)
        elif f in LITERALS_ASCII85_DECODE:
            _check_ascii85(data, cap)
            data = ascii85decode(data)
        elif f in LITERALS_ASCIIHEX_DECODE:
            data = asciihexdecode(data)  # 解出的不超过输入的一半
        elif f in _IMAGE_FILTERS:
            pass  # 图片：pdfminer 也是原样留着
        elif f in _REFUSED_FILTERS:
            raise Refused("format", f"用了不支持的压缩方式 {f.name}")
        elif f == LITERAL_CRYPT:
            raise PDFNotImplementedError("/Crypt filter is unsupported")
        else:
            raise PDFNotImplementedError(f"Unsupported filter: {f!r}")
        if params and "Predictor" in params:
            data = _predict(data, params)
        _check(len(data), cap)
    _check(len(data), cap)
    if budget is not None:
        budget.left -= len(data)
    self.data = data
    self.rawdata = None


def _inflate(data: bytes, cap: int) -> bytes:
    """FlateDecode：最多解出 cap + 1 个字节，超过 cap 就停。解不开的照 pdfminer 的办法（先整段解，
    出错再一个字节一个字节地解，见 _inflate_corrupted）。"""
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(data, cap + 1)
    except zlib.error as exc:
        if settings.STRICT:
            raise PDFException(f"Invalid zlib bytes: {exc!r}（{len(data)} 字节）") from exc
        return _inflate_corrupted(data, cap)
    _check(len(out), cap)
    # 数据不完整（截断）：pdfminer 的 zlib.decompress 报错，严格模式下抛 PDFException，平时再逐字节
    # 解出能解的部分，就是 out
    if settings.STRICT and not inflater.eof:
        raise PDFException(
            f"Invalid zlib bytes: incomplete or truncated stream（{len(data)} 字节）"
        )
    return out


def _inflate_corrupted(data: bytes, cap: int) -> bytes:
    """pdfminer 的 decompress_corrupted 加上限：一个字节一个字节地解，在最后 3 个字节里出错的
    （一般是校验和不对）用已经解出的，在前面就出错的当空数据流（pdfminer 也是解出空的）。"""
    inflater = zlib.decompressobj()
    out = bytearray()
    for i in range(len(data)):
        try:
            out += inflater.decompress(data[i : i + 1], cap + 1 - len(out))
        except zlib.error:
            if i < len(data) - 3:
                return b""
            break
        _check(len(out), cap)
    return bytes(out)


def _check_ascii85(data: bytes, cap: int) -> None:
    if len(data) > ASCII85_INPUT_MAX:
        raise Refused("too_large", f"ASCII85 数据流超过 {ASCII85_INPUT_MAX // _MB}MB")
    zeros = data.count(b"z")  # 一个 z 解出 4 个字节，别的 5 个字符解出 4 个字节
    _check((len(data) - zeros) // 5 * 4 + 4 * zeros + 4, cap)


def _predict(data: bytes, params: dict) -> bytes:
    pred = int_value(params["Predictor"])
    if pred == 1:
        # no predictor
        return data
    if pred == 2 or pred >= 10:
        colors = int_value(params.get("Colors", 1))
        columns = int_value(params.get("Columns", 1))
        bitspercomponent = int_value(params.get("BitsPerComponent", 8))
        if len(data) > PREDICTOR_INPUT_MAX or columns > PREDICTOR_COLUMNS_MAX:
            raise Refused("too_large", "用预测器的数据流太大")
        if pred == 2:
            return apply_tiff_predictor(colors, columns, bitspercomponent, data)
        return apply_png_predictor(pred, colors, columns, bitspercomponent, data)
    raise PDFNotImplementedError(f"Unsupported predictor: {pred!r}")


install()
