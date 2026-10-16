"""附件副本：通知的附件在小程序里直接打开（打开后右上角菜单可转发、保存），不用复制原文链接到浏览器。

方案（2026-10-05 用本机 curl 取回微信官方文档原文核对后选定，依据见 PR 说明）：
- admin 定时任务（youju cache-attachments）从原网站下载申报通知近 180 天、
  其他类型近 90 天文件的附件，校验后存到云托管对象存储
  attachments/<内容的 SHA-256>.<格式>，和数据库备份 backups/、日志归档 logs/ 分开。
- 小程序不直接读对象存储：存储安全规则以存储桶为边界、对桶里全部文件生效，放开读取会连备份一起放开，
  所以存储桶保持"只有管理员可读写"；也不用 wx.downloadFile（要创始人在小程序后台配 downloadFile
  合法域名）。小程序经 callContainer 向 api 分段取文件（每段 900 KiB，云托管返回上限 1000 KiB），
  api 用开放接口 /tcb/batchdownloadfile 换临时链接后按段读取，链接不发给小程序。
- api 只按附件编号找这张表里登记的副本，只读 attachments/ 下的文件；不接受小程序传来的
  文件 ID 或路径，给不出备份。定时任务删除时同样只删 attachments/ 下的文件。
- 只存：政府网站链接；小程序能打开的 7 种格式（wx.openDocument 的 fileType：doc、docx、xls、
  xlsx、ppt、pptx、pdf）；不超过 20MB；文件头和扩展名是同一类格式；申报通知和政策（名单等
  可能有个人姓名，同 D-030 不给正文）；取得出文字、文字里没有身份证号、个人手机号、银行账号
  （合规红线要求入库前脱敏，副本是原文件、没法脱敏，查不了或查出来有就不存）；没有查不了的图片
  （图片里的身份证、手机号查不出来）：Office 文件里单张位图超过 10KB 或任一边超过 200 像素、
  矢量图或嵌入对象超过 50KB，PDF 里任一张位图超过页面的 1/10 或一页合计超过半页，都不存
  （小图标、小 logo 不算）。
- 规则版本：每份副本记下检查时用的规则版本（RULES_VERSION，附件规则改了就加 1）。api 只提供
  当前版本的副本，旧版本的立即当作"还在准备"；每轮定时任务先从对象存储读出旧版本的副本按新规则
  重查（每轮有上限），合格的升到新版本恢复提供，不合格的改成不提供、从对象存储删掉。
  youju recheck-attachments 留作手动重查。
- 每 7 天重新下载核对一次：内容变了（SHA-256 不同）换新副本、删旧的；原网站删了、变成网页或
  超过 20MB，不再提供旧副本（以原网站为准）；网络等临时问题保留已有副本，6 小时、24 小时后重试。
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import math
import re
import struct
import tempfile
import threading
import time as clock
import zipfile
import zlib
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from youju.backup import _BUSY_CODES, BEIJING, BackupError, _open_api, _retrying, upload
from youju.classify import effective_kind
from youju.config import get_settings
from youju.crawler.detail import has_personal_numbers
from youju.crawler.detail_service import _government_url
from youju.crawler.fetcher import MAX_RESPONSE_BYTES, FetchResult, PoliteFetcher
from youju.dedup import canonical_ids
from youju.models import AttachmentCopy, Document, DocumentAttachment, Source
from youju.takedown import available_condition
from youju.version import build_revision

log = logging.getLogger("youju.attachments")

# wx.openDocument 的 fileType 合法值（官方文档）
PREVIEW_TYPES = ("doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf")
# 文件头判断出的格式和扩展名要是同一类（.doc 实际是 docx 这类常见的情况照样能打开）
_FAMILY = {
    "doc": "word",
    "docx": "word",
    "xls": "sheet",
    "xlsx": "sheet",
    "ppt": "slides",
    "pptx": "slides",
    "pdf": "pdf",
}
# 只给这两类文件存副本：和详情页显示正文的范围一致（D-030：名单、其他类可能有个人姓名）
COPY_KINDS = ("申报通知", "政策")
WINDOW_DAYS = 90
APPLICATION_WINDOW_DAYS = 180
MAX_BYTES = MAX_RESPONSE_BYTES  # 20MB，抓取器超过就停止下载
# 每段大小：callContainer 返回包上限 1000 KiB，留出余量
PART_SIZE = 900 * 1024
MAX_PARTS = math.ceil(MAX_BYTES / PART_SIZE)
RECHECK = timedelta(days=7)
# 附件检查规则的版本：规则改了（个人信息、图片、格式等）就加 1。副本记下检查时的版本，
# 不是当前版本的不提供，等定时任务按新规则重查（0 = 加版本号之前的规则；1 = 10/7 起加了
# 图片检查、数字间带空白或横线的号码、不写"账号"的卡号）
RULES_VERSION = 1
# 每轮定时任务最多重查多少个旧版本的副本（从对象存储读，不访问原网站）
RECHECK_PER_RUN = 100
_RETRY_DELAYS = (timedelta(hours=6), timedelta(hours=24))
PREFIX = "attachments/"
# 附件副本的文件 ID：cloud://环境ID.存储桶ID/attachments/<SHA-256>.<格式>
# （cloudID 的组成见云托管《对象存储》文档）
_FILE_ID = re.compile(
    r"cloud://([^./]+)\.[^/]+/(attachments/[0-9a-f]{64}\.(?:docx?|xlsx?|pptx?|pdf))"
)
_DELETE_BATCH = 50
_DELETE_MAX = 500
# delete_list.errmsg 只做整段相等的文本分类。过长、非字符串、空白后对不上，都是 unknown。
# 这些词只说明平台返回了哪类提示，不能据此认为文件不存在、权限不足，或删除已经成功。
_HINT_TEXT_MAX = 48
_HINT_NAMES = (
    "not_found_hint",
    "permission_hint",
    "invalid_file_id_hint",
    "unknown",
)
_PLATFORM_HINTS = {
    "STORAGE_FILE_NONEXIST": "not_found_hint",
    "STORAGE_FILE_NOT_EXIST": "not_found_hint",
    "NoSuchKey": "not_found_hint",
    "file not exist": "not_found_hint",
    "STORAGE_EXCEED_AUTHORITY": "permission_hint",
    "AccessDenied": "permission_hint",
    "permission denied": "permission_hint",
    "INVALID_FILEID": "invalid_file_id_hint",
    "InvalidFileId": "invalid_file_id_hint",
    "invalid fileid": "invalid_file_id_hint",
}
RUN_BYTES = 1024**3  # 每轮最多下载 1GB
_PDF_PAGES = 300
_TEXT_MAX = 100 * 1024 * 1024  # Office 文件解压后的文字部分上限（防压缩炸弹）
_LINK_MAX_AGE = 1800  # 临时链接有效期（秒）
_LINK_TTL = 1200.0  # api 进程里缓存临时链接的时间，比有效期短
_LINKS_MAX = 500


def url_hash(url: str) -> str:
    return sha256(url.encode()).hexdigest()


def object_path(digest: str, file_type: str) -> str:
    return f"{PREFIX}{digest}.{file_type}"


def parse_file_id(file_id: str | None) -> tuple[str, str] | None:
    """附件副本的文件 ID → (环境 ID, 对象路径)。不是 attachments/ 下的副本（比如备份）返回 None。"""
    match = _FILE_ID.fullmatch(file_id or "")
    return (match.group(1), match.group(2)) if match else None


def parts_of(size: int) -> int:
    return max(1, math.ceil(size / PART_SIZE))


# ---- 判断格式、查个人信息 ----

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OOXML_MAIN = {
    "word/document.xml": "docx",
    "xl/workbook.xml": "xlsx",
    "ppt/presentation.xml": "pptx",
}
# 旧版 Office（复合文档）按根目录下的主数据流区分；嵌入的其他文件在子目录里，不算
_OLE_MAIN = {"WordDocument": "doc", "Workbook": "xls", "Book": "xls", "PowerPoint Document": "ppt"}


def _ole_root_names(data: bytes) -> set[str]:
    from xlrd.compdoc import CompDoc

    try:
        doc = CompDoc(data, logfile=io.StringIO())
        return {d.name for d in doc.dirlist if d.parent == 0}
    except Exception:  # noqa: BLE001 - 外部文件可能损坏，第三方解析器会抛多种异常
        return set()


def detect_type(data: bytes) -> str | None:
    """按文件头和内部结构判断格式（7 种之一）；认不出（网页、压缩包、加密文件、损坏）返回 None。"""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, ValueError, OSError, EOFError):
            return None
        found = {kind for name, kind in _OOXML_MAIN.items() if name in names}
        return found.pop() if len(found) == 1 else None
    if data.startswith(_OLE):
        found = {kind for name, kind in _OLE_MAIN.items() if name in _ole_root_names(data)}
        return found.pop() if len(found) == 1 else None
    return None


# 段落、单元格、换行结束处换行，其余标签去掉（同一段里分成几块的数字能连起来）
_BREAK_TAGS = re.compile(r"</?(?:w:p|w:tc|w:tr|w:br|w:tab|a:p|a:br|c|row|si|v)\b[^>]*>")
_TAGS = re.compile(r"<[^>]+>")

# ---- 图片：图片里的身份证、手机号查不出来，有大图就不存副本 ----

# Office 文件里的位图：单张超过 10KB 或任一边超过 200 像素就不存（小图标、logo 不算）
BITMAP_BYTES = 10 * 1024
BITMAP_SIDE = 200
# 矢量图（文字存在文件里，取出来查）和嵌入的对象：超过 50KB 就不存
OBJECT_BYTES = 50 * 1024
# PDF：任一张位图超过页面的 1/10，或一页上位图合计超过半页，就不存（小 logo 不算；线条图不是位图）
PDF_IMAGE_SHARE = 0.1
PDF_PAGE_SHARE = 0.5
_IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".bmp", ".dib", ".tif", ".tiff",
    ".webp", ".wdp", ".hdp", ".jxr", ".heic", ".ico", ".pct", ".pict",
    ".emf", ".wmf", ".emz", ".wmz", ".svg",
)  # fmt: skip
# 矢量图里的文字是存在文件里的：小的取出文字，和正文一起查（emz、wmz 是压缩过的）
_VECTOR_SUFFIXES = (".emf", ".wmf", ".svg", ".emz", ".wmz")
_INFLATE_MAX = 16 * 1024 * 1024


class HasImages(Exception):
    """附件里有查不了个人信息的图片或嵌入对象（异常说明写在哪里）。"""


def _pdf_image_areas(page, images: list[dict]) -> list[float]:
    """这一页上每张图片占的面积（超出页面的部分不算）。"""
    x0, top, x1, bottom = page.bbox
    areas = []
    for image in images:
        width = min(image["x1"], x1) - max(image["x0"], x0)
        height = min(image["bottom"], bottom) - max(image["top"], top)
        areas.append(width * height if width > 0 and height > 0 else 0.0)
    return areas


def _bitmap_size(raw: bytes) -> tuple[int, int] | None:
    """位图的宽、高（像素）；认不出返回 None。只读文件头，不解码像素。"""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as image:
            return image.size
    except Exception:  # noqa: BLE001 - 认不出的格式、损坏的文件、超大尺寸都按认不出处理
        pass
    # 旧版 Office 的 DIB 图片没有文件头，直接是 BITMAPINFOHEADER（或旧的 BITMAPCOREHEADER）
    if len(raw) >= 12:
        header = int.from_bytes(raw[:4], "little")
        if header == 12:
            return int.from_bytes(raw[4:6], "little"), int.from_bytes(raw[6:8], "little")
        if header in (40, 52, 56, 64, 108, 124) and len(raw) >= 12:
            width = int.from_bytes(raw[4:8], "little", signed=True)
            height = int.from_bytes(raw[8:12], "little", signed=True)
            return abs(width), abs(height)
    return None


def _check_bitmap(raw: bytes, size: int, where: str) -> None:
    """Office 文件里的一张位图：超过 10KB、任一边超过 200 像素、认不出尺寸，都抛 HasImages。"""
    if size > BITMAP_BYTES:
        raise HasImages(f"图片超过 10KB：{where}")
    dims = _bitmap_size(raw)
    if dims is None:
        raise HasImages(f"图片认不出尺寸：{where}")
    if max(dims) > BITMAP_SIDE:
        raise HasImages(f"图片超过 {BITMAP_SIDE} 像素：{where}")


def _pdf_text(data: bytes) -> str | None:
    import pdfplumber

    texts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if len(pdf.pages) > _PDF_PAGES:
            return None
        for number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            images = page.images
            # 某一页几乎没有文字但有图片：多半是扫描页，查不了
            scanned = len(text.strip()) < 10 and bool(images)
            areas = _pdf_image_areas(page, images) if images else []
            page_area = page.width * page.height
            page.close()  # 释放这一页解析出的对象，大文件不至于占满内存
            if scanned:
                return None
            if any(area > page_area * PDF_IMAGE_SHARE for area in areas):
                raise HasImages(f"PDF 第 {number} 页有超过 1/10 页面的图片")
            if sum(areas) > page_area * PDF_PAGE_SHARE:
                raise HasImages(f"PDF 第 {number} 页的图片合计超过半页")
            texts.append(text)
    joined = "\n".join(texts)
    return joined if joined.strip() else None


def _xml_text(raw: bytes) -> str:
    return html.unescape(_TAGS.sub("", _BREAK_TAGS.sub("\n", raw.decode("utf-8", "ignore"))))


def _ooxml_text(data: bytes, nested: bool = False) -> str | None:
    """新版 Office 文件里的文字。位图超过 10KB 或 200 像素、矢量图和嵌入的对象（嵌入的表格、
    文档等）超过 50KB，抛 HasImages；小的矢量图取出文字，小的嵌入文档取出它的文字（只往下看
    一层），一起查。"""
    parts: list[str] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename.lower()
            embedded = "/embeddings/" in f"/{name}"
            # 文件属性里的首页缩略图是正文首页的样子，正文已经查过
            image = (
                not embedded
                and not name.startswith("docprops/thumbnail")
                and ("/media/" in f"/{name}" or name.endswith(_IMAGE_SUFFIXES))
            )
            if embedded or image:
                vector = image and name.endswith(_VECTOR_SUFFIXES)
                limit = OBJECT_BYTES if embedded or vector else BITMAP_BYTES
                where = info.filename[:80]
                raw = b""
                if info.file_size <= limit:
                    with zf.open(info) as fh:  # 压缩包里写的大小可能不实，按实际读到的算
                        raw = fh.read(limit + 1)
                size = max(info.file_size, len(raw))
                if embedded:
                    if size > limit:
                        raise HasImages(f"嵌入的对象超过 50KB：{where}")
                    parts.append(_embedded_text(raw, nested))
                elif vector:
                    if size > limit:
                        raise HasImages(f"矢量图超过 50KB：{where}")
                    parts.append(_vector_text(name, raw))
                else:
                    _check_bitmap(raw, size, where)  # 小图标、logo 放行
                continue
            if not name.endswith(".xml"):
                continue
            with zf.open(info) as fh:
                raw = fh.read(_TEXT_MAX - total + 1)
            total += len(raw)
            if total > _TEXT_MAX:
                return None
            parts.append(_xml_text(raw))
    return "\n".join(parts)


def _vector_text(name: str, raw: bytes) -> str:
    if name.endswith(".svg"):
        return _xml_text(raw)
    if name.endswith((".emz", ".wmz")):
        return _binary_text(_inflate(raw, zlib.MAX_WBITS | 16))
    return _binary_text(raw)


def _embedded_text(raw: bytes, nested: bool) -> str:
    """新版 Office 文件里嵌入的小对象（不超过 50KB）的文字：嵌入的新版文档取出它的文字（只往下看
    一层），嵌入的旧版表格按单元格取（数字存成数值，按字节找不到），其余按字节解码。"""
    if not nested and raw.startswith(b"PK\x03\x04"):
        return _ooxml_text(raw, nested=True) or ""
    if raw.startswith(_OLE) and detect_type(raw) == "xls":
        return f"{_xls_text(raw)}\n{_binary_text(raw)}"
    return _binary_text(raw)


def _xls_text(data: bytes) -> str:
    import xlrd

    book = xlrd.open_workbook(file_contents=data, logfile=io.StringIO())
    cells: list[str] = []
    for sheet in book.sheets():
        for row in range(sheet.nrows):
            for value in sheet.row_values(row):
                # 手机号、身份证号常被存成数字：整数去掉".0"再查
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                cells.append(str(value))
    return "\n".join(cells)


def _binary_text(data: bytes) -> str:
    """旧版 Word、PowerPoint：中文正文按 UTF-16LE 存，数字也可能是单字节，几种解法都查一遍。"""
    return "\n".join(
        (
            data.decode("utf-16-le", "ignore"),
            data[1:].decode("utf-16-le", "ignore"),
            data.decode("latin-1"),
        )
    )


# 旧版 Office 的图片存成 OfficeArt 的 BLIP 记录（[MS-ODRAW] 2.2.23–2.2.31）：记录头 8 字节 =
# recVer/recInstance（2 字节）+ recType（2 字节）+ recLen（4 字节）。按各类图片固定的
# recInstance 和 recType 找记录头，recLen 就是图片的大小
_BLIP_HEAD = re.compile(
    rb"(?:[\x40\x50]\x3d\x1a"  # EMF
    rb"|[\x60\x70]\x21\x1b"  # WMF
    rb"|[\x20\x30]\x54\x1c"  # PICT
    rb"|(?:[\xa0\xb0]\x46|[\x20\x30]\x6e)[\x1d\x2a]"  # JPEG
    rb"|[\x00\x10]\x6e\x1e"  # PNG
    rb"|[\x80\x90]\x7a\x1f"  # DIB
    rb"|[\x40\x50]\x6e\x29"  # TIFF
    rb")\xf0"
)
_VECTOR_BLIPS = (0x1A, 0x1B, 0x1C)  # EMF、WMF、PICT
# 存图片的数据流：Word 的图片在 Data 和 WordDocument 流里，PowerPoint 在 Pictures 流里，
# Excel 在 Workbook 流的 MSODRAWINGGROUP 记录里
_OLE_PICTURE_STREAMS = {
    "doc": ("WordDocument", "Data", "1Table", "0Table"),
    "ppt": ("Pictures", "PowerPoint Document"),
    "xls": ("Workbook", "Book"),
}


def _excel_drawings(stream: bytes) -> bytes:
    """Excel 的 Workbook 流里图片所在的 MSODRAWINGGROUP 记录（0x00EB）：超过 8224 字节的部分
    接在后面的 CONTINUE 记录（0x003C）里，拼回完整的再找图片。"""
    out = bytearray()
    pos, inside = 0, False
    while pos + 4 <= len(stream):
        kind, size = struct.unpack_from("<HH", stream, pos)
        if kind == 0x00EB or (inside and kind == 0x003C):
            out += stream[pos + 4 : pos + 4 + size]
            inside = True
        else:
            inside = False
        pos += 4 + size
    return bytes(out)


def _inflate(blob: bytes, *formats: int) -> bytes:
    """解压矢量图（默认先按 zlib、再按不带头的 DEFLATE 试）；解不开或太大抛 HasImages。"""
    for wbits in formats or (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            inflater = zlib.decompressobj(wbits)
            out = inflater.decompress(blob, _INFLATE_MAX)
        except zlib.error:
            continue
        if inflater.unconsumed_tail:
            raise HasImages("矢量图解压后太大")
        return out
    raise HasImages("矢量图解不开")


def _vector_blip_text(stream: bytes, at: int, end: int) -> str:
    """小的矢量图（EMF、WMF、PICT）：解压后取出文字。头部见 [MS-ODRAW] 2.2.24–2.2.26。"""
    instance = int.from_bytes(stream[at : at + 2], "little") >> 4
    head = at + 8 + 16 * (2 if instance & 1 else 1)
    if head + 34 > end:
        raise HasImages("矢量图不完整")
    saved = int.from_bytes(stream[head + 28 : head + 32], "little")
    compression = stream[head + 32]
    blob = stream[head + 34 : min(end, head + 34 + saved)]
    if compression == 0xFE:
        return _binary_text(blob)
    if compression == 0x00:
        return _binary_text(_inflate(blob))
    raise HasImages("矢量图的压缩方式认不出")


def _blip_text(stream: bytes) -> str:
    texts = []
    for match in _BLIP_HEAD.finditer(stream):
        at = match.start()
        size = int.from_bytes(stream[at + 4 : at + 8], "little")
        if size > len(stream) - at - 8:
            continue  # 记录长度超出数据流：不是真的记录头，只是碰巧一样的几个字节
        end = at + 8 + size
        if stream[at + 2] in _VECTOR_BLIPS:
            if size > OBJECT_BYTES:
                raise HasImages("矢量图超过 50KB")
            texts.append(_vector_blip_text(stream, at, end))
            continue
        # 位图：记录头、1 或 2 个 16 字节的编号、1 字节标记之后就是图片文件本身
        instance = int.from_bytes(stream[at : at + 2], "little") >> 4
        start = at + 8 + 16 * (2 if instance & 1 else 1) + 1
        _check_bitmap(stream[start:end], size, "旧版 Office 里的图片")
    return "\n".join(texts)


def _ole_pictures(data: bytes, file_type: str) -> str:
    """旧版 Office 文件里的图片和嵌入的对象：位图超过 10KB 或 200 像素、矢量图和嵌入对象超过 50KB，
    抛 HasImages；小的矢量图解压后取出文字。"""
    from xlrd.compdoc import CompDoc

    doc = CompDoc(data, logfile=io.StringIO())
    children: dict[int, list] = defaultdict(list)
    for node in doc.dirlist:
        if node.parent >= 0 and node.DID != 0:
            children[node.parent].append(node)

    def size_of(node) -> int:
        return node.tot_size if node.etype == 2 else sum(size_of(c) for c in children[node.DID])

    # 嵌入的对象：Word 在 ObjectPool 下每个对象一个子目录，Excel 每个对象一个 MBD 开头的目录
    for top in children[0]:
        if top.etype != 1:
            continue
        if top.name == "ObjectPool":
            objects = children[top.DID]
        elif top.name.startswith("MBD"):
            objects = [top]
        else:
            continue
        if any(size_of(obj) > OBJECT_BYTES for obj in objects):
            raise HasImages("嵌入的对象超过 50KB")
    texts = []
    for name in _OLE_PICTURE_STREAMS.get(file_type, ()):
        stream = doc.get_named_stream(name)
        if not stream:
            continue
        texts.append(_blip_text(_excel_drawings(stream) if file_type == "xls" else stream))
    return "\n".join(texts)


def text_of(data: bytes, file_type: str) -> str | None:
    """附件里的文字，用来查个人信息；取不出（扫描件、损坏、太大）返回 None。
    有查不了的图片（见 HasImages）抛 HasImages。"""
    try:
        if file_type == "pdf":
            return _pdf_text(data)
        if file_type in ("docx", "xlsx", "pptx"):
            return _ooxml_text(data)
        pictures = _ole_pictures(data, file_type)
        main = _xls_text(data) if file_type == "xls" else _binary_text(data)
        return f"{main}\n{pictures}"
    except HasImages:
        raise
    except Exception:  # noqa: BLE001 - 第三方解析器对损坏文件会抛多种异常
        return None


@dataclass(frozen=True)
class Failure:
    code: str
    detail: str
    # 临时问题（网络、服务器出错、对象存储）：已有副本照常可用，较快重试
    transient: bool = False


def check_content(data: bytes, declared: str) -> str | Failure:
    """下载到的内容能不能存副本：能就返回实际格式。"""
    if not data:
        return Failure("empty", "下载到的内容是空的")
    detected = detect_type(data)
    if detected is None or _FAMILY[detected] != _FAMILY.get(declared):
        return Failure("mismatch", "文件内容和扩展名对不上（可能是网页，或文件已损坏、加密）")
    try:
        text = text_of(data, detected)
    except HasImages as exc:
        return Failure("image", f"含图片，无法核验个人信息（{exc}）")
    if text is None:
        return Failure("unchecked", "取不出文字（扫描件或文件损坏），没法检查有没有个人信息")
    if has_personal_numbers(text):
        return Failure("personal", "附件里有身份证号、个人手机号或银行账号")
    return detected


def fetch_failure(result: FetchResult) -> Failure:
    status, code = result.status, result.http_status
    if status == "too_large":
        return Failure("too_large", "附件超过 20MB")
    if status == "http_error" and code in (404, 410):
        return Failure("gone", f"原网站已没有这个附件（HTTP {code}）")
    if status == "http_error":
        transient = code is None or code >= 500 or code == 429
        return Failure("http", f"HTTP {code}", transient)
    if status == "robots_disallowed":
        return Failure("robots", "robots.txt 不允许下载")
    if status == "robots_error":
        return Failure("robots", "robots.txt 暂时取不到", True)
    if status == "unsupported_host":
        return Failure("host", (result.error or "非政府网站")[:300])
    return Failure("network", (result.error or status)[:300], True)


# ---- 对象存储（经云托管开放接口服务，免 access_token；接口已在"微信令牌权限配置"登记） ----


def store_copy(
    client: httpx.Client,
    env: str,
    data: bytes,
    digest: str,
    file_type: str,
    sleep: Callable[[float], None] = clock.sleep,
) -> str:
    path = object_path(digest, file_type)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / Path(path).name
        local.write_bytes(data)
        file_id = _retrying(lambda: upload(client, env, path, local, what="附件副本"), sleep)
    parsed = parse_file_id(file_id)
    if parsed is None or parsed != (env, path):
        raise BackupError("上传后返回的文件 ID 和预期不同，没有使用这个副本")
    return file_id


def _platform_hint(errmsg: object) -> str:
    """把单条 errmsg 收成固定分类。不返回原文，也不接受超长或非字符串。"""
    if type(errmsg) is not str or len(errmsg) > _HINT_TEXT_MAX:
        return "unknown"
    return _PLATFORM_HINTS.get(errmsg.strip(), "unknown")


# 逐项未确认：文件 ID → (唯一整数状态，否则 None；固定提示分类)
Unconfirmed = dict[str, tuple[int | None, str]]


class _DeleteItemFailure(BackupError):
    """本批已解析响应里的未确认项。不用于网络、errcode 或畸形响应。

    只保留状态码和固定提示分类。not_found 文字不是删除成功。
    """

    def __init__(self, items: Unconfirmed) -> None:
        self.items = dict(items)
        message, retryable = _unconfirmed_message(list(self.items.values()))
        super().__init__(message, retryable=retryable)


class DeleteCopiesError(BackupError):
    """清理未全部确认；confirmed 仅供结算已确认成功的引用，不把部分成功写成全部成功。
    unconfirmed 是逐项未确认的明细，只给 settle_missing_copies 核对证据用，不写进报错文字；
    stopped：整体出错提前停下了（后面的批次没发）。"""

    def __init__(
        self,
        error: BackupError,
        confirmed: set[str],
        unconfirmed: Unconfirmed | None = None,
        *,
        stopped: bool = False,
    ) -> None:
        super().__init__(str(error), retryable=error.retryable)
        self.confirmed = set(confirmed)
        self.unconfirmed = dict(unconfirmed or {})
        self.stopped = stopped


def _unconfirmed_message(items: list[tuple[int | None, str]]) -> tuple[str, bool]:
    """固定安全摘要：个数、整数状态、提示分类。不含原文、文件 ID 或 URL。"""
    failed = [code for code, _hint in items]
    hint_counts = Counter(hint for _code, hint in items)
    shown = ",".join(str(code) for code in sorted({code for code in failed if code is not None}))
    if None in failed:
        shown += ",漏项或无效响应"
    categories = " ".join(f"{name}={hint_counts[name]}" for name in _HINT_NAMES)
    message = (
        f"附件副本删除未确认：{len(failed)} 个（状态 {shown.lstrip(',')}）"
        f"平台提示分类（仅文本提示，不是删除结果）{categories}"
    )
    retryable = bool(failed) and all(type(code) is int and code in _BUSY_CODES for code in failed)
    return message, retryable


def _delete_error(unconfirmed: Unconfirmed, top: BackupError | None) -> BackupError:
    """各批逐项失败加总（不丢较早的一批），再接上让本轮停下的整体错误。"""
    if not unconfirmed:
        if top is None:
            raise AssertionError("不会走到这里")
        return top
    message, retryable = _unconfirmed_message(list(unconfirmed.values()))
    if top is None:
        return BackupError(message, retryable=retryable)
    return BackupError(f"{message}；{top}", retryable=retryable and top.retryable)


def _order_salt(now: datetime | None) -> str:
    return now.isoformat() if now else ""


def delete_copies(
    client: httpx.Client,
    env: str,
    file_ids: list[str],
    *,
    sleep: Callable[[float], None] = clock.sleep,
    salt: str = "",
    on_confirmed: Callable[[set[str]], None] | None = None,
) -> set[str]:
    """只删本环境 attachments/ 文件；任何未确认项均失败，保留引用供下轮处理。

    每个待删 ID 恰好一条、且不是唯一整数 status 0：逐项失败。忙码仍重试，
    耗尽后记下摘要并继续后一批。delete_list 不是 list、ID 缺失或重复，以及
    _open_api 的网络、HTTP、errcode 失败或其他异常，都立即停止，不再发后一批。
    结束时若仍有失败，抛 DeleteCopiesError；confirmed 是各批已确认 ID 的并集。
    不把 not_found 文字当成成功。

    每轮最多处理 500 个。salt 非空时先按 salt 打乱顺序再截取（调用方每轮传不同的值）：
    长期删不掉的不会每轮都占住同样的名额，后面的总能轮到。
    on_confirmed：每批结束后把这批确认删掉的 ID 交给调用方当场结算并提交，进程中途退出时
    最多丢一批的结算。它抛的异常（如数据库提交失败）原样往外抛，不再发后一批。
    """
    ids = [i for i in file_ids if (parsed := parse_file_id(i)) and parsed[0] == env]
    if salt:
        ids.sort(key=lambda file_id: sha256(f"{salt}\n{file_id}".encode()).digest())
    ids = ids[:_DELETE_MAX]
    deleted: set[str] = set()
    unconfirmed: Unconfirmed = {}
    for start in range(0, len(ids), _DELETE_BATCH):
        batch = ids[start : start + _DELETE_BATCH]

        def attempt(batch=batch):
            # 已确认成功的对象不重复发送；不依赖平台未核实的 notfound 返回语义。
            pending = [file_id for file_id in batch if file_id not in deleted]
            data = _open_api(client, "/tcb/batchdeletefile", {"env": env, "fileid_list": pending})
            statuses = defaultdict(list)
            hints = defaultdict(list)
            items = data.get("delete_list")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("fileid") in pending:
                        statuses[item["fileid"]].append(item.get("status"))
                        hints[item["fileid"]].append(_platform_hint(item.get("errmsg")))
            failed: Unconfirmed = {}
            # 缺列表、缺 ID 或同一 ID 多条都不可按逐项失败跳过，调用方应马上停止。
            malformed = not isinstance(items, list)
            for file_id in pending:
                values = statuses[file_id]
                if len(values) == 1 and type(values[0]) is int and values[0] == 0:
                    deleted.add(file_id)
                    continue
                if len(values) != 1:
                    malformed = True
                code = values[0] if len(values) == 1 and type(values[0]) is int else None
                # 重复项或漏项没有唯一提示，只记 unknown，不保留任何原文。
                hint = hints[file_id][0] if len(hints[file_id]) == 1 else "unknown"
                failed[file_id] = (code, hint)
            if not failed:
                return
            if malformed:
                message, _retryable = _unconfirmed_message(list(failed.values()))
                raise BackupError(message, retryable=False)
            raise _DeleteItemFailure(failed)

        top: BackupError | None = None
        try:
            # 与备份相同：暂时繁忙或网络错误等待 5、15 秒；永久错误立即上报。
            _retrying(attempt, sleep)
        except _DeleteItemFailure as exc:
            unconfirmed.update(exc.items)
        except BackupError as exc:
            top = exc
        except Exception as exc:  # noqa: BLE001 - 已确认删掉的要带出去结算；报错只写类名
            top = BackupError(f"删除附件副本出错：{exc.__class__.__name__}")
            top.__cause__ = exc
        confirmed = {file_id for file_id in batch if file_id in deleted}
        if on_confirmed is not None and confirmed:
            on_confirmed(confirmed)
        if top is not None:
            error = _delete_error(unconfirmed, top)
            raise DeleteCopiesError(error, deleted, unconfirmed, stopped=True) from top
    if unconfirmed:
        raise DeleteCopiesError(_delete_error(unconfirmed, None), deleted, unconfirmed)
    return deleted


_links: OrderedDict[str, tuple[str, float]] = OrderedDict()
_links_guard = threading.Lock()


def download_link(
    client: httpx.Client, file_id: str, *, now: Callable[[], float] = clock.monotonic
) -> str:
    """附件副本的临时下载链接（只给 api 自己用，不发给小程序）；同一进程里缓存 20 分钟。"""
    parsed = parse_file_id(file_id)
    if parsed is None:
        raise BackupError("不是附件副本的文件 ID")
    with _links_guard:
        cached = _links.get(file_id)
        if cached and cached[1] > now():
            return cached[0]
    data = _open_api(
        client,
        "/tcb/batchdownloadfile",
        {"env": parsed[0], "file_list": [{"fileid": file_id, "max_age": _LINK_MAX_AGE}]},
    )
    items = data.get("file_list") or data.get("download_list") or []
    item = next((i for i in items if i.get("fileid") in (None, file_id)), None)
    url = (item or {}).get("download_url") or ""
    if not item or item.get("status") not in (None, 0) or not url.startswith("https://"):
        raise BackupError("没有拿到附件副本的下载链接")
    with _links_guard:
        _links[file_id] = (url, now() + _LINK_TTL)
        _links.move_to_end(file_id)
        while len(_links) > _LINKS_MAX:
            _links.popitem(last=False)
    return url


def forget_link(file_id: str) -> None:
    with _links_guard:
        _links.pop(file_id, None)


_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def read_range(client: httpx.Client, url: str, start: int, end: int, total: int) -> bytes:
    """读 [start, end) 这一段。对象存储支持分段时返回 206；
    不支持（返回 200 整份）就读到这一段为止。"""
    want = end - start
    with client.stream(
        "GET", url, headers={"Range": f"bytes={start}-{end - 1}"}, timeout=10
    ) as res:
        if res.status_code == 206:
            match = _CONTENT_RANGE.match(res.headers.get("content-range", ""))
            if not match or int(match.group(1)) != start or int(match.group(3)) != total:
                raise BackupError("附件副本的分段和预期不同")
            skip, limit = 0, want
        elif res.status_code == 200:
            skip, limit = start, end
        else:
            raise BackupError(
                f"读取附件副本失败（HTTP {res.status_code}）", retryable=res.status_code == 403
            )
        buf = bytearray()
        for chunk in res.iter_bytes():
            buf += chunk
            if len(buf) >= limit:
                break
    data = bytes(buf[skip:limit])
    if len(data) != want:
        raise BackupError("附件副本读取不完整")
    return data


def read_copy(client: httpx.Client, file_id: str, start: int, end: int, total: int) -> bytes:
    """读副本的一段；临时链接过期（403）时换一个新链接再读一次。"""
    try:
        return read_range(client, download_link(client, file_id), start, end, total)
    except BackupError as exc:
        if not exc.retryable:
            raise
        forget_link(file_id)
        return read_range(client, download_link(client, file_id), start, end, total)


# ---- 定时任务 ----


@dataclass
class CacheReport:
    added: int = 0
    checked: int = 0
    stored: int = 0
    unchanged: int = 0
    failed: Counter = field(default_factory=Counter)
    removed: int = 0
    bytes: int = 0
    deferred: int = 0
    storage_error: str | None = None
    # 本轮先重查的旧规则版本副本
    recheck: RecheckReport | None = None
    # 另一个附件任务（上一轮还没跑完，或有人在手动重查）正在跑，本轮跳过
    skipped: bool = False


def _since(now: datetime, days: int = WINDOW_DAYS) -> tuple[date, datetime]:
    day = now.astimezone(BEIJING).date() - timedelta(days=days)
    return day, datetime.combine(day, time(), tzinfo=BEIJING)


def _recent(since: date, since_at: datetime):
    return or_(
        Document.publish_date >= since,
        and_(Document.publish_date.is_(None), Document.first_seen_at >= since_at),
    )


def candidates(session: Session, now: datetime) -> dict[str, str]:
    """要有副本的附件：原站链接 → 题录里的扩展名，按文件发布日期从新到旧。"""
    since, since_at = _since(now)
    apply_since, apply_since_at = _since(now, APPLICATION_WINDOW_DAYS)
    kind = func.coalesce(Document.kind_override, Document.rule_kind)
    rows = session.execute(
        select(DocumentAttachment.url, DocumentAttachment.extension)
        .join(Document, Document.id == DocumentAttachment.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Source.rights_level == 1,
            available_condition(),
            or_(
                _recent(since, since_at),
                and_(kind == "申报通知", _recent(apply_since, apply_since_at)),
            ),
            kind.in_(COPY_KINDS),
        )
        .order_by(Document.publish_date.desc(), Document.id.desc(), DocumentAttachment.position)
    )
    found: dict[str, str] = {}
    for url, extension in rows:
        ext = (extension or "").lower()
        if ext in PREVIEW_TYPES and _government_url(url) and url not in found:
            found[url] = ext
    return found


def _retire(row: AttachmentCopy, file_id: str | None) -> None:
    if file_id:
        row.stale_file_ids = sorted({*(row.stale_file_ids or []), file_id})


def _record_failure(row: AttachmentCopy, failure: Failure, now: datetime) -> None:
    row.attempts += 1
    row.checked_at = now
    row.failure = failure.code
    row.failure_detail = failure.detail[:300]
    retry = failure.transient and row.attempts <= len(_RETRY_DELAYS)
    row.next_check_at = now + (_RETRY_DELAYS[row.attempts - 1] if retry else RECHECK)
    if row.status == "ready" and failure.transient:
        return  # 临时问题：已有副本照常可用
    # 原网站删了、变成网页、超过 20MB、查出个人信息等：不再提供旧副本（以原网站为准）
    _retire(row, row.file_id)
    row.status = "failed"
    row.file_id = row.file_type = row.sha256 = None
    row.size = None


def _check_one(
    session: Session,
    row: AttachmentCopy,
    fetcher,
    client: httpx.Client,
    env: str,
    now: datetime,
    report: CacheReport,
    sleep: Callable[[float], None],
) -> None:
    result = fetcher.fetch(row.url)
    report.checked += 1
    if result.status != "ok":
        outcome: Failure | str = fetch_failure(result)
    elif not _government_url(result.final_url or row.url):
        outcome = Failure("host", "跳转到了非政府网站")
    elif len(result.content) > MAX_BYTES:
        outcome = Failure("too_large", "附件超过 20MB")
    else:
        report.bytes += len(result.content)
        outcome = check_content(result.content, row.declared_type)
    if isinstance(outcome, Failure):
        _record_failure(row, outcome, now)
        report.failed[outcome.code] += 1
        session.commit()
        return
    file_type, data = outcome, result.content
    digest = sha256(data).hexdigest()
    if not (row.status == "ready" and row.sha256 == digest and row.file_id):
        # 内容相同的副本已经存过（别的链接）就共用，不重复上传
        same = session.scalar(
            select(AttachmentCopy.file_id)
            .where(
                AttachmentCopy.sha256 == digest,
                AttachmentCopy.file_type == file_type,
                AttachmentCopy.status == "ready",
                AttachmentCopy.file_id.is_not(None),
            )
            .limit(1)
        )
        # 上传失败抛 BackupError，这一行不改，由调用方停下这一轮
        file_id = same or store_copy(client, env, data, digest, file_type, sleep)
        if row.file_id != file_id:
            _retire(row, row.file_id)
        row.status = "ready"
        row.file_id, row.file_type, row.sha256, row.size = file_id, file_type, digest, len(data)
        row.fetched_at = now
        report.stored += 1
    else:
        report.unchanged += 1
    row.attempts = 0
    row.failure = row.failure_detail = None
    row.checked_at = now
    row.next_check_at = now + RECHECK
    row.rules_version = RULES_VERSION  # 刚按现在的规则查过
    session.commit()


# 附件定时任务（缓存 + 重查）和手动重查共用的 MySQL 命名锁：同一时间只有一个在改副本、删文件
_LOCK_NAME = "youju_attachment_copies"


@contextmanager
def _exclusive(session: Session) -> Iterator[bool]:
    """拿附件任务锁（不等待）：拿到给 True，另一个附件任务正在跑给 False。
    锁挂在单独的一个连接上，任务结束时释放；连接断了 MySQL 也会自动释放。"""
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine.dialect.name != "mysql":
        yield True
        return
    with engine.connect() as conn:
        got = conn.execute(text("SELECT GET_LOCK(:k, 0)"), {"k": _LOCK_NAME}).scalar() == 1
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": _LOCK_NAME})


def _referenced(session: Session, file_ids: set[str], live: set[str] | None) -> set[str]:
    """删除前在同一事务里再查一次（锁住这些行）：还有附件记录引用着的对象不删。
    live 给定时只算还要提供的附件（超过类型对应保留期、下架的正要一起删掉，不算）。"""
    if not file_ids:
        return set()
    rows = session.execute(
        select(AttachmentCopy.url_hash, AttachmentCopy.file_id)
        .where(AttachmentCopy.file_id.in_(sorted(file_ids)))
        .with_for_update()
    ).all()
    return {file_id for key, file_id in rows if live is None or key in live}


class _Settlement:
    """清理后改记录：已确认删掉的、不用删（keep：还有附件在用）的文件 ID 从旧副本列表去掉；
    到期记录的当前副本删掉了就清空，没有要等的了就删记录。每次调用当场提交。

    delete_copies 每批确认后调用一次：进程中途退出、提交失败，最多丢一批的结算（那批对象
    已删、引用还在，下轮再删只会得到"不存在"类提示，要靠 settle_missing_copies 核对后结算）。
    提交会放掉 _referenced 加的行锁；这张表只有拿着附件任务锁的任务会写，api 只读。"""

    def __init__(
        self,
        session: Session,
        rows: list[AttachmentCopy],
        expired: list[AttachmentCopy],
        keep: set[str],
    ) -> None:
        self.session = session
        self.rows = list(rows)
        self.expired = {id(row) for row in expired}
        self.keep = set(keep)
        self.deleted: set[str] = set()
        self._gone: set[int] = set()

    def __call__(self, confirmed: set[str]) -> None:
        deleted = self.deleted | confirmed
        settled = deleted | self.keep
        for row in self.rows:
            if id(row) in self._gone:
                continue
            if row.stale_file_ids:
                remaining = [i for i in row.stale_file_ids if i not in settled]
                if len(remaining) != len(row.stale_file_ids):
                    row.stale_file_ids = remaining or None
            if id(row) not in self.expired:
                continue
            if row.file_id in deleted:
                row.file_id = None  # 仍有待清理旧副本时保留记录，但不重复请求已确认删除的当前副本。
            if (not row.file_id or row.file_id in settled) and not row.stale_file_ids:
                self.session.delete(row)
                self._gone.add(id(row))
        self.session.commit()
        self.deleted = deleted


def _cleanup(
    session: Session,
    client: httpx.Client,
    env: str,
    live: set[str],
    report: CacheReport,
    now: datetime | None = None,
) -> None:
    """删掉不再需要的副本：超过类型对应保留期、下架、附件题录已变的，以及换下来的旧副本。
    对象存储确认删掉后才改记录（每批确认后当场结算提交）；别的记录还在用的同一对象不删。"""
    rows = session.scalars(select(AttachmentCopy)).all()
    expired = [r for r in rows if r.url_hash not in live]
    in_use = {r.file_id for r in rows if r.url_hash in live and r.file_id}
    wanted = {r.file_id for r in expired if r.file_id}
    for row in rows:
        wanted.update(row.stale_file_ids or [])
    wanted -= in_use
    kept = _referenced(session, wanted, live)
    wanted -= kept
    settle = _Settlement(session, list(rows), expired, in_use | kept)
    failure = None
    try:
        if wanted:
            delete_copies(client, env, sorted(wanted), salt=_order_salt(now), on_confirmed=settle)
    except DeleteCopiesError as exc:
        failure = exc
    # 不用删的旧副本、没有副本要删的到期记录，以及（出错时）已结算部分之外的改动
    settle(set())
    report.removed += len(settle.deleted)
    if failure is not None:
        raise failure


def cache_attachments(
    session: Session,
    env: str,
    *,
    client: httpx.Client | None = None,
    fetcher=None,
    now: datetime | None = None,
    limit: int = 100,
    max_bytes: int = RUN_BYTES,
    retry_failed: bool = False,
    recheck_limit: int = RECHECK_PER_RUN,
    sleep: Callable[[float], None] = clock.sleep,
) -> CacheReport:
    """先按新规则重查旧规则版本的副本（从对象存储读，最多 recheck_limit 个），再登记新附件、
    下载到期的（新的优先，再是失败重试、到期复查），最后清理不再需要的副本（含重查不合格的）。
    对象存储出错时停下这一轮，原因写在 report.storage_error（命令按失败退出、由 job.sh 提醒）。
    和手动重查共用一把锁：另一个附件任务正在跑就跳过本轮（report.skipped）。"""
    if not 1 <= limit <= 500:
        raise ValueError("每轮下载个数要在 1 到 500 之间")
    with _exclusive(session) as got:
        if not got:
            return CacheReport(skipped=True)
        return _cache_run(
            session,
            env,
            client=client,
            fetcher=fetcher,
            now=now,
            limit=limit,
            max_bytes=max_bytes,
            retry_failed=retry_failed,
            recheck_limit=recheck_limit,
            sleep=sleep,
        )


def _cache_run(
    session: Session,
    env: str,
    *,
    client: httpx.Client | None,
    fetcher,
    now: datetime | None,
    limit: int,
    max_bytes: int,
    retry_failed: bool,
    recheck_limit: int,
    sleep: Callable[[float], None],
) -> CacheReport:
    now = now or datetime.now(UTC)
    report = CacheReport()
    wanted = candidates(session, now)
    order = {url_hash(url): i for i, url in enumerate(wanted)}
    rows = {r.url_hash: r for r in session.scalars(select(AttachmentCopy))}
    for url, ext in wanted.items():
        key = url_hash(url)
        if key not in rows:
            rows[key] = AttachmentCopy(url_hash=key, url=url, declared_type=ext, status="pending")
            session.add(rows[key])
            report.added += 1
    session.commit()
    rank = {"pending": 0, "failed": 1, "ready": 2}
    due = sorted(
        (
            r
            for r in rows.values()
            if r.url_hash in order
            and (
                r.next_check_at is None
                or r.next_check_at <= now
                or (retry_failed and r.status == "failed")
            )
        ),
        key=lambda r: (rank[r.status], order[r.url_hash]),
    )
    owns_client, owns_fetcher = client is None, fetcher is None
    client = client or httpx.Client(trust_env=False)
    if fetcher is None:
        settings = get_settings()
        fetcher = PoliteFetcher(
            settings.user_agent,
            settings.request_timeout,
            settings.per_host_interval,
            url_allowed=_government_url,
            decode_text=False,
        )
    try:
        report.recheck = _recheck(
            session, env, client=client, now=now, outdated_only=True, limit=recheck_limit
        )
        processed = 0
        for row in due[:limit]:
            if report.bytes >= max_bytes:
                break
            try:
                _check_one(session, row, fetcher, client, env, now, report, sleep)
            except BackupError as exc:
                session.rollback()
                report.storage_error = f"上传附件副本失败：{exc}"
                break
            processed += 1
        report.deferred = len(due) - processed
        try:
            _cleanup(session, client, env, set(order), report, now)
        except BackupError as exc:
            session.rollback()
            # 上传已经出错时接在后面，不能盖掉删除的失败
            failure = f"删除附件副本失败：{exc}"
            report.storage_error = (
                f"{report.storage_error}；{failure}" if report.storage_error else failure
            )
    finally:
        if owns_client:
            client.close()
        if owns_fetcher:
            fetcher.close()
    return report


# ---- 按现在的规则重新检查已存的副本（规则收紧后跑一次：youju recheck-attachments） ----


@dataclass
class RecheckReport:
    # 检查了几个副本（对象存储里的文件；内容相同的几条附件共用一个）
    checked: int = 0
    kept: int = 0
    # 不合格的副本个数（按原因），和因此改成"不提供副本"的附件条数
    dropped: Counter = field(default_factory=Counter)
    rows: int = 0
    removed: int = 0
    # 读不到的副本：这次不动，下次再查
    unreadable: int = 0
    storage_error: str | None = None
    # 另一个附件任务正在跑，这次没有查
    skipped: bool = False


def _drop_stale(
    session: Session, client: httpx.Client, env: str, now: datetime | None = None
) -> int:
    """从对象存储删掉换下来、已经没有附件在用的副本，返回删掉几个（每批确认后当场结算提交）。
    只删本环境 attachments/ 下、文件 ID 校验通过的（delete_copies），备份等别的文件不碰。"""
    rows = session.scalars(select(AttachmentCopy)).all()
    in_use = {r.file_id for r in rows if r.file_id}
    wanted = {i for r in rows for i in (r.stale_file_ids or [])} - in_use
    kept = _referenced(session, wanted, None)
    wanted -= kept
    settle = _Settlement(session, list(rows), [], in_use | kept)
    failure = None
    try:
        if wanted:
            delete_copies(client, env, sorted(wanted), salt=_order_salt(now), on_confirmed=settle)
    except DeleteCopiesError as exc:
        failure = exc
    settle(set())
    if failure is not None:
        raise failure
    return len(settle.deleted)


def recheck_copies(
    session: Session,
    env: str,
    *,
    client: httpx.Client | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RecheckReport:
    """手动重查全部已存的副本（youju recheck-attachments），做法见 _recheck。和附件定时任务共用
    一把锁：定时任务正在跑就不查（report.skipped）。"""
    with _exclusive(session) as got:
        if not got:
            return RecheckReport(skipped=True)
        return _recheck(session, env, client=client, dry_run=dry_run, now=now)


def _recheck(
    session: Session,
    env: str,
    *,
    client: httpx.Client | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    outdated_only: bool = False,
    limit: int | None = None,
) -> RecheckReport:
    """从对象存储读出已存的副本，核对 SHA-256 后按现在的规则（格式、个人信息、图片）重新检查：
    合格的记为当前规则版本（恢复提供）；不合格的改成"不提供副本"（原因写进表里，小程序说明原因、
    复制原文链接），换下来的文件从对象存储删掉。只读、只删本环境 attachments/ 下文件 ID 校验通过的
    副本。读不到的这次不动（仍是旧版本就仍不提供），计入 unreadable。

    outdated_only：只查不是当前规则版本的（定时任务用，最多 limit 个文件；删除留给本轮的清理）。
    手动命令查全部，并当场删除。dry_run 只检查、报告，不改表、不删文件。
    按上次重查的时间排（没查过的先），每次查都记下时间：读不到的排到后面，不会一直堵在前面。"""
    now = now or datetime.now(UTC)
    report = RecheckReport()
    stmt = select(AttachmentCopy).where(AttachmentCopy.status == "ready")
    if outdated_only:
        stmt = stmt.where(AttachmentCopy.rules_version != RULES_VERSION)
    groups: dict[str, list[AttachmentCopy]] = defaultdict(list)
    order = (
        AttachmentCopy.rechecked_at.is_(None).desc(),
        AttachmentCopy.rechecked_at,
        AttachmentCopy.id,
    )
    for row in session.scalars(stmt.order_by(*order)):
        parsed = parse_file_id(row.file_id)
        if _has_copy(row) and parsed is not None and parsed[0] == env:
            groups[row.file_id].append(row)
    picked = list(groups.items())[:limit] if limit is not None else list(groups.items())
    owns_client = client is None
    client = client or httpx.Client(trust_env=False)
    try:
        for file_id, rows in picked:
            first = rows[0]
            if not dry_run:
                for row in rows:
                    row.rechecked_at = now
                session.commit()
            try:
                data = read_copy(client, file_id, 0, first.size, first.size)
            except (BackupError, httpx.HTTPError) as exc:
                log.warning("附件副本读不到，这次不检查：%s（%s）", file_id, exc)
                report.unreadable += 1
                continue
            report.checked += 1
            if any(sha256(data).hexdigest() != row.sha256 for row in rows):
                outcome: Failure | str = Failure("mismatch", "对象存储里的副本和记录的内容不一致")
            else:
                outcome = check_content(data, first.file_type)
            if not isinstance(outcome, Failure):
                report.kept += 1
                if not dry_run:
                    for row in rows:
                        row.rules_version = RULES_VERSION
                    session.commit()
                continue
            report.dropped[outcome.code] += 1
            report.rows += len(rows)
            log.info("附件副本按新规则不再提供：%s（%s）", file_id, outcome.detail)
            if dry_run:
                continue
            for row in rows:
                _record_failure(row, outcome, now)
            session.commit()
        if not dry_run and not outdated_only:
            try:
                report.removed = _drop_stale(session, client, env, now)
            except DeleteCopiesError as exc:
                # 部分删掉的已经结算提交，照实报数，再按失败上报
                report.removed = len(exc.confirmed)
                raise
    except BackupError as exc:
        session.rollback()
        report.storage_error = f"删除附件副本失败：{exc}"
    finally:
        if owns_client:
            client.close()
    return report


# ---- 删不掉、对象存储里也没有的副本：凭存储桶清单核对后结算（youju settle-missing-copies） ----
#
# 删除接口对不在的对象返回状态 1 和"不存在"一类提示，但官方没把它定义成删除成功（权限、
# 路径不对也会这样返回），所以定时清理从不凭它结算。对象早已删掉、引用没结算的（旧版代码删了
# 到期记录的当前副本却没清空、进程在删除和提交之间退出等）就会每轮报警、永远删不掉。
# 这里要第二份独立证据：运维在本机用 wxcloud storage:list 取对象存储 attachments/ 的完整清单
# （backend/scripts/attachment_listing.py 生成），同一存储桶、24 小时内取得、清单里没有，
# 而且这次再删仍是整数状态 1 + 不存在提示，才结算；结算前先把明细写进对象存储
# ops/attachment-settlements/ 留档，留档失败就不结算。

LISTING_MAX_AGE = timedelta(hours=24)
# 清单里每个对象只带内容 SHA-256 的前 4 个字节（要能粘贴进容器终端）：撞上了只会把不在的
# 当成"可能还在"多保留，不会把还在的当成不在
_HEAD_BYTES = 4
_BUCKET = re.compile(r"[0-9a-z][0-9a-z-]{2,80}")
_FILE_BUCKET = re.compile(r"cloud://[^./]+\.([^/]+)/")
AUDIT_PREFIX = "ops/attachment-settlements/"


def _bucket_of(file_id: str | None) -> str | None:
    match = _FILE_BUCKET.match(file_id or "")
    return match.group(1) if match else None


@dataclass(frozen=True)
class Listing:
    """对象存储 attachments/ 的完整清单（只有每个对象内容 SHA-256 的前 4 字节）。"""

    bucket: str
    listed_at: datetime
    count: int
    digest: str
    heads: frozenset[bytes]

    def has(self, file_id: str) -> bool:
        """在清单里，或者前缀撞上了（都按"可能还在"算）；认不出的文件 ID 也按还在算。"""
        parsed = parse_file_id(file_id)
        if parsed is None:
            return True
        # _FILE_ID 保证路径是 attachments/ 加 64 位小写十六进制，fromhex 不会出错
        return bytes.fromhex(parsed[1][len(PREFIX) :][: _HEAD_BYTES * 2]) in self.heads


def parse_listing(text: str, now: datetime) -> Listing:
    """第一行 JSON（v、bucket、prefix、listed_at、count、sha256），后面几行是 base64：
    每个对象 SHA-256 的前 4 字节，从小到大排好接在一起。任何一项对不上就整份拒绝。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    try:
        head = json.loads(lines[0]) if lines else None
        payload = base64.b64decode("".join(lines[1:]), validate=True)
        listed_at = datetime.fromisoformat(head["listed_at"])
    except (ValueError, TypeError, KeyError) as exc:  # binascii.Error 是 ValueError
        raise ValueError("清单格式不对（可能没有复制完整）") from exc
    if not isinstance(head, dict) or head.get("v") != 1 or head.get("prefix") != PREFIX:
        raise ValueError("清单的格式版本或前缀不对")
    bucket, count = head.get("bucket"), head.get("count")
    if not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket):
        raise ValueError("清单的存储桶名不对")
    if listed_at.tzinfo is None:
        raise ValueError("清单的取得时间要带时区")
    if not now - LISTING_MAX_AGE <= listed_at <= now + timedelta(minutes=10):
        raise ValueError("清单不是最近 24 小时内取得的，请重新取")
    if not payload or len(payload) % _HEAD_BYTES or type(count) is not int:
        raise ValueError("清单是空的或长度不对")
    heads = [payload[i : i + _HEAD_BYTES] for i in range(0, len(payload), _HEAD_BYTES)]
    if count != len(heads) or any(a >= b for a, b in zip(heads, heads[1:], strict=False)):
        raise ValueError("清单条数对不上或没有排好序")
    digest = sha256(payload).hexdigest()
    if head.get("sha256") != digest:
        raise ValueError("清单校验和对不上（可能没有复制完整）")
    return Listing(bucket, listed_at, count, digest, frozenset(heads))


