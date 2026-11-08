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
  内存里，整份读完才释放。解一个数据流时先预留它的额度：解码参数可能指向对象流里的对象，要先解
  那个对象流（甚至就是自己），预留的额度不让里面那一层用（解到一半出错的，解出的部分照样扣着）；一层套一层最多
  _NESTING_MAX 层；
- 有一个数据流超了，这份 PDF 里后面的数据流都不再解，reading() 结束时整份按超了算：pdfplumber
  读文档信息（元数据）时会吞掉异常接着往下读，不能让它照常"读完"；
- 解码方式只认 FlateDecode、ASCIIHexDecode、ASCII85Decode，图片的几种原样留着（取文字时不解图片，
  和 pdfminer 一样）。LZW、RunLength、CCITTFax 不解，整份按"格式不支持"：pdfminer 的这几个解码器
  内存或时间没有上限，抽查的真实文件和测试里的真实样本里一个都没有。ASCII85 要解（测试里江苏的通知、
  上海的名单两份真实 PDF 正文都是 ASCII85 + Flate），pdfminer 去头尾 <~ ~> 的正则遇到长空白是平方级
  （几万个空格就要几秒），先去掉 a85decode 本来就忽略的空白再交给它，输入长度和解出大小也有上限；
- 预测器（交叉引用流常用 PNG 预测器）：pdfminer 按文件里写的列数先分配一行，结果存成整数列表
  （每个字节约占 9 字节内存，8MB 要 36 秒），输入长度和列数都有上限。
不超上限的，解出的数据和 pdfminer 原来的写法一样（坏数据的处理也照抄）。超了、不支持的抛 Refused；
pdfplumber 会把它包进 PdfminerException，用 refused(exc) 找出来。

上限怎么定（2026-11-05 按 11/4 公开数据快照里附件、名单的 PDF 链接下载了 329 份真实文件，把每份的
非图片数据流全部解一遍）：一个数据流解出最大 2.4MB（字体最大 1.5MB）；一份合计最大 35.6MB，中位
1.0MB；文字多的文件解出的大约是文件大小的 10 倍，20MB 的文件整份读完约 220MB。解码方式只有
FlateDecode（14,865 个）和 ASCIIHexDecode（5 个），预测器只有交叉引用流的 PNG 预测器（解出最大
5KB）。15,349 个数据流用这里的写法和 pdfminer 原来的写法解出的逐字节一样。

为什么不像解 rar（attachment_text._rar，PR #451）那样放进限内存的子进程：
1. 本机 macOS 设不了 RLIMIT_AS（setrlimit 报 ValueError，分配照样成功），测试证明不了上限，只有线上
   Linux 管用；
2. 四处调用拿回的东西（分段和出处、表格行、页数、图片面积）都要改成能跨进程传的格式，冒充 pdfplumber
   的测试也要跟着改；
