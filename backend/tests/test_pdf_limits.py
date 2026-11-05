"""PDF 解压上限（pdf_limits）：压缩炸弹在四处读 PDF 的地方（附件副本、附件文字、名单解析、PDF 正文）
都停在上限、按"太大""格式不支持"记，内存不跟着涨；不超上限的数据流解出的和 pdfminer 原来的写法一样。
样本都是内存里现做的几百字节到几百 KB 的 PDF，不联网、不用数据库。"""

import base64
import functools
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


@pytest.mark.parametrize("name", ["LZWDecode", "RunLengthDecode", "CCITTFaxDecode", "LZW", "RL"])
def test_filters_without_limits_are_refused(name: str) -> None:
    """pdfminer 的 LZW、RunLength、CCITT 解码器内存或时间没有上限；现在只有图片用（取文字不解图片）。"""
    pdf = make_pdf((b"/Filter /%s" % name.encode(), b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"))
    detail = f"用了不支持的压缩方式 {name}"
    assert results(pdf) == {
        "copy": "unchecked",
        "text": ("format", f"PDF {detail}"),
        "list": f"暂不支持这份 PDF（{detail}）",
        "body": ("", f"PDF {detail}"),
    }


@pytest.mark.parametrize(
    "params",
    [
        b"/Columns 50000000",
        b"/Colors 50000000",
        b"/Colors 400000000 /BitsPerComponent 1",
        b"/BitsPerComponent 64",
    ],
)
def test_predictor_parameters_cannot_blow_up_memory(params: bytes) -> None:
    """pdfminer 的 PNG 预测器先按列数分配一行（整数列表）：5000 万列要 450MB 内存，文件只有几百字节，
    列数超了不解。颜色数、位深再大也不先分配（20260107 版只按列数分配），内存照样不涨。"""
    raw = zlib.compress(bytes([2]) + bytes(999))
    pdf = make_pdf((b"/Filter /FlateDecode /DecodeParms << /Predictor 12 %s >>" % params, raw))
    assert peak_memory(lambda: attachment_text.extract(pdf)) < 8 * MB
    if params.startswith(b"/Columns"):
        result = attachment_text.extract(pdf)
        assert (result.reason, result.detail) == ("too_large", "PDF 用预测器的数据流太大")


def test_used_up_total_never_means_unlimited() -> None:
    """合计额度用完（剩 0）时也是上限：zlib 的 max_length 给 0 是"不限"，这里给的是 1。"""
    bomb = PDFStream({"Filter": LIT("FlateDecode")}, review_bomb())

    def read() -> None:
        with pdf_limits.reading():
            pdf_limits._BUDGET.get().left = 0
            with pytest.raises(pdf_limits.Refused, match="合计"):
                bomb.get_data()

    assert peak_memory(read) < MB
    assert bomb.data is None and bomb.rawdata is not None  # 没解成，下次再读还是拒绝


def test_ascii85_zero_runs_are_counted_before_decoding(small_caps) -> None:
    """ASCII85 里一个 z 解出 4 个字节：先数出解出多大，超了不解。"""
    pdf = make_pdf((b"/Filter /ASCII85Decode", b"z" * (512 * 1024) + b"~>"))
    result = attachment_text.extract(pdf)
    assert (result.reason, result.detail) == ("too_large", "PDF 数据流解压后超过 1MB")


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
    "ascii85": ({"Filter": LIT("A85")}, base64.a85encode(b"hello world " * 20 + bytes(8)) + b"~>"),
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