@dataclass
class MissingReport:
    # 和定时清理同一口径要删的副本（本环境的），其中清单里有、没有、存储桶和清单不同的个数
    wanted: int = 0
    listed: int = 0
    unlisted: int = 0
    bucket_mismatch: int = 0
    # 还在提供、但清单里没有的副本（打不开）；--apply 时改回待下载的个数
    live_missing: int = 0
    live_reset: int = 0
    # 以下只在 --apply 时有：这次确认删掉（照常结算）、未确认、未确认里清单有（平台提示和
    # 清单矛盾）、不是"状态 1 + 不存在提示"的、证据齐全按已不存在结算的
    deleted: int = 0
    unconfirmed: int = 0
    listed_unconfirmed: int = 0
    other_status: int = 0
    settled: int = 0
    audit: str | None = None
    applied: bool = False
    skipped: bool = False
    error: str | None = None


def settle_missing_copies(
    session: Session,
    env: str,
    listing: Listing,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    apply: bool = False,
    sleep: Callable[[float], None] = clock.sleep,
) -> MissingReport:
    """不加 apply 只读：按清单统计要删的副本、还在提供的副本各有几个不在对象存储里，不删不改。
    apply：先照常删（确认删掉的照常结算），删不掉的逐项核对证据，齐全的先留档再结算；还在提供、
    清单里没有、清单之前存的副本改回待下载（下轮定时任务重新下载）。和附件任务共用一把锁。"""
    now = now or datetime.now(UTC)
    with _exclusive(session) as got:
        if not got:
            return MissingReport(skipped=True)
        owns_client = client is None
        client = client or httpx.Client(trust_env=False)
        try:
            return _settle_missing(session, env, listing, client, now, apply, sleep)
        finally:
            if owns_client:
                client.close()