3. 每份 PDF 多起一个 Python 进程、重新导入 pdfplumber（约 0.5 秒）。
这里的上限只管"解压"这一层。解出来的内容在上限以内、却让 pdfminer 生成海量对象的，它管不到：几十 MB
层层嵌套的方括号；字体的 ToUnicode 里一行 beginbfrange <00000000> <FFFFFFFF>（不到 1KB 的文件就让
pdfminer 建一个 2^32 项的字典）。这些要把整个取文字放进限内存、限时的子进程，另做。
"""

from __future__ import annotations

import gc
import logging
import re
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
# 预测器的输入长度、列数（pdfminer 的写法：每个字节约占 9 字节内存，一行按列数先分配）。
# 真实文件里用预测器的都是交叉引用流，解出最大 5KB。交叉引用流（/Type /XRef）放宽到 8MB：每个
# 对象占 5–7 个字节，2MB 只够 30 万个对象，带标签的大名单可能更多，它解不开整份就读不了
PREDICTOR_INPUT_MAX = 2 * _MB
XREF_PREDICTOR_INPUT_MAX = 8 * _MB
PREDICTOR_COLUMNS_MAX = 1 << 20
# ASCII85 的输入长度（Python 的 a85decode 每 4 个字节先存成一个小 bytes 对象，约占 45 字节）；
# a85decode 忽略的空白（它的默认 ignorechars）
ASCII85_INPUT_MAX = 8 * _MB
_A85_IGNORED = b" \t\n\r\x0b"
_FORM_FEEDS = re.compile(rb"\x0c+")
# 数据流套着数据流（解码参数指向对象流里的对象，那个对象流要先解）最多几层；正常文件一两层
_NESTING_MAX = 8
# 坏数据逐字节解之前，先一块一块地解，出错的那一块再逐字节（结果和 pdfminer 从头逐字节一样）
_CHUNK = 64 * 1024
# 读完的 PDF 要等循环垃圾回收才释放：pdfminer 的文档对象互相引用，拒读时解到一半的数据还挂在
# 异常的回溯上。定时任务在一个进程里连着读，大文件、炸弹会一份份叠上去（独立审阅实测：同一个炸弹
# 连读 6 份，内存叠到 800MB）。上次回收以后读完的合计解出超过这么多（或者有拒读），下一份开始前
# 先回收一次；平常的小文件（中位 1MB）不用每份都回收
_COLLECT_AFTER = 32 * _MB
_uncollected = 0

_IMAGE_FILTERS = (*LITERALS_DCT_DECODE, *LITERALS_JBIG2_DECODE, *LITERALS_JPX_DECODE)
_REFUSED_FILTERS = (
    *LITERALS_LZW_DECODE,
    *LITERALS_RUNLENGTH_DECODE,
    *LITERALS_CCITTFAX_DECODE,
)


class Refused(Exception):
    """这份 PDF 不读：reason 用 attachment_text.REASONS 里的代码（too_large：解压后太大；
    format：压缩方式不支持；broken：数据流互相引用、套得太深），detail 写具体原因（不带"PDF"
    两个字，调用方自己接）。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class _Budget:
    left: int
    # 第一次拒读：之后这份 PDF 里的数据流一律不解，reading() 结束时整份按拒读算
    refused: Refused | None = None


_BUDGET: ContextVar[_Budget | None] = ContextVar("pdf_decode_budget", default=None)
_DEPTH: ContextVar[int] = ContextVar("pdf_decode_depth", default=0)
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
    """读一份 PDF：这里面解出的数据合计不超过 DOCUMENT_MAX；有数据流被拒，整份按拒读算（哪怕异常
    被 pdfplumber 吞掉、照常读完了）。on_refused：把拒读换成调用方自己的异常；不给就抛 Refused。"""
    global _uncollected
    install()
    if _uncollected >= _COLLECT_AFTER:
        gc.collect()
        _uncollected = 0
    budget = _Budget(DOCUMENT_MAX)
    start = budget.left
    token = _BUDGET.set(budget)
    ended = False
    try:
        yield
    except Exception as exc:
        # 别的异常（调用方自己的超时等）原样抛：本来就不是"读成了"，不用改
        hit = refused(exc)
        if hit is None:
            raise
        raise _refusal(hit, on_refused) from exc
    else:
        if budget.refused is not None:  # 拒读被吞掉了，照常"读完"不算数
            raise _refusal(budget.refused, on_refused)
        ended = True
    finally:
        _BUDGET.reset(token)
        # 出了错（含拒读）时解到一半的数据挂在异常的回溯上、没记在额度里（嵌套时可能有好几层），
        # 下一份前一定回收；正常读完的按解出的合计算
        _uncollected += start - budget.left if ended else _COLLECT_AFTER


def _refusal(hit: Refused, on_refused: Callable[[Refused], Exception] | None) -> Exception:
    # 记一条：附件副本的检查只记"取不出文字"，看日志才分得清是不是解压超了
    log.info("PDF 不读：%s", hit.detail)
    return on_refused(hit) if on_refused is not None else Refused(hit.reason, hit.detail)


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
    budget = _BUDGET.get()
    if budget is not None and budget.refused is not None:
        raise Refused(budget.refused.reason, budget.refused.detail)
    depth = _DEPTH.get()
    token = _DEPTH.set(depth + 1)
    try:
        if depth >= _NESTING_MAX:
            raise Refused("broken", "数据流互相引用、套得太深")
        _decode_within(self, budget)
    except Refused as exc:
        if budget is not None and budget.refused is None:
            budget.refused = exc
        raise
    finally:
        _DEPTH.reset(token)


