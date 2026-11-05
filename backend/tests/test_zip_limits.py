"""xlsx、docx 交给 openpyxl、python-docx 之前的解压检查（zip_limits）。

只用内存里几 KB 的样本，tracemalloc 量内存峰值。三种炸弹：部件用 bzip2、LZMA 压缩（Python 解这两种
不受 read(n) 限制），deflate 部件目录里把大小写小（ZipFile.read() 一次解到底再截到写的大小）；
三种都把目录里的 CRC 也改成截短后内容的，读取库不报错、读出的和正常文件一样。每种先确认去掉检查时
内存真会涨（样本有效；检查拿掉，下面"有检查"那组就失败），再确认有检查时峰值很小、给出原因。
"""

import functools
import gc
import struct
import tracemalloc
import zipfile
import zlib
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from openpyxl import Workbook

from tests.test_advisor import advisor_settings as advisor_settings
from tests.test_advisor import agree_advisor, upload_body
from tests.test_advisor import client as client
from youju import zip_limits
from youju.compare import CompareInputError, read_customer_file
from youju.lists import parser

PAYLOAD = 8 * 1024 * 1024  # 炸弹里藏的空格：没有检查时读取库先分配这么多
PEAK = 2 * 1024 * 1024  # 有检查时整个调用的内存峰值上限
METHODS = {"bzip2": zipfile.ZIP_BZIP2, "lzma": zipfile.ZIP_LZMA, "deflate": zipfile.ZIP_DEFLATED}
NAME = "示例科技有限公司"
# 炸弹藏在 openpyxl、python-docx 用 ZipFile.read() 整段读的部件里
_TARGET = {"xlsx": "[Content_Types].xml", "docx": "word/document.xml"}
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    ),
    "_rels/.rels": (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="word/document.xml" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument"/></Relationships>'
    ),
    "word/document.xml": (
        f'<w:document xmlns:w="{_W}"><w:body><w:tbl>'
        + "".join(
            f"<w:tr><w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc></w:tr>"
            for text in ("企业名称", NAME)
        )
        + "</w:tbl></w:body></w:document>"
    ),
}


def office(kind: str) -> bytes:
    """正常的 xlsx（openpyxl 存的）、docx（最小的一份）：一张表，表头"企业名称"，一行企业。"""
    output = BytesIO()
    if kind == "xlsx":
        book = Workbook()
        book.active.append(["企业名称"])
        book.active.append([NAME])
        book.save(output)
        return output.getvalue()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in _DOCX_PARTS.items():
            archive.writestr(name, text.encode())
    return output.getvalue()


def patch_entry(data: bytes, name: str, *, crc=None, size=None, flags=None, method=None) -> bytes:
    """改目录和文件头里一个部件的 CRC、解压后大小、标志位、压缩方式（造假文件用）。"""
    out = bytearray(data)
    eocd = out.rindex(b"PK\x05\x06")
    count, _, pos = struct.unpack_from("<HLL", out, eocd + 10)
    for _ in range(count):
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", out, pos + 28)
        if out[pos + 46 : pos + 46 + name_len] == name.encode():
            local = struct.unpack_from("<L", out, pos + 42)[0]
            # 目录项和文件头里 CRC、解压后大小、标志位各自的位置
            for header, crc_at, size_at, flag_at in ((pos, 16, 24, 8), (local, 14, 22, 6)):
                if crc is not None:
                    struct.pack_into("<L", out, header + crc_at, crc)
                if size is not None:
                    struct.pack_into("<L", out, header + size_at, size)
                if flags is not None:
                    struct.pack_into("<H", out, header + flag_at, flags)
                if method is not None:  # 压缩方式紧跟在标志位后面
                    struct.pack_into("<H", out, header + flag_at + 2, method)
        pos += 46 + name_len + extra_len + comment_len
    return bytes(out)


