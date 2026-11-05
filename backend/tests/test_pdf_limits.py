"""PDF 解压上限（pdf_limits）：压缩炸弹在四处读 PDF 的地方（附件副本、附件文字、名单解析、PDF 正文）
都停在上限、按"太大""格式不支持"记，内存不跟着涨；不超上限的数据流解出的和 pdfminer 原来的写法一样。
样本都是内存里现做的几百字节到几百 KB 的 PDF，不联网、不用数据库。"""

import contextlib
import functools
import gc
import logging
import random
import tracemalloc
import zlib
from pathlib import Path

import pdfminer
import pdfminer.settings
import pytest
from pdfminer.pdfexceptions import PDFNotImplementedError
from pdfminer.pdftypes import PDFStream
from pdfminer.psparser import LIT

from youju import attachment_text, pdf_limits
from youju import attachments as copies
from youju.crawler.pdf import extract_pdf_text
from youju.lists.parser import parse_attachment

MB = 1024 * 1024
SRC = Path(__file__).parents[1] / "src" / "youju"
TEXT = b"BT /F1 12 Tf 20 100 Td (Notice 2026) Tj ET"


def spaces_flate(size: int) -> bytes:
    """size 个空格（合法的页面内容，什么也不画）压成 FlateDecode；分块压，不占 size 那么大的内存。"""
    packer = zlib.compressobj(9)
    chunk = b" " * MB
    out = [packer.compress(chunk) for _ in range(size // MB)]
    out.append(packer.compress(b" " * (size % MB)))
    return b"".join([*out, packer.flush()])


@functools.cache
def review_bomb() -> bytes:
    """11/5 独立审阅那样的数据流：300MB 的空格压成约 300KB（几个测试共用，只压一次）。"""
    return spaces_flate(300 * MB)


def make_pdf(*streams: tuple[bytes, bytes]) -> bytes:
    """每个 (数据流字典里加的项, 数据流的原始字节) 一页：先画一行字，再接这个数据流（也是页面内容）。"""
    objs = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    objs.append(b"<< /Length %d >>\nstream\n" % len(TEXT) + TEXT + b"\nendstream")
    kids = []
    for extra, raw in streams:
        objs.append(b"<< /Length %d %s >>\nstream\n" % (len(raw), extra) + raw + b"\nendstream")
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents [4 0 R %d 0 R]"
            b" /Resources << /Font << /F1 3 0 R >> >> >>" % len(objs)
        )
        kids.append(b"%d 0 R" % len(objs))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def objstm_pdf(
    stream_extra: bytes,
    inner: dict[int, bytes],
    size: int,
    info: bytes = b"",
    filters: bytes = b"/FlateDecode",
) -> bytes:
    """PDF 1.5：交叉引用流 + 一个对象流（12 号，数据按 FlateDecode 压，字典里 /Filter 写 filters，
    stream_extra 是字典里再加的项），
    inner 里的对象放在对象流里，对象流解压后补空格到 size 字节。页面、字体、页面内容是普通对象；
    info 给了就当文档信息字典（元数据，11 号对象）。"""
    plain = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        4: b"<< /Length %d >>\nstream\n" % len(TEXT) + TEXT + b"\nendstream",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    if info:
        plain[11] = info
    for objid in inner:
        plain.pop(objid, None)  # 放进对象流的就不再单独写
    header = bodies = b""
    for objid, body in inner.items():
        header += b"%d %d " % (objid, len(bodies))
        bodies += body + b" "
    data = header + bodies
    packer = zlib.compressobj(9)
    raw = packer.compress(data) + b"".join(
        packer.compress(b" " * min(MB, size - len(data) - done))
        for done in range(0, max(0, size - len(data)), MB)
    )
    raw += packer.flush()
    plain[12] = (
        b"<< /Type /ObjStm /N %d /First %d /Length %d /Filter %s %s >>\nstream\n"
        % (len(inner), len(header), len(raw), filters, stream_extra)
        + raw
        + b"\nendstream"
    )
    where = {objid: (2, 12, index) for index, objid in enumerate(inner)}
    out = b"%PDF-1.5\n"
    for objid, body in sorted(plain.items()):
        where[objid] = (1, len(out), 0)
        out += b"%d 0 obj\n" % objid + body + b"\nendobj\n"
    xref = max(where) + 1
    where[xref] = (1, len(out), 0)
    rows = b"".join(
        bytes([kind]) + a.to_bytes(4, "big") + b.to_bytes(2, "big")
        for kind, a, b in (where.get(i, (0, 0, 0xFFFF)) for i in range(xref + 1))
    )
    trailer = b"/Root 1 0 R" + (b" /Info 11 0 R" if info else b"")
    out += (
        b"%d 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] %s /Length %d >>\nstream\n"
        % (xref, xref + 1, trailer, len(rows))
        + rows
        + b"\nendstream\nendobj\n"
    )
    return out + b"startxref\n%d\n%%%%EOF\n" % where[xref][1]


def texts(result: attachment_text.Extraction) -> list[str]:
    return [segment.text for segment in result.segments]


def peak_memory(read) -> int:
    """read() 期间 Python 分配的内存最多比开始时多多少（tracemalloc，含 zlib 解出的数据）。"""
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    try:
        read()
        return tracemalloc.get_traced_memory()[1] - base
    finally:
        if started:
            tracemalloc.stop()


def results(pdf: bytes) -> dict:
    """四处读 PDF 的结果：附件副本的检查、附件文字、名单解析（失败时的原因）、PDF 正文。"""
    check = copies.check_content(pdf, "pdf")
    text = attachment_text.extract(pdf)
    try:
        listed = parse_attachment(pdf, "pdf")
    except ValueError as exc:
        listed = str(exc)
    return {
        "copy": check if isinstance(check, str) else check.code,
        "text": (text.reason, text.detail),
        "list": listed,
        "body": extract_pdf_text(pdf),
    }


@pytest.fixture
def small_caps(monkeypatch) -> None:
    """上限调小（一个数据流 1MB、整份合计 3MB），几 KB 的样本就能超。"""
    monkeypatch.setattr(pdf_limits, "STREAM_MAX", 1 * MB)
    monkeypatch.setattr(pdf_limits, "DOCUMENT_MAX", 3 * MB)


# ---- 压缩炸弹 ----


def test_flate_bomb_fails_in_all_four_readers(small_caps) -> None:
    ok = make_pdf((b"/Filter /FlateDecode", spaces_flate(MB // 2)))
    assert results(ok) == {
        "copy": "pdf",
        "text": (None, None),
        "list": [],
        "body": ("Notice 2026", None),
    }
    bomb = make_pdf((b"/Filter /FlateDecode", spaces_flate(8 * MB)))
    assert len(bomb) < 16 * 1024
    assert results(bomb) == {
        # 取不出文字、没法查个人信息：不存副本
        "copy": "unchecked",
        "text": ("too_large", "PDF 数据流解压后超过 1MB"),
        # "暂不支持"开头：名单记成 unsupported，不再重试
        "list": "暂不支持这份 PDF（数据流解压后超过 1MB）",
        "body": ("", "PDF 数据流解压后超过 1MB"),
    }


def test_review_sample_keeps_memory_bounded_at_real_limits() -> None:
    """11/5 独立审阅的样本：约 300KB 的 PDF，一个数据流解压后 300MB（以前进程内存从 71MB 涨到 656MB：
    zlib 整段解开时先分块、最后拼成一整段，要占解出大小的两倍）。现在解到上限就停。"""
    bomb = make_pdf((b"/Filter /FlateDecode", review_bomb()))
    assert len(bomb) < 400 * 1024
    for read in (
        lambda: copies.check_content(bomb, "pdf"),
        lambda: attachment_text.extract(bomb),
        lambda: extract_pdf_text(bomb),
    ):
        assert peak_memory(read) < 2 * pdf_limits.STREAM_MAX + 16 * MB
    assert attachment_text.extract(bomb).reason == "too_large"


def test_doubly_compressed_bomb_is_tiny_and_refused() -> None:
    """数据流压两遍：300MB 的空格压一遍约 300KB，再压一遍不到 1KB。第二遍解压停在上限。"""
    raw = zlib.compress(review_bomb(), 9)
    bomb = make_pdf((b"/Filter [/FlateDecode /FlateDecode]", raw))
    assert len(bomb) < 2048
    assert peak_memory(lambda: attachment_text.extract(bomb)) < 2 * pdf_limits.STREAM_MAX + 16 * MB
    result = attachment_text.extract(bomb)
    assert result.reason == "too_large"
    assert result.detail == f"PDF 数据流解压后超过 {pdf_limits.STREAM_MAX // MB}MB"


def test_many_streams_stop_at_document_total(small_caps) -> None:
    """每个数据流都没超（900KB），合计超了（pdfminer 把解过的数据流留在内存里，整份读完才释放）。"""
    page = (b"/Filter /FlateDecode", spaces_flate(900 * 1024))
    many = make_pdf(*[page] * 5)
    result = attachment_text.extract(many)
    assert (result.reason, result.detail) == ("too_large", "PDF 解压后合计超过 3MB")
    # 每份 PDF 各算各的：一份读完，下一份重新计
    one = make_pdf(page, page)
    assert attachment_text.extract(one).ok
    assert attachment_text.extract(one).ok
    assert extract_pdf_text(one) == ("Notice 2026\nNotice 2026", None)


@pytest.mark.parametrize(
    "name", ["LZWDecode", "RunLengthDecode", "CCITTFaxDecode", "ASCII85Decode", "LZW", "RL", "A85"]
)
def test_filters_without_limits_are_refused(name: str) -> None:
    """pdfminer 的 LZW、RunLength、CCITT、ASCII85 解码器内存或时间没有上限（ASCII85 去空白的正则
    是平方级：几万个空格就要几秒）；抽查的真实文件里一个都没有（图片用的不解）。"""
    pdf = make_pdf((b"/Filter /%s" % name.encode(), b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"))
    detail = f"用了不支持的压缩方式 {name}"
    assert results(pdf) == {
        "copy": "unchecked",
        "text": ("format", f"PDF {detail}"),
        "list": f"暂不支持这份 PDF（{detail}）",
        "body": ("", f"PDF {detail}"),
    }


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        (b"/Columns 50000000", "too_large"),
        (b"/Colors 50000000", None),
        (b"/Colors 400000000 /BitsPerComponent 1", None),
        (b"/BitsPerComponent 64", "broken"),  # pdfminer 只认位深 1、8，别的直接报错
    ],
)
def test_predictor_parameters_cannot_blow_up_memory(params: bytes, reason: str | None) -> None:
    """pdfminer 的 PNG 预测器先按列数分配一行（整数列表）：5000 万列要 450MB 内存，文件只有几百字节，
    列数超了不解。颜色数、位深再大也不先分配（20260107 版只按列数分配），内存照样不涨。"""
    raw = zlib.compress(bytes([2]) + bytes(999))
    pdf = make_pdf((b"/Filter /FlateDecode /DecodeParms << /Predictor 12 %s >>" % params, raw))
    assert peak_memory(lambda: attachment_text.extract(pdf)) < 8 * MB
    result = attachment_text.extract(pdf)
    assert result.reason == reason
    if reason == "too_large":
        assert result.detail == "PDF 用预测器的数据流太大"
    if reason is None:
        assert texts(result) == ["Notice 2026"]


def test_used_up_total_never_means_unlimited(caplog) -> None:
    """合计额度用完（剩 0）时也是上限：zlib 的 max_length 给 0 是"不限"，这里给的是 1。拒读记一条日志；
    没给 on_refused 时原样抛出 Refused；出了 reading() 额度就清掉。"""
    bomb = PDFStream({"Filter": LIT("FlateDecode")}, review_bomb())

    def read() -> None:
        with pytest.raises(pdf_limits.Refused, match="合计"), pdf_limits.reading():
            pdf_limits._BUDGET.get().left = 0
            bomb.get_data()

    with caplog.at_level(logging.INFO, logger="youju.pdf_limits"):
        assert peak_memory(read) < MB
    assert "PDF 不读：解压后合计超过" in caplog.text
    assert pdf_limits._BUDGET.get() is None
    assert bomb.data is None and bomb.rawdata is not None  # 没解成，下次再读还是拒绝


# 页面树（2 号）和 /Predictor 的值（20 号）都在对象流里：打开文件要先解对象流，解的时候又要读 20 号
SELF_REF = {2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 20: b"1"}


def test_stream_referring_to_itself_stops_at_the_total(small_caps) -> None:
    """独立审阅实测：对象流的解码参数 /Predictor 指向这个对象流里的对象，要解它就得先解对象流自己，
    一层套一层，每层都拿满一个数据流的额度（812 字节的文件涨到 7GB）。现在每层先预留额度，合计用完
    就停（这里 1MB、合计 3MB：解到第 4 层停）。"""
    pdf = objstm_pdf(b"/DecodeParms << /Predictor 20 0 R >>", SELF_REF, 900 * 1024)
    assert len(pdf) < 4096
    # 留着的不超过合计上限，再加正在解的那一层 zlib 的两倍：以前是几百层 × 900KB，直到递归太深
    assert peak_memory(lambda: results(pdf)) < 4 * pdf_limits.DOCUMENT_MAX
    assert results(pdf) == {
        "copy": "unchecked",
        "text": ("too_large", "PDF 解压后合计超过 3MB"),
        "list": "暂不支持这份 PDF（解压后合计超过 3MB）",
        "body": ("", "PDF 解压后合计超过 3MB"),
    }


def test_streams_nested_too_deep_are_refused(monkeypatch) -> None:
    """合计额度很宽时，一层套一层也有上限（最多 8 层）。"""
    monkeypatch.setattr(pdf_limits, "STREAM_MAX", 1 * MB)
    monkeypatch.setattr(pdf_limits, "DOCUMENT_MAX", 512 * MB)
    pdf = objstm_pdf(b"/DecodeParms << /Predictor 20 0 R >>", SELF_REF, 900 * 1024)
    assert peak_memory(lambda: attachment_text.extract(pdf)) < 16 * MB
    result = attachment_text.extract(pdf)
    assert (result.reason, result.detail) == ("broken", "PDF 数据流互相引用、套得太深")


def test_filter_referring_into_its_own_object_stream_is_refused() -> None:
    """/Filter 本身指向这个对象流里的对象：要知道怎么解就得先解自己（取解码方式时就套进去，还没解出
    任何数据）。pdfminer 原来一直套到递归太深，现在第 9 层拒绝。"""
    inner = {2: SELF_REF[2], 20: b"/FlateDecode"}
    pdf = objstm_pdf(b"", inner, 4096, filters=b"20 0 R")
    assert peak_memory(lambda: results(pdf)) < 4 * MB
    detail = "数据流互相引用、套得太深"
    assert results(pdf) == {
        "copy": "unchecked",
        "text": ("broken", f"PDF {detail}"),
        "list": f"暂不支持这份 PDF（{detail}）",
        "body": ("", f"PDF {detail}"),
    }


def test_refusal_swallowed_by_pdfplumber_still_fails_the_file(small_caps, monkeypatch) -> None:
    """pdfplumber 读文档信息（元数据）时吞掉异常、接着往下读：信息字典里 2000 项都指向超了上限的
    对象流里的对象，以前每项都解到上限再被吞掉（真实上限下每项约 18ms），最后整份照常"通过"。
    现在第一次超了以后不再解，整份按超了算。"""
    info = b"<< " + b" ".join(b"/K%d 20 0 R" % i for i in range(2000)) + b" >>"
    pdf = objstm_pdf(b"", {20: b"(Alice)"}, 2 * MB, info=info)
    calls = []
    inflate = pdf_limits._inflate
    monkeypatch.setattr(
        pdf_limits, "_inflate", lambda data, cap: calls.append(cap) or inflate(data, cap)
    )
    assert results(pdf) == {
        "copy": "unchecked",
        "text": ("too_large", "PDF 数据流解压后超过 1MB"),
        "list": "暂不支持这份 PDF（数据流解压后超过 1MB）",
        "body": ("", "PDF 数据流解压后超过 1MB"),
    }
    assert len(calls) == 4  # 四处各解一次就停
    # 对象流没超时照常读（同一份结构，信息字典读得出来）
    ok = objstm_pdf(b"", {20: b"(Alice)"}, 512 * 1024, info=info)
    assert results(ok)["copy"] == "pdf"


def test_swallowed_refusal_fails_a_normal_exit_only(small_caps) -> None:
    """拒读被吞掉以后：照常读完的，整份按拒读算；中途出了别的错（调用方自己的超时等），原样抛出。"""
    bomb = PDFStream({"Filter": LIT("FlateDecode")}, spaces_flate(2 * MB))

    def swallowed() -> None:
        with contextlib.suppress(pdf_limits.Refused):
            bomb.get_data()

    with pytest.raises(pdf_limits.Refused, match="数据流解压后超过 1MB"), pdf_limits.reading():
        swallowed()
        assert pytest.raises(pdf_limits.Refused, bomb.get_data)  # 之后再读直接拒，不再解
    with pytest.raises(KeyError, match="别的错"), pdf_limits.reading():
        swallowed()
        raise KeyError("别的错")
    with pdf_limits.reading():  # 新的一份重新计
        assert PDFStream({}, b"BT ET").get_data() == b"BT ET"


def test_predictor_input_limit(monkeypatch) -> None:
    """预测器的输入超了不解；不超的，内存是输入的十几倍（pdfminer 用整数列表），上限因此定得小。"""
    monkeypatch.setattr(pdf_limits, "PREDICTOR_INPUT_MAX", 256 * 1024)
    rows = bytes([2]) + bytes(255)  # 每行：过滤方式 + 255 个字节（Columns 255）
    params = b"/Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 255 >>"
    fits = make_pdf((params, zlib.compress(rows * 1024)))  # 256KB
    over = make_pdf((params, zlib.compress(rows * 1025)))
    assert peak_memory(lambda: attachment_text.extract(fits)) < 8 * MB
    assert attachment_text.extract(fits).ok
    assert attachment_text.extract(over).reason == "too_large"


def test_cross_reference_streams_get_a_larger_predictor_limit(monkeypatch) -> None:
    """交叉引用流（/Type /XRef）每个对象占 5–7 个字节，对象多的大文件要用到更大的预测器输入。"""
    monkeypatch.setattr(pdf_limits, "PREDICTOR_INPUT_MAX", 64 * 1024)
    monkeypatch.setattr(pdf_limits, "XREF_PREDICTOR_INPUT_MAX", 256 * 1024)
    raw = zlib.compress((bytes([2]) + bytes(255)) * 512)  # 128KB
    params = {"Predictor": 12, "Columns": 255}
    xref = PDFStream({"Type": LIT("XRef"), "Filter": LIT("Fl"), "DecodeParms": params}, raw)
    other = PDFStream({"Filter": LIT("Fl"), "DecodeParms": params}, raw)
    assert len(xref.get_data()) == 255 * 512
    with pytest.raises(pdf_limits.Refused, match="预测器"):
        other.get_data()


def test_earlier_files_are_collected_before_the_next(small_caps, monkeypatch) -> None:
    """pdfminer 的文档对象互相引用，读完要等循环垃圾回收才释放；拒读时解到一半的还挂在异常的回溯上。
    连着读几份不能一份份叠上去（独立审阅实测：同一个炸弹连读 6 份，内存叠到 800MB）：前面读完的
    合计解出超过 _COLLECT_AFTER（或者有拒读），下一份开始前先回收。"""
    monkeypatch.setattr(pdf_limits, "_COLLECT_AFTER", MB)
    bomb = objstm_pdf(b"/DecodeParms << /Predictor 20 0 R >>", SELF_REF, 900 * 1024)
    normal = make_pdf(*[(b"/Filter /FlateDecode", spaces_flate(700 * 1024))] * 2)
    gc.collect()
    gc.disable()  # 自动回收什么时候跑说不准：关掉，只看 reading() 自己回收
    tracemalloc.start()
    held = []
    try:
        for pdf in [bomb] * 4 + [normal] * 4:
            copies.check_content(pdf, "pdf")
            held.append(tracemalloc.get_traced_memory()[0])
    finally:
        tracemalloc.stop()
        gc.enable()
    assert max(held) < 2 * pdf_limits.DOCUMENT_MAX  # 不回收时 8 份叠到 16MB


# ---- 不超上限的，和 pdfminer 原来解出的一样 ----

_TEXT = zlib.compress(b"BT /F1 12 Tf (abc) Tj ET\n" * 200)
_PNG_ROWS = b"".join(bytes([2, i, i + 1, i + 2, i + 3]) for i in range(0, 200, 4))
CASES = {
    "flate": ({"Filter": LIT("FlateDecode")}, _TEXT),
    "flate with trailing bytes": ({"Filter": LIT("FlateDecode")}, _TEXT + b"\r\nend"),
    "truncated flate": ({"Filter": LIT("FlateDecode")}, _TEXT[:-40]),
    "bad checksum": ({"Filter": LIT("FlateDecode")}, _TEXT[:-1] + bytes([_TEXT[-1] ^ 1])),
    "corrupted in the middle": ({"Filter": LIT("Fl")}, _TEXT[:30] + b"\xff\xff\xff" + _TEXT[33:]),
    "hex": ({"Filter": LIT("ASCIIHexDecode")}, b"48 65 6c 6c 6f>"),
    "hex then flate": (
        {"Filter": [LIT("AHx"), LIT("FlateDecode")]},
        _TEXT.hex().encode() + b">",
    ),
    "png predictor": (
        {"Filter": LIT("FlateDecode"), "DecodeParms": {"Predictor": 12, "Columns": 4}},
        zlib.compress(_PNG_ROWS),
    ),
    "tiff predictor": (
        {"Filter": LIT("FlateDecode"), "DecodeParms": {"Predictor": 2, "Columns": 8}},
        zlib.compress(bytes(range(64))),
    ),
    "no predictor": (
        {"Filter": LIT("FlateDecode"), "DecodeParms": {"Predictor": 1}},
        _TEXT,
    ),
    "image": ({"Filter": LIT("DCTDecode")}, b"\xff\xd8\xff\xe0 jpeg"),
    "plain": ({}, b"BT ET"),
}


def decoded(decode, attrs: dict, raw: bytes):
    """解出的数据；出错时是异常的类名。"""
    stream = PDFStream(dict(attrs), raw)
    try:
        decode(stream)
    except Exception as exc:  # noqa: BLE001 - 对照两种写法抛的是不是同一种异常
        return exc.__class__.__name__
    assert stream.rawdata is None
    return stream.data


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("name", CASES)
def test_streams_within_limits_decode_like_pdfminer(monkeypatch, name: str, strict: bool) -> None:
    """不超上限的，解出的和 pdfminer 原来的写法逐字节一样；严格模式（STRICT）下坏数据照样报同一种错。"""
    monkeypatch.setattr(pdfminer.settings, "STRICT", strict)
    attrs, raw = CASES[name]
    theirs = decoded(pdf_limits._ORIGINAL, attrs, raw)
    assert decoded(pdf_limits._decode, attrs, raw) == theirs
    if not strict:
        assert isinstance(theirs, bytes)


_WORDS = [bytes(random.Random(i).choices(range(97, 123), k=1 + i % 9)) for i in range(400)]
_PROSE = b" ".join(random.Random(1105).choices(_WORDS, k=12000))  # 约 70KB，压完约 30KB
_PACKED = zlib.compress(_PROSE, 9)
DAMAGED = {
    "early": _PACKED[:2000] + b"\xff\xff\xff" + _PACKED[2003:],
    "late": _PACKED[:-3000] + b"\x00\xff\x00" + _PACKED[-2997:],
    "bit flips": bytes(b ^ (1 if i % 997 == 5 else 0) for i, b in enumerate(_PACKED)),
    "bad checksum": _PACKED[:-1] + bytes([_PACKED[-1] ^ 1]),
    "truncated": _PACKED[:-5000],
    "trailing bytes": _PACKED + b"\r\nendstream",
}


@pytest.mark.parametrize("damage", DAMAGED)
def test_corrupted_streams_decoded_in_chunks_like_pdfminer(monkeypatch, damage: str) -> None:
    """坏数据先按块解、出错的那块再逐字节：解出的和 pdfminer 从头逐字节解的一样（块调成 1KB，
    这几种坏法都跨好几块）。"""
    monkeypatch.setattr(pdf_limits, "_CHUNK", 1024)
    attrs, raw = {"Filter": LIT("FlateDecode")}, DAMAGED[damage]
    assert decoded(pdf_limits._decode, attrs, raw) == decoded(pdf_limits._ORIGINAL, attrs, raw)


@pytest.mark.parametrize(
    ("attrs", "error"),
    [
        ({"Filter": LIT("Crypt")}, "/Crypt filter is unsupported"),
        ({"Filter": LIT("Bogus")}, "Unsupported filter"),
        ({"Filter": LIT("FlateDecode"), "DecodeParms": {"Predictor": 5}}, "Unsupported predictor"),
    ],
)
def test_unsupported_filters_raise_like_pdfminer(attrs: dict, error: str) -> None:
    for decode in (pdf_limits._decode, pdf_limits._ORIGINAL):
        with pytest.raises(PDFNotImplementedError, match=error):
            decode(PDFStream(dict(attrs), _TEXT))


# ---- 装在哪里 ----


def test_every_pdf_reader_uses_the_limits() -> None:
    """src 里用 pdfplumber、pdfminer 读 PDF 的文件都套了 pdf_limits.reading()（解码换成有上限的写法）。"""
    assert PDFStream.decode is pdf_limits._decode
    readers = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.name != "pdf_limits.py"
        and any(word in path.read_text(encoding="utf-8") for word in ("pdfplumber", "pdfminer"))
    )
    assert readers == ["attachment_text.py", "attachments.py", "crawler/pdf.py", "lists/parser.py"]
    for name in readers:
        assert "pdf_limits.reading(" in (SRC / name).read_text(encoding="utf-8"), name


def test_pdfminer_version_is_the_one_decode_was_copied_from() -> None:
    """pdf_limits._decode 照抄 pdfminer 20260107 版的 PDFStream.decode。升级 pdfminer 时对照新版重看
    （新的解码方式、预测器、坏数据怎么处理），改完再改这里的版本号。"""
    assert pdfminer.__version__ == "20260107"
