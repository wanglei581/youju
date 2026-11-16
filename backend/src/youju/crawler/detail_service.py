"""按网站抓取详情，复用列表抓取器的 robots、限速与标识。"""

import fcntl
import json
import logging
import os
import queue
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from youju import pdf_sandbox
from youju.categories import doc_category
from youju.classify import doc_kind
from youju.config import get_settings
from youju.crawler.detail import (
    DetailResult,
    parse_detail,
    redact_sensitive,
    search_throttle_site,
)
from youju.crawler.fetcher import (
    SITE_PAUSED,
    FetchResult,
    PoliteFetcher,
    SharedLimitError,
    bare_page,
    decode_html,
    html_gate,
    robots_gate,
    text_gate,
)
from youju.crawler.pdf import extract_pdf_text
from youju.crawler.titles import clean_title, completed_title, is_truncated
from youju.dbtypes import BODY_TEXT_MAX_CHARS
from youju.dedup import reconcile_documents
from youju.issuer import normalize_issuer
from youju.models import Document, DocumentAttachment, DocumentDetail, Source
from youju.title_tags import INDUSTRY_RULES, SUPPORT_RULES

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetailFetchSummary:
    document_id: int
    status: str
    failure_reason: str | None = None


class DetailFetchResults(list[DetailFetchSummary]):
    """保持列表接口，同时提供本次停取的网站及原因。"""

    def __init__(self, results=(), *, paused_hosts=None):
        super().__init__(results)
        self.paused_hosts = dict(paused_hosts or {})


class DetailFetcher:
    def fetch(self, url: str) -> FetchResult:  # pragma: no cover - 接口定义
        raise NotImplementedError


_RETRY_DELAYS = (timedelta(hours=6), timedelta(hours=24))
REFETCH_FAILURE_PREFIX = "详情重抓未完成："
_FILE_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".wps", ".et", ".zip", ".rar", ".ofd")


@dataclass(frozen=True)
class _FetchOutcome:
    document_id: int
    fetched_at: datetime
    fetch: FetchResult | None
    parsed: DetailResult | None
    status: str
    failure_reason: str | None


def _retryable(status: str, http_status: int | None) -> bool:
    # robots.txt 暂不可用时禁止本次抓取，但可在退避后重新检查。pdf_retry：读 PDF 正文的子进程
    # 遇到和当时的环境有关的问题（容器内存不够被停掉、太忙等，见 pdf_sandbox），不是文件本身的问题
    return status in {"network_error", "robots_error", "pdf_retry"} or (
        status == "http_error"
        and http_status is not None
        and (http_status == 429 or (http_status >= 500 and http_status != 521))
    )


def _government_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "gov.cn" or host.endswith(".gov.cn")


def _apply_full_title(doc: Document, title: str) -> None:
    """换成详情页的完整标题；按标题算的类型、类别和筛选标签随之重算（同入库和 youju reclassify）。
    人工定的类型（kind_override）不动。"""
    doc.title = title
    doc.rule_kind = doc_kind(title)
    doc.rule_category = doc_category(title)
    doc.rule_supports = SUPPORT_RULES.stored(title)
    doc.rule_industries = INDUSTRY_RULES.stored(title)


def _save_result(
    session: Session,
    outcome: _FetchOutcome,
) -> DetailFetchSummary:
    document_id = outcome.document_id
    doc = session.get(Document, document_id)
    if doc is None:
        return DetailFetchSummary(document_id, "missing", "题录已删除")
    detail = session.get(DocumentDetail, document_id)
    if detail is not None and detail.status == "ok" and outcome.status != "ok":
        # 补附件失败不能抹掉已经成功入库的正文、附件与原抓取时间。复用重试字段，
        # 用固定标记区分本次维护与首次抓取的次数；永久失败不自动重试。
        attempts = (
            (detail.attempt_count or 0) + 1
            if (detail.failure_reason or "").startswith(REFETCH_FAILURE_PREFIX)
            else 1
        )
        detail.attempt_count = attempts
        detail.failure_reason = REFETCH_FAILURE_PREFIX + outcome.status
        detail.next_retry_at = (
            outcome.fetched_at + _RETRY_DELAYS[attempts - 1]
            if attempts <= len(_RETRY_DELAYS)
            and _retryable(outcome.status, outcome.fetch.http_status if outcome.fetch else None)
            else None
        )
        session.commit()
        return DetailFetchSummary(document_id, outcome.status, outcome.failure_reason)
    if detail is None:
        detail = DocumentDetail(document_id=document_id)
        session.add(detail)
    attempts = detail.attempt_count + 1 if detail.attempt_count is not None else 1
    detail.attempt_count = attempts
    detail.next_retry_at = (
        outcome.fetched_at + _RETRY_DELAYS[attempts - 1]
        if attempts <= len(_RETRY_DELAYS)
        and _retryable(outcome.status, outcome.fetch.http_status if outcome.fetch else None)
        else None
    )
    body = outcome.parsed.body_text if outcome.parsed else ""
    # 数据库正文列有上限（MEDIUMTEXT 16MB），超长的截断，不让整份详情写入失败
    detail.body_text = body[:BODY_TEXT_MAX_CHARS]
    detail.issuer = (
        normalize_issuer(outcome.parsed.issuer)
        if outcome.parsed and outcome.parsed.issuer
        else None
    )
    detail.doc_number = outcome.parsed.doc_number if outcome.parsed else None
    detail.publish_date = outcome.parsed.publish_date if outcome.parsed else None
    detail.fetched_at = outcome.fetched_at
    detail.http_status = outcome.fetch.http_status if outcome.fetch else None
    detail.status = outcome.status
    detail.failure_reason = outcome.failure_reason
    detail.final_url = outcome.fetch.final_url if outcome.fetch else None
    titles = {doc.title}
    if outcome.parsed and outcome.status == "ok":
        detail.attachments = [
            DocumentAttachment(position=i, name=a.name, url=a.url, extension=a.extension)
            for i, a in enumerate(outcome.parsed.attachments)
        ]
        doc.status = "fetched"
        # 列表页截断的标题（以"..."或"…"结尾）用详情页标明的完整标题补全，对不上不改
        full = completed_title(doc.title, outcome.parsed.page_titles)
        if full is not None:
            _apply_full_title(doc, full)
            titles.add(full)
    # 截断的旧标题不参与归并；补全后按新标题归并（同一文件的其他网址可能早就是完整标题）
    reconcile_documents(session, titles)
    session.commit()
    return DetailFetchSummary(document_id, outcome.status, outcome.failure_reason)