@functools.cache
def bomb(kind: str, method: str, payload: int = PAYLOAD) -> bytes:
    """正常文件里的一个部件换成"原内容 + payload 字节空格"、用 method 压缩，目录和文件头里的
    大小、CRC 都写成原内容的。没有检查时读取库先解出整段、再截回原内容，CRC 对得上，不报错。"""
    target = _TARGET[kind]
    output = BytesIO()
    with (
        zipfile.ZipFile(BytesIO(office(kind))) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        original = source.read(target)
        for item in source.infolist():
            if item.filename != target:
                archive.writestr(item.filename, source.read(item))
        info = zipfile.ZipInfo(target)
        info.compress_type = METHODS[method]
        with archive.open(info, "w") as fh:
            fh.write(original)
            for _ in range(payload >> 20):
                fh.write(b" " * (1 << 20))
    return patch_entry(output.getvalue(), target, crc=zlib.crc32(original), size=len(original))


def measure(call) -> tuple[int, object]:
    """调用 call，返回（这次调用的内存峰值字节数，返回值；出错时是抛出的异常）。"""
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    before = tracemalloc.get_traced_memory()[0]
    try:
        outcome = call()
    except Exception as exc:  # 由各测试核对是哪种错
        outcome = exc
    peak = tracemalloc.get_traced_memory()[1] - before
    tracemalloc.stop()
    return peak, outcome


# 会把 Office 文件交给读取库的三条路：用户上传的表格（api 和后台任务都走它）、名单附件的
# xlsx 和 docx。值是（文件类型，读法，从结果里取企业名称）
READERS = {
    "upload_xlsx": (
        "xlsx",
        lambda data: read_customer_file("upload.xlsx", content=data),
        lambda rows: rows,
    ),
    "list_xlsx": (
        "xlsx",
        lambda data: parser.parse_attachment_result(data, "xlsx"),
        lambda result: [entry.name for entry in result.entries],
    ),
    "list_docx": (
        "docx",
        lambda data: parser.parse_attachment_result(data, "docx"),
        lambda result: [entry.name for entry in result.entries],
    ),
}


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("reader", READERS)
def test_bomb_sample_inflates_when_the_check_is_removed(monkeypatch, reader, method):
    kind, read, names = READERS[reader]
    data = bomb(kind, method)
    assert len(data) < 16 * 1024
    monkeypatch.setattr(zip_limits, "check", lambda *args, **kwargs: None)
    peak, outcome = measure(lambda: read(data))
    # 读取库照常读完、结果和正常文件一样，只是内存先涨了整段
    assert names(outcome) == [NAME]
    assert peak > PAYLOAD


@pytest.mark.parametrize(
    ("reader", "method", "payload", "error", "reason"),
    [
        ("upload_xlsx", "bzip2", PAYLOAD, CompareInputError, "表格的压缩方式不支持"),
        ("upload_xlsx", "lzma", PAYLOAD, CompareInputError, "表格的压缩方式不支持"),
        ("upload_xlsx", "deflate", PAYLOAD, CompareInputError, "表格解压后过大"),
        # 实际解出的没超 2MB，但和目录里写的不符
        ("upload_xlsx", "deflate", 1 << 20, CompareInputError, "表格文件已损坏"),
        ("list_xlsx", "bzip2", PAYLOAD, ValueError, "暂不支持此压缩方式（bzip2）"),
        ("list_xlsx", "lzma", PAYLOAD, ValueError, "暂不支持此压缩方式（LZMA）"),
        ("list_xlsx", "deflate", PAYLOAD, zipfile.BadZipFile, "部件实际大小和目录不符"),
        ("list_docx", "bzip2", PAYLOAD, ValueError, "暂不支持此压缩方式（bzip2）"),
        ("list_docx", "lzma", PAYLOAD, ValueError, "暂不支持此压缩方式（LZMA）"),
        ("list_docx", "deflate", PAYLOAD, zipfile.BadZipFile, "部件实际大小和目录不符"),
    ],
)
def test_bomb_rejected_before_inflating(reader, method, payload, error, reason):
    kind, read, _ = READERS[reader]
    data = bomb(kind, method, payload)
    peak, outcome = measure(lambda: read(data))
    assert isinstance(outcome, error) and str(outcome).startswith(reason)
    assert peak < PEAK


@pytest.mark.parametrize("reader", READERS)
def test_normal_office_files_read_as_before(reader):
    kind, read, names = READERS[reader]
    assert names(read(office(kind))) == [NAME]


@pytest.mark.parametrize(
    ("method", "limit", "reason"),
    [
        ("bzip2", None, "暂不支持此压缩方式（bzip2）"),
        ("deflate", None, "解析失败：BadZipFile"),
        ("deflate", 1 << 20, "暂不支持超过安全解析预算的文件（解压后超过 1MB）"),
    ],
)
def test_list_bomb_recorded_without_aborting_batch(monkeypatch, method, limit, reason):
    from youju.lists import service

    if limit is not None:
        monkeypatch.setattr(parser, "_MAX_ZIP_UNCOMPRESSED", limit)
    response = SimpleNamespace(status="ok", final_url=None, content=bomb("docx", method), text="")
    fetcher = SimpleNamespace(fetch=lambda _: response)
    recorded = []
    monkeypatch.setattr(service, "_government_url", lambda _: True)
    monkeypatch.setattr(service, "_save", lambda *args, **kwargs: recorded.append(args))
    service._parse_source(
        None, None, "https://test.gov.cn/list.docx", "docx", "attachment", None, fetcher
    )
    # 压缩方式不支持、超上限记"暂不支持"（文件不会变，不再重试）；大小不符和以前读取库报 CRC 错时
    # 一样记坏文件，过后重试
    assert len(recorded) == 1 and recorded[0][-2] is None and recorded[0][-1] == reason


def test_list_too_many_members_not_retried(monkeypatch):
    monkeypatch.setattr(parser, "_MAX_ZIP_MEMBERS", 2)
    with pytest.raises(ValueError, match=r"^暂不支持超过安全解析预算的文件（内部文件超过 2 个）$"):
        parser.parse_attachment_result(office("docx"), "docx")


@pytest.mark.parametrize(
    ("method", "payload", "reason"),
    [
        ("bzip2", PAYLOAD, "压缩方式不支持"),
        ("lzma", PAYLOAD, "压缩方式不支持"),
        ("deflate", PAYLOAD, "解压后过大"),
        ("deflate", 1 << 20, "已损坏"),
    ],
)
def test_upload_bomb_rejected_with_reason(client, method, payload, reason):
    agree_advisor(client)
    body = upload_body(bomb("xlsx", method, payload), name="clients.xlsx")
    key = f"bomb-{method}-{payload}"
    for path, headers in (("preview", {}), ("tasks", {"Idempotency-Key": key})):
        response = client.post(f"/api/v1/advisor/{path}", headers=headers, json=body)
        assert response.status_code == 422 and reason in response.json()["detail"]


def test_legacy_result_bomb_not_opened():
    from youju.advisor_tasks import legacy_result_document_ids

    class SessionStub:
        def scalars(self, query):
            raise AssertionError("炸弹不该读到原文链接")

    content = bomb("xlsx", "deflate", 1 << 20)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    with (
        httpx.Client(transport=transport) as http,
        pytest.raises(zip_limits.ZipLimitError, match="实际大小和目录不符"),
    ):
        legacy_result_document_ids(SessionStub(), http, "https://example.test/file")


def _zip(members: dict[str, bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", method) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output.getvalue()


def _check(data: bytes, max_bytes: int = 1000, max_members: int = 10) -> None:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        zip_limits.check(archive, max_bytes=max_bytes, max_members=max_members)


def _kind(data: bytes, **limits) -> str:
    with pytest.raises(zip_limits.ZipLimitError) as caught:
        _check(data, **limits)
    return caught.value.kind


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_exact_limits_pass_one_more_fails(method):
    data = _zip({"a": b"x" * 600, "b": b"y" * 400}, method)
    _check(data, max_bytes=1000, max_members=2)
    assert _kind(data, max_bytes=999) == "size"
    assert _kind(data, max_members=1) == "members"
    _check(_zip({}))


def test_size_written_small_counted_by_what_actually_inflates():
    real = b"x" * 1001
    lying = patch_entry(_zip({"a": real}), "a", crc=zlib.crc32(real[:10]), size=10)
    assert _kind(lying, max_bytes=1000) == "size"  # 实际解出 1001 字节，超上限
    assert _kind(lying, max_bytes=5000) == "broken"  # 没超上限，但和目录写的不符
    # 写大了（实际只有 10 字节）也不符
    assert _kind(patch_entry(_zip({"a": b"x" * 10}), "a", size=500)) == "broken"


def test_limit_shared_by_all_members():
    # a 写的是实话（600），b 实际 600 却写成 1：合计按实际的 1200 算
    lying = patch_entry(_zip({"a": b"x" * 600, "b": b"y" * 600}), "b", size=1)
    assert _kind(lying, max_bytes=1000) == "size"


@pytest.mark.parametrize(
    ("change", "kind", "detail"),
    [
        ({"flags": 0x1}, "method", "加密"),
        ({"method": 9}, "method", "deflate64"),
        ({"method": 97}, "method", "编号 97"),
        # 规范里没有的编号多半是文件坏了：按坏文件算，名单解析过后会重试
        ({"method": 7}, "broken", ""),
        ({"method": 0xFFFF}, "broken", ""),
    ],
)
def test_other_methods_and_encryption_rejected(change, kind, detail):
    data = patch_entry(_zip({"a": b"x" * 10}), "a", **change)
    with pytest.raises(zip_limits.ZipLimitError) as caught:
        _check(data)
    assert caught.value.kind == kind and caught.value.detail == detail


def test_two_entries_for_the_same_data_rejected():
    data = _zip({"a": b"x" * 10})
    eocd = data.rindex(b"PK\x05\x06")
    count, size, offset = struct.unpack_from("<HLL", data, eocd + 10)
    entry = data[offset : offset + size]  # 只有一个部件：整个目录就是这一项
    record = bytearray(data[eocd:])
    struct.pack_into("<HHLL", record, 8, count + 1, count + 1, size * 2, offset)
    assert _kind(data[:offset] + entry + entry + bytes(record)) == "broken"


def test_damaged_data_rejected():
    data = bytearray(_zip({"a": bytes(i % 251 for i in range(900))}))
    start = data.index(b"PK\x03\x04") + 30 + 1  # 文件头 30 字节 + 文件名 "a"
    data[start : start + 20] = b"\xff" * 20
    assert _kind(bytes(data)) == "broken"


def _zip64_entry(data: bytes, name: str, size: int) -> bytes:
    """把一个部件的目录项改成 zip64 写法：解压后大小改写在 zip64 扩展里，写成 size。"""
    eocd = data.rindex(b"PK\x05\x06")
    count, _, offset = struct.unpack_from("<HLL", data, eocd + 10)
    entries, pos = [], offset
    for _ in range(count):
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, pos + 28)
        end = pos + 46 + name_len + extra_len + comment_len
        entry = bytearray(data[pos:end])
        if entry[46 : 46 + name_len] == name.encode():
            struct.pack_into("<L", entry, 24, 0xFFFFFFFF)
            struct.pack_into("<H", entry, 30, extra_len + 12)
            at = 46 + name_len + extra_len
            entry[at:at] = struct.pack("<HHQ", 1, 8, size)
        entries.append(bytes(entry))
        pos = end
    directory = b"".join(entries)
    record = bytearray(data[eocd:])
    struct.pack_into("<L", record, 12, len(directory))
    return data[:offset] + directory + bytes(record)


class _Unseekable(BytesIO):
    """不能倒回去补文件头的输出：zipfile 只好在每个部件后面写数据描述符（标志位第 3 位）。"""

    def seek(self, *args):
        raise OSError("不能倒回去")

    def seekable(self):
        return False


def test_zip64_size_written_small_counted_by_what_actually_inflates():
    data = _zip64_entry(_zip({"a": b"x" * 1001}), "a", 10)
    with zipfile.ZipFile(BytesIO(data)) as archive:
        assert archive.getinfo("a").file_size == 10  # zipfile 认 zip64 扩展里写的
    assert _kind(data, max_bytes=1000) == "size"
    assert _kind(data, max_bytes=5000) == "broken"


def test_data_descriptor_member_checked_by_central_directory():
    stream = _Unseekable()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a", b"x" * 600)
    data = stream.getvalue()
    with zipfile.ZipFile(BytesIO(data)) as archive:
        assert archive.getinfo("a").flag_bits & 0x8
    _check(data)
    assert _kind(patch_entry(data, "a", size=10)) == "broken"


def test_stored_member_with_more_data_than_written_rejected():
    data = _zip({"a": b"x" * 10, "b": b"y" * 10}, zipfile.ZIP_STORED)
    # 存了 10 字节、目录写解压后 5 字节：zipfile 读到写的大小就停，实际有的比写的多
    assert _kind(patch_entry(data, "a", size=5, crc=zlib.crc32(b"x" * 5))) == "broken"


def test_local_header_method_ignored():
    # 只认目录里写的方式：本地文件头写成 bzip2，zipfile 照样按目录的 deflate 读（检查和读取库一致）
    data = bytearray(_zip({"a": b"x" * 600}))
    struct.pack_into("<H", data, 8, zipfile.ZIP_BZIP2)
    _check(bytes(data))
    with zipfile.ZipFile(BytesIO(bytes(data))) as archive:
        assert archive.read("a") == b"x" * 600


def test_prefixed_zip_checked_the_same():
    real = b"x" * 1001
    lying = patch_entry(_zip({"a": real}), "a", crc=zlib.crc32(real[:10]), size=10)
    # 前面拼了别的数据：zipfile 按目录位置推算偏移，检查和读取库读的还是同一段
    assert _kind(b"junk" * 100 + lying, max_bytes=5000) == "broken"


def test_check_reads_in_small_chunks():
    data = _zip({"a": b" " * PAYLOAD})
    peak, outcome = measure(lambda: _check(data, max_bytes=PAYLOAD))
    assert outcome is None and peak < PEAK