def _settle_missing(
    session: Session,
    env: str,
    listing: Listing,
    client: httpx.Client,
    now: datetime,
    apply: bool,
    sleep: Callable[[float], None],
) -> MissingReport:
    report = MissingReport(applied=apply)
    live = {url_hash(url) for url in candidates(session, now)}
    rows = session.scalars(select(AttachmentCopy)).all()
    expired = [r for r in rows if r.url_hash not in live]
    in_use = {r.file_id for r in rows if r.url_hash in live and r.file_id}
    wanted = {r.file_id for r in expired if r.file_id}
    for row in rows:
        wanted.update(row.stale_file_ids or [])
    wanted = {i for i in wanted - in_use if (parsed := parse_file_id(i)) and parsed[0] == env}
    # 和 _cleanup 一样再查一次还有附件在用的（只读时也扣掉，统计和 --apply 实际处理的一致）
    kept = _referenced(session, wanted, live)
    wanted -= kept
    same_bucket = {i for i in wanted if _bucket_of(i) == listing.bucket}
    report.wanted, report.bucket_mismatch = len(wanted), len(wanted) - len(same_bucket)
    report.listed = sum(1 for i in same_bucket if listing.has(i))
    report.unlisted = len(same_bucket) - report.listed
    serving = [
        r
        for r in rows
        if r.url_hash in live
        and r.status == "ready"
        and _bucket_of(r.file_id) == listing.bucket
        and parse_file_id(r.file_id) is not None
    ]
    # 清单之后存的副本清单里本来就没有，不算
    dangling = [
        r
        for r in serving
        if not listing.has(r.file_id)
        and r.fetched_at is not None
        and r.fetched_at < listing.listed_at
    ]
    report.live_missing = len(dangling)
    if serving and not any(listing.has(r.file_id) for r in serving):
        session.rollback()
        report.error = "还在提供的副本一个都不在清单里，清单可能取错了存储桶或前缀：没有改动"
        return report
    if not apply:
        session.rollback()
        return report
    settle = _Settlement(session, list(rows), expired, in_use | kept)
    try:
        failure: DeleteCopiesError | None = None
        try:
            if wanted:
                delete_copies(
                    client,
                    env,
                    sorted(wanted),
                    sleep=sleep,
                    salt=_order_salt(now),
                    on_confirmed=settle,
                )
        except DeleteCopiesError as exc:
            failure = exc
        report.deleted = len(settle.deleted)
        if failure is not None and failure.stopped:
            settle(set())
            report.error = f"删除附件副本中途出错，这次不按清单结算：{failure}"
            return report
        unconfirmed = failure.unconfirmed if failure is not None else {}
        report.unconfirmed = len(unconfirmed)
        gone: Unconfirmed = {}
        for file_id, (code, hint) in unconfirmed.items():
            if _bucket_of(file_id) != listing.bucket:
                continue  # 已计入 bucket_mismatch
            if listing.has(file_id):
                report.listed_unconfirmed += 1
            elif type(code) is int and code == 1 and hint == "not_found_hint":
                gone[file_id] = (code, hint)
            else:
                report.other_status += 1
        if gone or dangling:
            report.audit = _upload_settlement(client, env, now, listing, gone, dangling, sleep)
            for row in dangling:
                # 对象已不在（清单为证），引用直接去掉，不放进旧副本列表（那样每轮又删不掉）
                row.status = "pending"
                row.file_id = row.file_type = row.sha256 = None
                row.size = None
                row.next_check_at = None
                row.attempts = 0
            settle(set(gone))
            report.settled, report.live_reset = len(gone), len(dangling)
            log.info(
                "附件副本按清单结算已不存在的 %d 个，改回待下载 %d 个，留档 %s",
                len(gone),
                len(dangling),
                report.audit,
            )
        settle(set())
    except BackupError as exc:
        session.rollback()
        report.error = f"按清单结算失败，没有改动：{exc}"
    return report