def _decode_within(self: PDFStream, budget: _Budget | None) -> None:
    data = self.rawdata
    if self.decipher:
        # Handle encryption
        assert self.objid is not None
        assert self.genno is not None
        data = self.decipher(self.objid, self.genno, data, self.attrs)
    # 先取解码方式：它可能指向对象流里的对象，要先解那个对象流（也计入合计），再算这个的额度
    filters = self.get_filters()
    cap = STREAM_MAX if budget is None else max(0, min(STREAM_MAX, budget.left))
    if budget is not None:
        budget.left -= cap  # 先预留，解的过程中再引用到的数据流用不到这一份
    try:
        for f, params in filters:
            if f in LITERALS_FLATE_DECODE:
                data = _inflate(data, cap)
            elif f in LITERALS_ASCII85_DECODE:
                data = _ascii85(data, cap)
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
                data = _predict(data, params, _predictor_limit(self))
            _check(len(data), cap)
        _check(len(data), cap)
    finally:
        # 退回预留、按已经解出的扣：解成了是解出的结果；解到一半出错的，解出的那部分还挂在异常上
        # （嵌套时好几层，pdfplumber 关闭文件时又会再解析一遍页面树），照样扣着，不让后面的又拿到
        # 满额度（独立审阅实测：不扣时峰值到合计上限的将近两倍）；没解出什么的不多扣（agy 第 5 轮）
        if budget is not None:
            budget.left += cap - min(len(data), cap)
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
    """pdfminer 的 decompress_corrupted 加上限：逐字节地解，在最后 3 个字节里出错的（一般是校验和
    不对）用已经解出的，在前面就出错的当空数据流（pdfminer 也是解出空的）。为了快，先一块一块地解，
    哪一块出错，退回这一块开头的状态再逐字节解这一块（zlib 解出的和怎么分块无关）。"""
    inflater = zlib.decompressobj()
    out = bytearray()
    for start in range(0, len(data), _CHUNK):
        saved = inflater.copy()
        try:
            out += inflater.decompress(data[start : start + _CHUNK], cap + 1 - len(out))
            _check(len(out), cap)
            continue
        except zlib.error:
            inflater = saved
        for i in range(start, min(start + _CHUNK, len(data))):
            try:
                out += inflater.decompress(data[i : i + 1], cap + 1 - len(out))
            except zlib.error:
                return b"" if i < len(data) - 3 else bytes(out)
            _check(len(out), cap)
    return bytes(out)


def _ascii85(data: bytes, cap: int) -> bytes:
    """ASCII85。pdfminer 的 ascii85decode 先用两个正则去掉头尾的 <~、~>（连同两边的空白），遇到
    长空白是平方级；后面的 a85decode 本来就忽略这几种空白（_A85_IGNORED）。先去掉它们、把连着的
    换页符（正则当空白、a85decode 不忽略）缩成一个，再交给 pdfminer：解出的和原来一样，时间是
    线性的。"""
    if len(data) > ASCII85_INPUT_MAX:
        raise Refused("too_large", f"ASCII85 数据流超过 {ASCII85_INPUT_MAX // _MB}MB")
    data = _FORM_FEEDS.sub(b"\x0c", data.translate(None, _A85_IGNORED))
    zeros = data.count(b"z")  # 一个 z 解出 4 个字节，别的 5 个字符解出 4 个字节
    _check((len(data) - zeros) // 5 * 4 + 4 * zeros + 4, cap)
    return ascii85decode(data)


def _predictor_limit(stream: PDFStream) -> int:
    xref = getattr(stream.get("Type"), "name", None) == "XRef"
    return XREF_PREDICTOR_INPUT_MAX if xref else PREDICTOR_INPUT_MAX


def _predict(data: bytes, params: dict, limit: int) -> bytes:
    pred = int_value(params["Predictor"])
    if pred == 1:
        # no predictor
        return data
    if pred == 2 or pred >= 10:
        colors = int_value(params.get("Colors", 1))
        columns = int_value(params.get("Columns", 1))
        bitspercomponent = int_value(params.get("BitsPerComponent", 8))
        if len(data) > limit or columns > PREDICTOR_COLUMNS_MAX:
            raise Refused("too_large", "用预测器的数据流太大")
        if pred == 2:
            return apply_tiff_predictor(colors, columns, bitspercomponent, data)
        return apply_png_predictor(pred, colors, columns, bitspercomponent, data)
    raise PDFNotImplementedError(f"Unsupported predictor: {pred!r}")


install()