def _pdf_detail(content: bytes) -> DetailResult:
    """PDF 当正文：入库的遮过号码，没遮的只留在内存里（raw_body）。"""
    raw, error = extract_pdf_text(content)
    return DetailResult(body_text=redact_sensitive(raw), failure_reason=error, raw_body=raw)


def _html_page_gate(text: str, url: str) -> bool:
    """网页是不是整页就是一个关卡（D-013：防护页、验证码、登录页，要停取这个网站）：云锁、安全狗这类
    平台的拦截页，标题写着登录、验证或者整页几乎没字又带着验证码、密码框的（html_gate）；详情页解析
    认出的验证码、访问过于频繁这类字也要整页几乎没字才算——正常通知讲验证码使用要求、页头挂个登录
    小框都不算，不停整个网站。"""
    if html_gate(text):
        return True
    return bare_page(text) and parse_detail(text, url).failure_reason == "防护页或验证码"


def _gate_page(result: FetchResult, url: str) -> bool:
    """回来的是不是防护页、验证码（D-013：网站拦着我们，要停取这个网站）。网页按整页是不是一个关卡
    认（_html_page_gate）；不是 PDF 也不是网页的回应（纯文字、JSON）照 text_gate。判过的记在回应上：
    取页器还拿着网站锁时判一次（_PausingFetcher._response），_fetch_one 直接用。"""
    cached = getattr(result, "_gate_decision", None)
    if cached is not None:
        return cached
    content = result.content or b""
    text = result.text or (decode_html(content, result.content_type) if content else "")
    head = text.lstrip("\ufeff \t\r\n")
    if b"%PDF-" in content[:1024] or not head or "\x00" in head[:1024]:
        decision = False  # PDF、空的、二进制文件
    elif head.startswith("<"):
        decision = _html_page_gate(text, result.final_url or url)
    else:
        decision = text_gate(head)
    result._gate_decision = decision
    return decision


def _without_body(fetched: FetchResult) -> FetchResult:
    """防护页交回的抓取结果：留状态、状态码、最后的网址，去掉网页内容（拦截页上的字不往下传）。"""
    return replace(fetched, content=b"", text="")


def _fetch_one(document_id: int, url: str, fetcher: DetailFetcher) -> _FetchOutcome:
    now = datetime.now(UTC)
    if not _government_url(url):
        return _FetchOutcome(document_id, now, None, None, "unsupported_host", "非政府网站")
    if urlsplit(url).path.lower().endswith(_FILE_SUFFIXES) and not urlsplit(
        url
    ).path.lower().endswith(".pdf"):
        return _FetchOutcome(
            document_id, now, None, None, "unsupported_format", "链接不是 HTML 详情页"
        )
    fetched = fetcher.fetch(url)
    now = datetime.now(UTC)
    if fetched.status != "ok":
        return _FetchOutcome(
            document_id, now, fetched, None, fetched.status, fetched.error or fetched.status
        )
    if not _government_url(fetched.final_url or url):
        return _FetchOutcome(document_id, now, fetched, None, "unsupported_host", "非政府网站")
    content_type = (fetched.content_type or "").split(";", 1)[0].strip().lower()
    is_pdf = (
        content_type == "application/pdf"
        or fetched.content.startswith(b"%PDF-")
        or fetched.text.lstrip().startswith("%PDF-")
    )
    # 防护页、验证码（网页、纯文字的验证码页、拦截页、说是 PDF 其实不是的）：先停这个网站，不当成
    # 格式不对、PDF 读不出；不管 Content-Type 写的什么（text/html 回纯文字的 Access Denied 也算）
    if _gate_page(fetched, url):
        return _FetchOutcome(
            document_id, now, _without_body(fetched), None, "protected", "防护页或验证码"
        )
    if is_pdf:
        try:
            parsed = _pdf_detail(fetched.content)
        except pdf_sandbox.Cancelled:
            return _FetchOutcome(document_id, now, fetched, None, "deadline", "停下了，PDF 没读完")
        except pdf_sandbox.Failed as exc:  # 只有 retry 为真的会抛到这里
            return _FetchOutcome(document_id, now, fetched, None, "pdf_retry", f"PDF {exc.detail}")
        return _FetchOutcome(
            document_id,
            now,
            fetched,
            parsed,
            "ok" if parsed.body_text else "pdf_error",
            parsed.failure_reason,
        )
    if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
        return _FetchOutcome(
            document_id, now, fetched, None, "unsupported_format", "响应不是 HTML 详情页"
        )
    parsed = parse_detail(fetched.text, fetched.final_url or url)
    if parsed.failure_reason == "防护页或验证码":
        # 整页关卡上面已经停了网站；这里是页面上有验证码这类字、又不像整页就是一个关卡（正常通知讲
        # 验证码使用要求这类）：不停网站，这份记读不出、不取正文
        why = "页面上有验证码这类字，不像整页就是关卡，没停取网站，也没取正文"
        return _FetchOutcome(
            document_id, now, fetched, DetailResult(failure_reason=why), "parse_empty", why
        )
    pdf_attachments = [a for a in parsed.attachments if a.extension == "pdf"]
    if not parsed.body_text and len(parsed.attachments) == 1 and len(pdf_attachments) == 1:
        pdf_url = pdf_attachments[0].url
        if not _government_url(pdf_url):
            return _FetchOutcome(
                document_id, now, fetched, parsed, "unsupported_host", "PDF 非政府网站"
            )
        pdf_fetch = fetcher.fetch(pdf_url)
        if pdf_fetch.status != "ok":
            return _FetchOutcome(
                document_id,
                now,
                pdf_fetch,
                parsed,
                pdf_fetch.status,
                pdf_fetch.error or pdf_fetch.status,
            )
        if not _government_url(pdf_fetch.final_url or pdf_url):
            return _FetchOutcome(
                document_id, now, pdf_fetch, parsed, "unsupported_host", "PDF 非政府网站"
            )
        # PDF 附件回来的不是 PDF（网页、纯文字的验证码页）：先看是不是防护页（是就记 protected、
        # 停这个网站），不当成 PDF 读不出
        if _gate_page(pdf_fetch, pdf_url):
            return _FetchOutcome(
                document_id, now, _without_body(pdf_fetch), parsed, "protected", "防护页或验证码"
            )
        try:
            pdf = _pdf_detail(pdf_fetch.content)
        except pdf_sandbox.Cancelled:
            return _FetchOutcome(
                document_id, now, pdf_fetch, parsed, "deadline", "停下了，PDF 没读完"
            )
        except pdf_sandbox.Failed as exc:  # 只有 retry 为真的会抛到这里
            reason = f"PDF {exc.detail}"
            return _FetchOutcome(document_id, now, pdf_fetch, parsed, "pdf_retry", reason)
        if pdf.failure_reason:
            return _FetchOutcome(
                document_id, now, pdf_fetch, parsed, "pdf_error", pdf.failure_reason
            )
        parsed.body_text, parsed.raw_body = pdf.body_text, pdf.raw_body
    status = (
        "protected"
        if parsed.failure_reason == "防护页或验证码"
        else "parse_empty"
        if parsed.failure_reason
        else "ok"
    )
    return _FetchOutcome(document_id, now, fetched, parsed, status, parsed.failure_reason)