def _upload_settlement(
    client: httpx.Client,
    env: str,
    now: datetime,
    listing: Listing,
    gone: Unconfirmed,
    dangling: list[AttachmentCopy],
    sleep: Callable[[float], None],
) -> str:
    """结算明细先写进对象存储留档（只有管理员可读的存储桶），返回对象路径。"""
    record = {
        "v": 1,
        "at": now.astimezone(UTC).isoformat(),
        "revision": build_revision(),
        "listing": {
            "bucket": listing.bucket,
            "listed_at": listing.listed_at.astimezone(UTC).isoformat(),
            "count": listing.count,
            "sha256": listing.digest,
        },
        "settled": [
            {"file_id": file_id, "status": code, "hint": hint}
            for file_id, (code, hint) in sorted(gone.items())
        ],
        "reset_to_pending": [
            {"url_hash": row.url_hash, "file_id": row.file_id} for row in dangling
        ],
    }
    path = f"{AUDIT_PREFIX}{now.astimezone(UTC):%Y%m%dT%H%M%S}Z.json"
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "settlement.json"
        local.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        _retrying(lambda: upload(client, env, path, local, what="结算留档"), sleep)
    return path


# ---- 小程序接口用 ----

# 不能打开时的原因代码（小程序据此写说明）
_FAILURE_REASONS = {
    "too_large": "too_large",
    "personal": "personal",
    "unchecked": "scan",
    "image": "image",
}


