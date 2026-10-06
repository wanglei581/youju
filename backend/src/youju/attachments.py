"""附件副本：通知的附件在小程序里直接打开（打开后右上角菜单可转发、保存），不用复制原文链接到浏览器。

方案（2026-10-05 用本机 curl 取回微信官方文档原文核对后选定，依据见 PR 说明）：
- admin 定时任务（youju cache-attachments）从原网站下载近 90 天文件的附件，校验后存到云托管对象存储
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
  （合规红线要求入库前脱敏，副本是原文件、没法脱敏，查不了或查出来有就不存）。
- 每 7 天重新下载核对一次：内容变了（SHA-256 不同）换新副本、删旧的；原网站删了、变成网页或
  超过 20MB，不再提供旧副本（以原网站为准）；网络等临时问题保留已有副本，6 小时、24 小时后重试。
"""

from __future__ import annotations

import html
import io
import logging
import math
import re
import tempfile
import threading
import time as clock
import zipfile
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from youju.backup import BEIJING, BackupError, _open_api, _retrying, upload
from youju.classify import effective_kind
from youju.config import get_settings
from youju.crawler.detail import has_personal_numbers
from youju.crawler.detail_service import _government_url
from youju.crawler.fetcher import MAX_RESPONSE_BYTES, FetchResult, PoliteFetcher
from youju.dedup import canonical_ids
from youju.models import AttachmentCopy, Document, DocumentAttachment, Source
from youju.takedown import available_condition

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
MAX_BYTES = MAX_RESPONSE_BYTES  # 20MB，抓取器超过就停止下载
# 每段大小：callContainer 返回包上限 1000 KiB，留出余量
PART_SIZE = 900 * 1024
MAX_PARTS = math.ceil(MAX_BYTES / PART_SIZE)
RECHECK = timedelta(days=7)
_RETRY_DELAYS = (timedelta(hours=6), timedelta(hours=24))
PREFIX = "attachments/"
# 附件副本的文件 ID：cloud://环境ID.存储桶ID/attachments/<SHA-256>.<格式>
# （cloudID 的组成见云托管《对象存储》文档）
_FILE_ID = re.compile(
    r"cloud://([^./]+)\.[^/]+/(attachments/[0-9a-f]{64}\.(?:docx?|xlsx?|pptx?|pdf))"
)
_DELETE_BATCH = 50
_DELETE_MAX = 500
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


def _pdf_text(data: bytes) -> str | None:
    import pdfplumber

    texts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if len(pdf.pages) > _PDF_PAGES:
            return None
        for page in pdf.pages:
            text = page.extract_text() or ""
            # 某一页几乎没有文字但有图片：多半是扫描页，查不了
            scanned = len(text.strip()) < 10 and bool(page.images)
            page.close()  # 释放这一页解析出的对象，大文件不至于占满内存
            if scanned:
                return None
            texts.append(text)
    joined = "\n".join(texts)
    return joined if joined.strip() else None


def _ooxml_text(data: bytes) -> str | None:
    parts: list[str] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if not info.filename.endswith(".xml"):
                continue
            with zf.open(info) as fh:
                raw = fh.read(_TEXT_MAX - total + 1)
            total += len(raw)
            if total > _TEXT_MAX:
                return None
            xml = _TAGS.sub("", _BREAK_TAGS.sub("\n", raw.decode("utf-8", "ignore")))
            parts.append(html.unescape(xml))
    return "\n".join(parts)


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


def text_of(data: bytes, file_type: str) -> str | None:
    """附件里的文字，用来查个人信息；取不出（扫描件、损坏、太大）返回 None。"""
    try:
        if file_type == "pdf":
            return _pdf_text(data)
        if file_type in ("docx", "xlsx", "pptx"):
            return _ooxml_text(data)
        if file_type == "xls":
            return _xls_text(data)
        return _binary_text(data)
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
    text = text_of(data, detected)
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