class _PauseStoreError(RuntimeError):
    """停取记录无法保存时不得继续抓取。"""


# 停取记录里延后的单份文件：唯一的 PDF 在停取中的网站上，那边恢复以前不再选它
DEFERRED_PREFIX = "doc:"


class _SitePause:
    """停取记录。持久的存在共享文件里：每轮抓取的补正文和补正文队列共用一份（各是独立的进程），
    用独立锁文件保护原子替换；别的进程写过（文件变了）下次查时就重读。只停这次运行的（搜索限流）
    另存在内存里，同步文件时不会被盖掉。"""

    def __init__(self, path: str | None = None) -> None:
        self.persistent: dict[str, tuple[str, datetime]] = {}
        self.temporary: dict[str, str] = {}
        self.lock = threading.RLock()
        self.path = Path(path) if path else None
        self._seen: tuple[int, int, int] | None = None
        if self.path:
            try:
                self._sync()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise _PauseStoreError(f"停取记录读不出：{self.path}") from exc

    @property
    def reasons(self) -> dict[str, str]:
        self.refresh()
        now = datetime.now(UTC)
        with self.lock:
            merged = {h: r for h, (r, until) in self.persistent.items() if until > now}
            merged.update(self.temporary)
            return merged

    @property
    def hosts(self) -> dict[str, str]:
        """停取中的网站（不含延后的单份文件 doc:编号）。"""
        return {h: r for h, r in self.reasons.items() if not h.startswith(DEFERRED_PREFIX)}

    def expiry(self, key: str) -> datetime | None:
        """持久停取到什么时候；只停这次运行的、没停的给 None。"""
        now = datetime.now(UTC)
        with self.lock:
            record = self.persistent.get(key)
            return record[1] if record and record[1] > now else None

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (info.st_ino, info.st_mtime_ns, info.st_size)

    def refresh(self) -> None:
        """文件变了（别的进程加了记录）就重读；读不出时停下这次运行（宁可不抓，也不能在不知道
        哪些网站拒绝过我们的情况下接着抓）。"""
        if self.path is None:
            return
        with self.lock:
            try:
                if self._stat() != self._seen:
                    self._sync()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise _PauseStoreError(f"停取记录读不出：{self.path}") from exc

    def _sync(self, addition=None) -> None:
        path = self.path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path) + ".lock", "a") as lockfile:
            fcntl.flock(lockfile, fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    data = {}
                now = datetime.now(UTC)
                records = {}
                for host, value in data.items():
                    until = datetime.fromisoformat(value["until"])
                    if until > now:
                        records[host] = (value["reason"], until)
                if addition:
                    host, reason, until = addition
                    if host not in records or records[host][1] < until:
                        records[host] = (reason, until)
                    payload = {
                        h: {"reason": r, "until": t.isoformat()} for h, (r, t) in records.items()
                    }
                    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as out:
                            json.dump(payload, out, ensure_ascii=False)
                            out.flush()
                            os.fsync(out.fileno())
                        os.replace(tmp, path)
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                self.persistent = records
                self._seen = self._stat()
            finally:
                fcntl.flock(lockfile, fcntl.LOCK_UN)

    def add(
        self,
        host: str,
        reason: str,
        *,
        persistent: bool = True,
        hours: float | None = None,
        until: datetime | None = None,
    ) -> None:
        """persistent：写进共享文件，停到 until（不给时停 hours 小时；都不给时 429 停 6 小时、别的
        24 小时）；已有更长的停取不缩短。不持久的只停这次运行。"""
        if not host:
            return
        with self.lock:
            if not persistent:
                self.temporary[host] = reason
                return
            if until is None:
                if hours is None:
                    hours = 6 if reason == "HTTP 429" else 24
                until = datetime.now(UTC) + timedelta(hours=hours)
            if self.path is None:
                old = self.persistent.get(host)
                if old is None or old[1] < until:
                    self.persistent[host] = (reason, until)
                return
            try:
                self._sync((host, reason, until))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise _PauseStoreError("停取记录保存失败") from exc


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class _PausingFetcher:
    """取一份文件用：取页器每一跳、每次真正发请求之前问 _admit——这份文件已经经过的网站和要去的网站
    有没有这一轮已停的，有就不去（记 site_paused；详情页跳进已停网站以后，它的 PDF 附件也不再取）。
    放行的页面请求当场记下（record=True）；查 robots 前的预先检查、读 robots.txt 的请求只查不记，
    被挡下没去的也不算经过。"""

    def __init__(self, inner: DetailFetcher, pause: _SitePause, should_stop=None) -> None:
        self.inner, self.pause, self.should_stop = inner, pause, should_stop
        self.visited: list[str] = []
        # 这份文件每次请求碰过的地址都累计起来（详情页和它的 PDF 附件，含跳转、读 robots.txt
        # 走过的）
        self.touched: list[str] = []

    def _admit(self, target: str, *, record: bool = True) -> bool:
        if self.should_stop and self.should_stop():
            return False
        with self.pause.lock:
            if ({_host(u) for u in self.visited} | {_host(target)}) & self.pause.reasons.keys():
                return False
            if record:
                self.visited.append(target)
            return True

    def _response(self, result: FetchResult) -> None:
        """页面的回应（取页器还拿着这个网站的锁，进程内的和跨进程的）：被拒、回 200 的验证码或
        防护页，当场记下停取。别的线程、进程排在这个网站的锁后面，等它放锁时停取已经写好，不会
        等够间隔又去敲门。"""
        host = _host(result.final_url or result.url)
        if result.http_status in {401, 403, 412, 429, 521}:
            self.pause.add(host, f"HTTP {result.http_status}")
        elif result.status == "ok" and _gate_page(result, result.final_url or result.url):
            self.pause.add(host, "防护页或验证码")

    def _robots(self, result: FetchResult) -> None:
        """读 robots.txt 的回应（同样还拿着锁）：回了防护页、429、521，当场记下停取（和事后按
        robots 读不到停取的时长一样）。"""
        host = _host(result.final_url or result.url)
        status = result.http_status
        if result.status == "ok" and robots_gate(result):
            self.pause.add(host, "robots.txt 回了防护页或验证码")
        elif status in (429, 521):
            self.pause.add(host, f"robots.txt HTTP {status}", hours=6)

    def fetch(self, url: str) -> FetchResult:
        if isinstance(self.inner, PoliteFetcher):
            result = self.inner.fetch(
                url,
                hop_allowed=self._admit,
                on_response=self._response,
                on_robots=self._robots,
                stop_when=self.should_stop,
            )
        elif not self._admit(url):
            result = FetchResult(url, "site_paused", error=SITE_PAUSED)
        else:
            # 测试用的假取页器不跟跳转：请求前问一次，请求后记下跳转后的地址
            result = self.inner.fetch(url)
            if not result.url:
                result.url = url
            self._response(result)
            with self.pause.lock:
                self.visited += [*result.hops, result.final_url or url]
        self.touched += [url, *result.hops, *result.robots_hops, result.final_url or url]
        return result


def _follow(pause: _SitePause, host: str, blocked: str, reason: str) -> None:
    """host 跟着停取中的 blocked 停：blocked 持久停到什么时候 host 也停到那时，只停这次运行的
    host 也只停这次运行。"""
    until = pause.expiry(blocked)
    if until is not None:
        pause.add(host, reason, until=until)
    else:
        pause.add(host, reason, persistent=False)


def _follow_paused(pause: _SitePause, document_id: int, url: str, outcome) -> None:
    """这份文件被挡在别的停取中的网站 B 前面：
    - 挡在 robots.txt 上（A 的 robots.txt 跳到 B）：A 上每份文件都要先读这份 robots.txt，A 跟着
      B 停到 B 恢复；
    - 详情页自己跳到 B：这份延后到 B 恢复（doc:编号），A 只停这次运行（A 没拒绝过我们，下次运行
      再试一份）；
    - 详情页取到了、唯一的 PDF 在 B 上：这份延后到 B 恢复，不影响 A 上别的文件。"""
    own = _host(url)
    fetched = outcome.fetch
    blocked = _host(fetched.robots_url or fetched.final_url or fetched.url or "")
    if not blocked or blocked == own or blocked not in pause.reasons:
        return
    until = pause.expiry(blocked)
    doc = f"{DEFERRED_PREFIX}{document_id}"
    if fetched.robots_url:
        hop = _host(fetched.final_url or url)
        _follow(pause, hop, blocked, f"robots.txt 跳到停取中的 {blocked}")
        if hop != own and until is not None:
            # 挡住的是别的网站上的 PDF（那边的 robots.txt 跳进停取中的网站）：这份也延后到那边
            # 恢复，下次运行不再先把详情页取一遍
            pause.add(doc, f"PDF 所在网站的 robots.txt 跳到停取中的 {blocked}", until=until)
    elif outcome.parsed is None:
        if until is not None:
            pause.add(doc, f"跳到停取中的 {blocked}", until=until)
        pause.add(own, f"跳到停取中的 {blocked}", persistent=False)
    elif until is not None:
        pause.add(doc, f"PDF 在停取中的 {blocked} 上", until=until)


def _fetch_outcomes(
    selected: list[tuple[int, str]],
    fetcher: DetailFetcher,
    max_workers: int,
    *,
    pause: _SitePause | None = None,
    on_outcome=None,
    should_stop=None,
    strip_content: bool = False,
) -> dict[int, _FetchOutcome]:
    """strip_content：交回的结果不带页面内容（只写库的调用方用；body_refresh 这类要拿原网页核对的
    不能清）。"""
    by_host: dict[str, list[tuple[int, str]]] = {}
    for document_id, url in selected:
        by_host.setdefault(_host(url), []).append((document_id, url))

    # 没传就用配置的共享停取文件：body_refresh、refetch_redacted、名单 rescan、补标题这些手动命令
    # 也和补正文队列、每轮抓取共用一份——队列刚停的网站它们不去，它们被拒也写进去
    pause = pause or _SitePause(get_settings().detail_pause_path)
    completed: queue.Queue = queue.Queue(maxsize=max(1, max_workers))
    failed = threading.Event()

    def stopped():
        return failed.is_set() or bool(should_stop and should_stop())

    def put(item):
        while not failed.is_set():
            try:
                completed.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def produce(documents):
        odd_streak = 0
        try:
            for document_id, url in documents:
                if stopped():
                    break
                with pause.lock:
                    reasons = pause.reasons
                    if _host(url) in reasons:
                        break
                    if f"{DEFERRED_PREFIX}{document_id}" in reasons:
                        continue
                guarded = _PausingFetcher(fetcher, pause, stopped)
                try:
                    with pdf_sandbox.stop_when(stopped):
                        outcome = _fetch_one(document_id, url, guarded)
                except (_PauseStoreError, SharedLimitError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    reason = f"详情处理异常：{type(exc).__name__}"
                    log.warning("详情 %s %s", document_id, reason)
                    outcome = _FetchOutcome(
                        document_id, datetime.now(UTC), None, None, "processing_error", reason
                    )
                if outcome.status == "robots_error":
                    actual = outcome.fetch.final_url or outcome.fetch.url if outcome.fetch else url
                    page_host = _host(actual)
                    # 停的是实际回了错的那台主机（robots.txt 可能跳到了别的主机）
                    if outcome.fetch and outcome.fetch.robots_url:
                        actual = outcome.fetch.robots_url
                    code = outcome.fetch.robots_status if outcome.fetch else None
                    failure = outcome.fetch.robots_failure if outcome.fetch else ""
                    if failure == "protected":
                        # robots.txt 回了防护页、验证码：和页面被拦一样停 24 小时
                        pause.add(_host(actual), "robots.txt 回了防护页或验证码")
                    elif failure == "too_large":
                        # 太大读不完（回的是 200）：规则看不全，一时好不了，停 6 小时
                        pause.add(_host(actual), "robots.txt 读不到（太大读不完）", hours=6)
                    elif code in (429, 521):
                        # robots.txt 回 429（服务器忙）、521（防护）：读不到，停 6 小时再来
                        pause.add(_host(actual), f"robots.txt HTTP {code}", hours=6)
                    elif code:
                        # 回别的 5xx：停 1 小时，免得每次运行都去请求坏掉的 robots.txt
                        pause.add(_host(actual), f"robots.txt 读不到（HTTP {code}）", hours=1)
                    elif failure == "ssl_error":
                        # 证书、加密连接出错：对方网站的问题（我们自己断网不会这样），一时好不了
                        pause.add(_host(actual), "robots.txt 读不到（证书或加密连接出错）", hours=6)
                    else:
                        # 连不上、超时、域名解析失败：停 1 小时、写进共享文件，下次运行不再先选它
                        # （真是我们自己断网，熔断会停下这次运行，这些网站最多晚一小时再取）
                        pause.add(_host(actual), "robots.txt 读不到（连不上、超时）", hours=1)
                    if _host(actual) != page_host:
                        # A 的 robots.txt 跳到 B、B 读不到：A 上每份文件都要读它，A 跟着 B 停
                        reason = f"robots.txt 跳到读不到的 {_host(actual)}"
                        _follow(pause, page_host, _host(actual), reason)
                if outcome.status == "protected":
                    actual = outcome.fetch.final_url or outcome.fetch.url if outcome.fetch else url
                    pause.add(_host(actual), "防护页或验证码")
                    for visited in guarded.visited:
                        if search_throttle_site(visited) and _host(visited) != _host(actual):
                            pause.add(_host(visited), "搜索限流防护页", persistent=False)
                if outcome.status == "site_paused" and outcome.fetch is not None:
                    _follow_paused(pause, document_id, url, outcome)
                own = _host(url)
                answered = (
                    _host(outcome.fetch.final_url or outcome.fetch.url) if outcome.fetch else ""
                )
                # 只算详情页所在网站自己回的（PDF 在别的网站上回的不算到它头上）
                http = outcome.fetch.http_status if outcome.fetch and answered == own else None
                odd = http is not None and 400 <= http < 500 and http not in (404, 410)
                odd_streak = odd_streak + 1 if odd else 0
                if odd_streak >= 3:
                    # 连续 3 份回 404、410 以外的 4xx（405、418 这类多半是拦截）：这次运行先停
                    pause.add(own, f"连续 3 份 HTTP {http}", persistent=False)
                touched = {_host(u) for u in (url, *guarded.touched, *guarded.visited)}
                refused = outcome.status == "protected" or (
                    outcome.fetch and outcome.fetch.http_status in {401, 403, 412, 429, 521}
                )
                if outcome.status != "site_paused" and (
                    outcome.status in {"ok", "robots_error"}
                    or refused
                    or not touched & pause.reasons.keys()
                ):
                    if strip_content and outcome.fetch is not None:
                        # 写库只用状态码和最后的网址：页面内容（最多 20MB）不跟着排队
                        outcome = replace(
                            outcome, fetch=replace(outcome.fetch, content=b"", text="")
                        )
                    put(outcome)
                del outcome, guarded
                if _host(url) in pause.reasons:
                    break
        except BaseException as exc:
            put(exc)
        finally:
            put(None)

    outcomes = {}
    pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
    futures = []
    try:
        # 起线程也在 try 里：中途起不来时先让已经起来的停下（不然它们卡在排队上，进程退不出）
        for group in by_host.values():
            futures.append(pool.submit(produce, group))
        remaining = len(futures)
        while remaining:
            outcome = completed.get()
            if outcome is None:
                remaining -= 1
            elif isinstance(outcome, BaseException):
                raise outcome
            elif on_outcome:
                on_outcome(outcome)
                del outcome
            else:
                outcomes[outcome.document_id] = outcome
        for future in futures:
            future.result()
    except BaseException:
        failed.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return outcomes


def _fetch_selected(
    session: Session,
    selected: list[tuple[int, str]],
    fetcher: DetailFetcher,
    max_workers: int,
    *,
    pause: _SitePause | None = None,
    should_stop=None,
    on_outcome=None,
) -> list[DetailFetchSummary]:
    if not selected:
        return []
    saved = {}

    def save(outcome):
        if on_outcome is not None:
            # 回调写了库就交回结果（照实汇总），没写（熔断前先攒着、到期限没取完）交回 None
            result = on_outcome(outcome)
            if result is not None:
                saved[outcome.document_id] = result
        elif outcome.status != "deadline":
            # robots.txt 读不到的也照常写库（带重试次数，过几小时再来），免得每轮都把它选回来
            saved[outcome.document_id] = _save_result(session, outcome)

    _fetch_outcomes(
        selected,
        fetcher,
        max_workers,
        pause=pause,
        on_outcome=save,
        should_stop=should_stop,
        strip_content=True,
    )
    return [
        saved[document_id]
        if document_id in saved
        else DetailFetchSummary(document_id, "site_paused", SITE_PAUSED)
        for document_id, _ in selected
    ]


# 补正文队列因为跨进程限速用不了而停下（命令按失败退出，job.sh 发提醒）
SHARED_LIMIT_STOP = "跨进程限速用不了，停下"
# 熔断停下（多半是我们自己断网，也可能是一批网站同时连不上）：同样按失败退出、发提醒
CIRCUIT_STOP = "熔断"


def _polite_fetcher(deadline: float | None = None, require_shared: bool = False) -> PoliteFetcher:
    from youju.govsites.inventory import CachingResolver

    settings = get_settings()
    # 成功结果缓存一小时；解析失败不缓存，下次仍可重试。
    return PoliteFetcher(
        settings.user_agent,
        settings.request_timeout,
        settings.per_host_interval,
        per_address_interval=settings.per_address_interval,
        shared_limits_dir=settings.rate_limit_dir or None,
        per_address_concurrency=settings.per_address_concurrency,
        url_allowed=_government_url,
        resolver=CachingResolver(),
        deadline=deadline,
        require_shared_limits=require_shared,
    )


def _detail_order(today: date | None):
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    recent = Document.publish_date >= today - timedelta(days=30)
    priority = func.coalesce(Document.kind_override, Document.rule_kind).in_(("申报通知", "政策"))
    tier = case((and_(recent, priority), 0), (recent, 1), (priority, 2), else_=3)
    return (tier, Document.publish_date.is_(None), Document.publish_date.desc(), Document.id)


@dataclass(frozen=True)
class DetailQueueSummary:
    statuses: dict[str, int]
    total: int
    paused_hosts: dict[str, str]
    stop_reason: str
    batches: int


def run_detail_queue(
    session: Session,
    *,
    minutes: float,
    workers: int = 32,
    batch: int = 2000,
    per_host: int = 20,
    stop=None,
    today: date | None = None,
    fetcher: DetailFetcher | None = None,
    clock=time.monotonic,
) -> DetailQueueSummary:
    if minutes <= 0 or min(workers, batch, per_host) < 1:
        raise ValueError("minutes、workers、batch、per_host 必须大于 0")
    deadline = clock() + minutes * 60
    pause = _SitePause(get_settings().detail_pause_path)
    statuses: dict[str, int] = {}
    batches = 0
    # 这次运行选过的都不再选（写了库的有了详情；没写的被停取挡下、读不到 robots、到时间停了，
    # 或者熔断时扔掉了，留给下次运行）；写了库的另记下来，汇总时没写库的才算 site_paused
    chosen: set[int] = set()
    written: set[int] = set()
    gone: set[int] = set()
    # 还没写库的连续网络失败（到达时刻, 结果）：凑够熔断条件就整批不写
    pending: list[tuple[float, _FetchOutcome]] = []
    tripped = threading.Event()
    urls: dict[int, str] = {}

    def record(outcome):
        if outcome.status == "deadline":
            # 到了这次运行的期限没取完：不写库，报 site_paused。robots.txt 读不到的照常写库
            # （带重试次数，过几小时再来；网站另按停取处理），免得每次运行都把它选回来
            return None
        result = _save_result(session, outcome)
        if result.status == "missing":
            gone.add(outcome.document_id)  # 题录已经删了：不算"没写库"
            return None
        written.add(outcome.document_id)
        statuses[result.status] = statuses.get(result.status, 0) + 1
        return result

    def flush():
        for _, item in pending:
            record(item)
        pending.clear()

    def receive(outcome):
        if outcome.status == "deadline":
            return None  # 到了这次运行的期限没取完：不算网络失败、不写库，下次再取
        reason = (outcome.failure_reason or "").lower()
        robots_status = outcome.fetch.robots_status if outcome.fetch else None
        robots_failure = outcome.fetch.robots_failure if outcome.fetch else ""
        network = (
            outcome.status == "network_error"
            # robots.txt 回了状态码（5xx、429、521）、证书或加密连接出错是对方的事，不是我们自己
            # 断网；连不上、超时才可能是
            or (
                outcome.status == "robots_error"
                and robots_status is None
                and robots_failure not in ("ssl_error", "too_large")
            )
            or any(text in reason for text in ("域名解析失败", "同时在等的域名解析过多", "dns"))
        )
        if tripped.is_set():
            # 熔断以后网络失败不写库；别的结果（熔断前已经在取的）照常写
            return None if network else record(outcome)
        if not network:
            flush()
            return record(outcome)
        now = clock()
        # 熔断：连续 20 份网络失败发生在 60 秒以内、来自至少 5 个主机（多半是我们自己断网）。
        # 早于 60 秒的失败再也凑不进这样的时间窗，照常写库、记次数
        while pending and now - pending[0][0] > 60:
            record(pending.pop(0)[1])
        pending.append((now, outcome))
        window = pending[-20:]
        if (
            len(window) == 20
            and now - window[0][0] <= 60
            and len({_host(urls[x.document_id]) for _, x in window}) >= 5
        ):
            pending.clear()
            tripped.set()
        elif len(pending) > 20:
            record(pending.pop(0)[1])
        return None

    def stop_reason():
        if tripped.is_set():
            return f"{CIRCUIT_STOP}：60 秒内连续 20 份网络失败，涉及至少 5 个主机"
        if clock() >= deadline:
            return "到时间"
        if stop:
            reason = stop()
            if reason:
                return reason if isinstance(reason, str) else "每轮要锁"
        return None

    def run(active_fetcher):
        nonlocal batches
        while True:
            reason = stop_reason()
            if reason:
                return reason
            now = datetime.now(UTC)
            stmt = (
                select(Document.id, Document.url)
                .join(Source, Document.source_id == Source.id)
                .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
                .where(
                    Source.rights_level == 1,
                    or_(
                        DocumentDetail.document_id.is_(None),
                        and_(
                            DocumentDetail.next_retry_at.is_not(None),
                            DocumentDetail.next_retry_at <= now,
                            DocumentDetail.attempt_count < 3,
                        ),
                    ),
                )
                .order_by(*_detail_order(today))
            )
            selected = []
            counts: dict[str, int] = {}
            page_size = batch * 10
            page = 0
            picked: set[int] = set()
            # 一直翻到选满或者真到末尾：前面全是停取网站的文件时，后面的照样选得上
            while True:
                if page:
                    reason = stop_reason()  # 翻页之间也看一眼要不要停
                    if reason:
                        session.commit()
                        return reason
                paused = pause.reasons  # 每页取一次（要看文件、去掉过期的），不按行重算
                rows = session.execute(stmt.limit(page_size).offset(page * page_size))
                count = 0
                try:
                    for document_id, url in rows:
                        count += 1
                        host = _host(url)
                        if (
                            document_id in chosen
                            or document_id in picked  # 翻页之间有新插入时同一份可能出现两次
                            or host in paused
                            or f"{DEFERRED_PREFIX}{document_id}" in paused
                            or counts.get(host, 0) >= per_host
                        ):
                            continue
                        picked.add(document_id)
                        selected.append((int(document_id), url))
                        counts[host] = counts.get(host, 0) + 1
                        if len(selected) >= batch:
                            break
                finally:
                    rows.close()
                if len(selected) >= batch or count < page_size:
                    break
                page += 1
            session.commit()
            if not selected:
                return "没有了"
            batches += 1
            urls.update(selected)
            chosen.update(document_id for document_id, _ in selected)
            _fetch_selected(
                session,
                selected,
                active_fetcher,
                workers,
                pause=pause,
                should_stop=lambda: stop_reason() is not None,
                on_outcome=receive,
            )

    if fetcher is None:
        # 到了这次运行的期限，正在下载的也放弃（不拖着每轮抓取等锁）
        # 32 个并发的队列一定要跨进程限速：没配目录、目录用不了、中途锁文件读写出错都停下，
        # 不退回只按本进程限速
        try:
            with _polite_fetcher(
                deadline=time.monotonic() + minutes * 60, require_shared=True
            ) as active:
                reason = run(active)
        except SharedLimitError as exc:
            log.error("补正文队列停下：%s", exc)
            reason = f"{SHARED_LIMIT_STOP}：{exc}"
    else:
        reason = run(fetcher)
    if not tripped.is_set():
        flush()
    # 选了没写库的（被停取挡下、robots 读不到、停下、熔断扔掉）单列 site_paused；total 只算写了库的
    unsaved = len(chosen - written - gone)
    if unsaved:
        statuses["site_paused"] = unsaved
    return DetailQueueSummary(statuses, len(written), pause.hosts, reason, batches)


def fetch_details(
    session: Session,
    *,
    limit: int,
    source_id: int | None = None,
    fetcher: DetailFetcher | None = None,
    max_workers: int | None = None,
    since: date | None = None,
    notices_only: bool = False,
    document_ids: list[int] | None = None,
    refetch: bool = False,
    today: date | None = None,
) -> list[DetailFetchSummary]:
    """抓尚无详情或已到重试时间的公开政府文件；临时失败最多尝试三次。
    refetch：和 document_ids 一起用，详情已抓成功的也重抓一次（解析规则改进后补附件等）。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if refetch and document_ids is None:
        raise ValueError("refetch 必须指定 document_ids")
    now = datetime.now(UTC)
    stmt = (
        select(Document.id, Document.url)
        .join(Source, Document.source_id == Source.id)
        .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
        .where(Source.rights_level == 1)
        .order_by(*(_detail_order(today) if document_ids is None else (Document.id,)))
    )
    if not (refetch and document_ids is not None):
        stmt = stmt.where(
            or_(
                DocumentDetail.document_id.is_(None),
                and_(
                    DocumentDetail.next_retry_at.is_not(None),
                    DocumentDetail.next_retry_at <= now,
                    DocumentDetail.attempt_count < 3,
                ),
            )
        )
    if source_id is not None:
        stmt = stmt.where(Document.source_id == source_id)
    if since is not None:
        stmt = stmt.where(Document.publish_date >= since)
    if notices_only:
        stmt = stmt.where(
            or_(
                *(
                    Document.title.contains(term)
                    for term in ("申报", "征集", "推荐", "遴选", "认定")
                )
            )
        )
    pause = _SitePause(get_settings().detail_pause_path)
    if document_ids is not None:
        if not document_ids:
            return DetailFetchResults()
        stmt = stmt.where(Document.id.in_(document_ids))
        selected = [(int(i), url) for i, url in session.execute(stmt.limit(limit)).all()]
    else:
        # 停取中的网站、延后的文件选取时就跳过，一直翻到选满或真到末尾：不然排在最前面的都是
        # 停取网站的文件时，每轮都选它们、再一份份跳过，后面的新文件永远补不上
        selected, seen, page = [], set(), 0
        while len(selected) < limit:
            paused = pause.reasons
            rows = session.execute(stmt.limit(limit).offset(page * limit)).all()
            for document_id, url in rows:
                if (
                    document_id in seen
                    or _host(url) in paused
                    or f"{DEFERRED_PREFIX}{document_id}" in paused
                ):
                    continue
                seen.add(document_id)
                selected.append((int(document_id), url))
                if len(selected) >= limit:
                    break
            if len(rows) < limit:
                break
            page += 1
    # 查询事务先结束；网络请求期间不占用数据库连接和行锁。
    session.commit()
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if fetcher is not None:
        results = _fetch_selected(session, selected, fetcher, workers, pause=pause)
    else:
        with _polite_fetcher() as polite:
            results = _fetch_selected(session, selected, polite, workers, pause=pause)
    return DetailFetchResults(results, paused_hosts=pause.hosts)


@dataclass(frozen=True)
class TitleBackfill:
    document_id: int
    old_title: str
    # filled：可补全（预览）或已补全（写入）；no_match：详情页没有对得上的完整标题，不改；
    # not_truncated：去掉装饰符和粘连的正文后不是截断标题（youju reclassify 会清理），不抓也不改；
    # unavailable：详情页没抓到或没解析出内容（原因见 reason）；missing：题录已删除
    result: str
    new_title: str | None = None
    reason: str | None = None
    page_titles: tuple[str, ...] = ()


def backfill_truncated_titles(
    session: Session,
    *,
    apply: bool = False,
    limit: int,
    source_id: int | None = None,
    since: date | None = None,
    after_id: int | None = None,
    fetcher: DetailFetcher | None = None,
    max_workers: int | None = None,
) -> list[TitleBackfill]:
    """一次性补全已入库的截断标题：重新打开详情页，按补详情时的同一规则（completed_title）找完整标题。

    默认只预览，apply=True 才写入。只改标题和按标题算的类型、类别、筛选标签，并按新标题重算归并；
    详情、附件、抓取记录都不动。补全后不再以省略号结尾，重复运行不会再选中，也就不会再改。
    按编号从小到大处理；对不上的每次都还会选中，分批时用 after_id 从上一批最后一份之后接着处理。"""
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    stmt = (
        select(Document.id, Document.url, Document.title)
        .join(Source, Document.source_id == Source.id)
        .where(
            Source.rights_level == 1,
            or_(Document.title.like("%..."), Document.title.like("%…")),
        )
        .order_by(Document.id)
        .limit(limit)
    )
    if source_id is not None:
        stmt = stmt.where(Document.source_id == source_id)
    if since is not None:
        stmt = stmt.where(Document.publish_date >= since)
    if after_id is not None:
        stmt = stmt.where(Document.id > after_id)
    rows = [(int(i), url, title) for i, url, title in session.execute(stmt).all()]
    # 同补详情：网络请求期间不占用数据库连接和行锁。
    session.commit()
    selected = [(i, url) for i, url, title in rows if is_truncated(clean_title(title))]
    workers = max_workers if max_workers is not None else get_settings().max_workers
    if not selected:
        outcomes: dict[int, _FetchOutcome] = {}
    elif fetcher is not None:
        outcomes = _fetch_outcomes(selected, fetcher, workers)
    else:
        with _polite_fetcher() as polite:
            outcomes = _fetch_outcomes(selected, polite, workers)
    results: list[TitleBackfill] = []
    chosen = {document_id for document_id, _ in selected}
    for document_id, _, title in rows:
        outcome = outcomes.get(document_id)
        if outcome is None and document_id in chosen:
            # 同一网站这一轮停取了（见 _fetch_outcomes），没去取：不是"没截断"
            results.append(TitleBackfill(document_id, title, "unavailable", reason="site_paused"))
            continue
        if outcome is None:
            results.append(TitleBackfill(document_id, title, "not_truncated"))
            continue
        if outcome.status != "ok" or outcome.parsed is None:
            reason = outcome.status
            results.append(TitleBackfill(document_id, title, "unavailable", reason=reason))
            continue
        page_titles = tuple(outcome.parsed.page_titles)
        doc = session.get(Document, document_id) if apply else None
        if apply:
            if doc is None:
                results.append(TitleBackfill(document_id, title, "missing"))
                continue
            # 按写入时库里的标题再判断一次：期间被别的任务改过的，按改过的算
            title = doc.title
        full = completed_title(title, page_titles)
        if full is None:
            result = "no_match" if is_truncated(clean_title(title)) else "not_truncated"
            results.append(TitleBackfill(document_id, title, result, page_titles=page_titles))
            continue
        if doc is not None:
            _apply_full_title(doc, full)
            reconcile_documents(session, {title, full})
            session.commit()
        results.append(TitleBackfill(document_id, title, "filled", full, page_titles=page_titles))
    return results