def _has_copy(row: AttachmentCopy | None) -> bool:
    """表里登记着一份完整的副本（不管是按哪一版规则查的）。"""
    return (
        row is not None
        and row.status == "ready"
        and bool(row.sha256)
        and bool(row.size)
        and row.file_type in PREVIEW_TYPES
        and parse_file_id(row.file_id) is not None
    )


def servable(row: AttachmentCopy | None) -> bool:
    """能提供给小程序：有完整的副本，而且是按现在的规则查过的（旧规则版本的当作还在准备）。"""
    return _has_copy(row) and row.rules_version == RULES_VERSION


def attachment_items(
    session: Session, attachments: list[DocumentAttachment], *, kind: str, recent: bool
) -> list[dict]:
    """详情页的附件列表：能打开的给格式、大小、版本和分段数；不能打开的给原因代码。
    recent：这份文件在类型对应保留期内（超过的不存副本）。"""
    keys = {a.url: url_hash(a.url) for a in attachments}
    rows = (
        {
            r.url_hash: r
            for r in session.scalars(
                select(AttachmentCopy).where(AttachmentCopy.url_hash.in_(set(keys.values())))
            )
        }
        if keys
        else {}
    )
    items = []
    for a in attachments:
        item: dict = {"id": str(a.id), "ext": a.extension.upper(), "name": a.name, "url": a.url}
        row = rows.get(keys[a.url])
        if a.extension.lower() not in PREVIEW_TYPES:
            reason = "format"
        elif not _government_url(a.url):
            reason = "host"
        elif kind not in COPY_KINDS:
            reason = "kind"
        elif servable(row):
            item.update(
                preview=True,
                fileType=row.file_type,
                size=row.size,
                version=row.sha256[:16],
                parts=parts_of(row.size),
                partSize=PART_SIZE,
            )
            items.append(item)
            continue
        elif row is not None and row.status == "failed":
            reason = _FAILURE_REASONS.get(row.failure or "", "failed")
        else:
            reason = "pending" if recent else "old"
        item.update(preview=False, reason=reason)
        items.append(item)
    return items