def delete_copies(client: httpx.Client, env: str, file_ids: list[str]) -> set[str]:
    """从对象存储删掉附件副本，返回确认删掉的。
    只删本环境 attachments/ 下的文件，别的（如备份）一律不碰。"""
    ids = [i for i in file_ids if (parsed := parse_file_id(i)) and parsed[0] == env][:_DELETE_MAX]
    deleted: set[str] = set()
    for start in range(0, len(ids), _DELETE_BATCH):
        batch = ids[start : start + _DELETE_BATCH]
        data = _open_api(client, "/tcb/batchdeletefile", {"env": env, "fileid_list": batch})
        for item in data.get("delete_list", []):
            if item.get("status") == 0 and item.get("fileid") in batch:
                deleted.add(item["fileid"])
            else:
                log.info("附件副本没删掉：%s（%s）", item.get("fileid"), item.get("errmsg"))
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


def _since(now: datetime) -> tuple[date, datetime]:
    day = now.astimezone(BEIJING).date() - timedelta(days=WINDOW_DAYS)
    return day, datetime.combine(day, time(), tzinfo=BEIJING)


def _recent(since: date, since_at: datetime):
    return or_(
        Document.publish_date >= since,
        and_(Document.publish_date.is_(None), Document.first_seen_at >= since_at),
    )


def candidates(session: Session, now: datetime) -> dict[str, str]:
    """要有副本的附件：原站链接 → 题录里的扩展名，按文件发布日期从新到旧。"""
    since, since_at = _since(now)
    rows = session.execute(
        select(DocumentAttachment.url, DocumentAttachment.extension)
        .join(Document, Document.id == DocumentAttachment.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Source.rights_level == 1,
            available_condition(),
            _recent(since, since_at),
            func.coalesce(Document.kind_override, Document.rule_kind).in_(COPY_KINDS),
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
    session.commit()


def _cleanup(
    session: Session, client: httpx.Client, env: str, live: set[str], report: CacheReport
) -> None:
    """删掉不再需要的副本：超过 90 天、下架、附件题录已变的，以及换下来的旧副本。
    对象存储确认删掉后才删记录；别的记录还在用的同一对象不删。"""
    rows = session.scalars(select(AttachmentCopy)).all()
    expired = [r for r in rows if r.url_hash not in live]
    in_use = {r.file_id for r in rows if r.url_hash in live and r.file_id}
    wanted = {r.file_id for r in expired if r.file_id}
    for row in rows:
        wanted.update(row.stale_file_ids or [])
    wanted -= in_use
    deleted = delete_copies(client, env, sorted(wanted)) if wanted else set()
    report.removed += len(deleted)
    settled = deleted | in_use
    for row in rows:
        if row.stale_file_ids:
            row.stale_file_ids = [i for i in row.stale_file_ids if i not in settled] or None
    for row in expired:
        if (not row.file_id or row.file_id in settled) and not row.stale_file_ids:
            session.delete(row)
    session.commit()


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
    sleep: Callable[[float], None] = clock.sleep,
) -> CacheReport:
    """登记新附件、下载到期的（新的优先，再是失败重试、到期复查），最后清理不再需要的副本。
    对象存储出错时停下这一轮，原因写在 report.storage_error（命令按失败退出、由 job.sh 提醒）。"""
    if not 1 <= limit <= 500:
        raise ValueError("每轮下载个数要在 1 到 500 之间")
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
            _cleanup(session, client, env, set(order), report)
        except BackupError as exc:
            session.rollback()
            report.storage_error = report.storage_error or f"删除附件副本失败：{exc}"
    finally:
        if owns_client:
            client.close()
        if owns_fetcher:
            fetcher.close()
    return report


# ---- 小程序接口用 ----

# 不能打开时的原因代码（小程序据此写说明）
_FAILURE_REASONS = {"too_large": "too_large", "personal": "personal", "unchecked": "scan"}


def servable(row: AttachmentCopy | None) -> bool:
    return (
        row is not None
        and row.status == "ready"
        and bool(row.sha256)
        and bool(row.size)
        and row.file_type in PREVIEW_TYPES
        and parse_file_id(row.file_id) is not None
    )


def attachment_items(
    session: Session, attachments: list[DocumentAttachment], *, kind: str, recent: bool
) -> list[dict]:
    """详情页的附件列表：能打开的给格式、大小、版本和分段数；不能打开的给原因代码。
    recent：这份文件在 90 天内（超过的不存副本）。"""
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


def is_recent(published: date | None, first_seen: datetime | None, now: datetime) -> bool:
    since, since_at = _since(now)
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