def is_recent(
    published: date | None, first_seen: datetime | None, now: datetime, *, kind: str = ""
) -> bool:
    since, since_at = _since(now, APPLICATION_WINDOW_DAYS if kind == "申报通知" else WINDOW_DAYS)
    if published is not None:
        return published >= since
    return first_seen is not None and first_seen >= since_at


def servable_copy(session: Session, attachment_id: int) -> AttachmentCopy | None:
    """小程序按附件编号取副本：附件所在文件公开、没下架、是申报通知或政策，副本可用，才给。"""
    att = session.get(DocumentAttachment, attachment_id)
    if att is None or att.extension.lower() not in PREVIEW_TYPES or not _government_url(att.url):
        return None
    public = session.scalar(
        select(Document.id)
        .join(Source, Source.id == Document.source_id)
        .where(Document.id == att.document_id, Source.rights_level == 1, available_condition())
    )
    if public is None:
        return None
    master_id = canonical_ids(session, [att.document_id]).get(att.document_id, att.document_id)
    master = session.get(Document, master_id)
    if master is None or effective_kind(master.title, master.kind_override) not in COPY_KINDS:
        return None
    row = session.scalar(select(AttachmentCopy).where(AttachmentCopy.url_hash == url_hash(att.url)))
    return row if servable(row) else None
