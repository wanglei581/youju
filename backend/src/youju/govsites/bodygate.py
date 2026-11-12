"""正文能读率门（D-129，方案第三节）：每省每级从试抓到的申报通知、政策里（不限发布时间）按种子随机
取 60 份试取正文，能读率 ≥90% 才放，和 D-097 的门槛一致。这两类合起来 20–59 份的全取；不足 20 份的
先不放（等抓到够数再测），也不取正文。

- 能读的判断和日报、详情页同一个：v1.reading_body 的 status == "ok"（D-097）。
- 取正文用现成的 fetch_details：同一网站间隔至少 3 秒、只取政府网址、遵守 robots.txt。取到的详情写进
  YOUJU_DATABASE_URL 指的库，所以只在本机试抓用的库上跑：命令行要用 --trial-db 写明库名，连的必须是
  本机库、库里不能有在收的来源（check_trial_db），不然不取。
- 来源：这一级这个省试抓过的（验证状态以"试跑可读"开头）；抽查证据表里记了不通过、不收、不能抓、
  待重试的栏目不算。
- 证据写到 抽查/<层级>/正文/<省>.csv（放子目录：抽查/<层级>/ 下的 CSV 都当省份证据表读）。公开仓库：
  标题过 public_title。按平台分别列，哪个平台低就改哪个平台的提取规则。
"""

import csv
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.models import Document, Source

GATE_SIZE = 60
GATE_MIN = 20
GATE_RATE = 0.9
GATE_KINDS = ("申报通知", "政策")
FIELDS = (
    "序号",
    "城市",
    "区县",
    "部门",
    "栏目URL",
    "平台",
    "文件标题",
    "文件网址",
    "类型",
    "能读",
    "原因",
)
_SKIPPED = ("不通过", "不收", "不能抓", "待重试")


@dataclass
class BodyGate:
    province: str
    level: str
    available: int = 0  # 这一省这一级能抽的申报通知、政策有几份
    rows: list[dict] = field(default_factory=list)

    @property
    def readable(self) -> int:
        return sum(r["能读"] == "能读" for r in self.rows)

    @property
    def passed(self) -> bool:
        return gate_passed(len(self.rows), self.readable)

    def text(self) -> str:
        if self.available < GATE_MIN:
            return (
                f"{self.province}：申报通知、政策只有 {self.available} 份，"
                f"不足 {GATE_MIN} 份，先不放（等抓到够数再测）"
            )
        return f"{self.province}：{gate_text(len(self.rows), self.readable)}"

    def by_platform(self) -> list[tuple[str, int, int]]:
        """每个平台：能读几份、抽了几份（抽得多的在前）。"""
        total = Counter(r["平台"] for r in self.rows)
        ok = Counter(r["平台"] for r in self.rows if r["能读"] == "能读")
        return [(name, ok[name], n) for name, n in total.most_common()]


MIN_INTERVAL = 3.0  # 同一网站两次请求至少隔 3 秒（不管配置里调成多少）


def polite_fetcher():
    """取正文用的抓取器：和 fetch_details 默认的一样（只取政府网址、遵守 robots.txt），同一网站间隔
    至少 3 秒。"""
    from youju.config import get_settings
    from youju.crawler.detail_service import _government_url
    from youju.crawler.fetcher import PoliteFetcher

    settings = get_settings()
    return PoliteFetcher(
        settings.user_agent,
        settings.request_timeout,
        max(settings.per_host_interval, MIN_INTERVAL),
        url_allowed=_government_url,
    )


def check_trial_db(session: Session, trial_db: str | None) -> str | None:
    """取正文要写库：只在写明了库名的本机试抓库上跑。看的是实际连上的地址和库（连接参数可能
    盖过网址里写的）。不行时返回原因，行就返回 None。"""
    from sqlalchemy import text

    raw = session.connection().connection.dbapi_connection
    socket = getattr(raw, "unix_socket", None)
    host = getattr(raw, "host", None)
    database = session.scalar(text("SELECT DATABASE()"))
    where = f"{socket or host}/{database}"
    if not socket and host not in ("127.0.0.1", "localhost", "::1"):
        return f"连的不是本机库（{where}）"
    if not trial_db or database != trial_db:
        return f"要用 --trial-db 写明试抓库的库名，和实际连上的库（{where}）对上"
    if session.scalar(select(Source.id).where(Source.enabled.is_(True)).limit(1)) is not None:
        return f"{where} 里有在收的来源，不像试抓库"
    return None


def gate_passed(picked: int, readable: int) -> bool:
    return picked >= GATE_MIN and readable / picked >= GATE_RATE


def gate_text(picked: int, readable: int) -> str:
    if picked < GATE_MIN:
        return f"正文能读率：只抽到 {picked} 份，不足 {GATE_MIN} 份 → 没过"
    rate = readable / picked
    verdict = "过" if gate_passed(picked, readable) else "没过"
    return (
        f"正文能读率：抽 {picked} 份，能读 {readable} 份（{rate:.0%}，要 {GATE_RATE:.0%}）"
        f"→ {verdict}"
    )


def skipped_urls(evidence: Path | None) -> set[str]:
    """抽查证据表里记了不通过、不收、不能抓、待重试的栏目网址。"""
    from youju.govsites.sample import final_verdict

    if evidence is None or not evidence.exists():
        return set()
    with evidence.open(encoding="utf-8-sig", newline="") as fh:
        return {r["栏目URL"] for r in csv.DictReader(fh) if final_verdict(r) in _SKIPPED}


def platforms_by_url(out: Path, province: str, level: str) -> dict[str, str]:
    """栏目清单里每个栏目网址的建站平台。"""
    path = out / "栏目清单" / f"{province}.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {r["栏目URL"]: r.get("平台") or "" for r in csv.DictReader(fh) if r["层级"] == level}


def candidates(
    session: Session, level: str, province: str, skipped: set[str] = frozenset()
) -> list[tuple[Document, Source, str]]:
    from youju.classify import effective_kind

    stmt = (
        select(Document, Source)
        .join(Source, Document.source_id == Source.id)
        .where(
            Source.level == level,
            Source.province == province,
            Source.verification_status.startswith("试跑可读"),
        )
        .order_by(Document.id)
    )
    out = []
    for doc, src in session.execute(stmt).all():
        if src.url in skipped:
            continue
        kind = effective_kind(doc.title, doc.kind_override)
        if kind in GATE_KINDS:
            out.append((doc, src, kind))
    return out


def pick(cands: list, seed: str, size: int = GATE_SIZE) -> list:
    """按种子散列排序取前 size 份：重跑抽到的一样，换种子就换一批。"""
    return sorted(cands, key=lambda c: hashlib.sha1(f"{seed}|{c[0].url}".encode()).hexdigest())[
        :size
    ]


def run_gate(
    session: Session,
    level: str,
    province: str,
    *,
    seed: str,
    evidence: Path | None = None,
    platforms: dict[str, str] | None = None,
    fetch: bool = True,
    fetcher=None,
    now: datetime | None = None,
) -> BodyGate:
    from youju import attachment_text
    from youju.api import v1
    from youju.crawler.detail_service import fetch_details
    from youju.digest import BODY_PROBLEMS
    from youju.govsites.privacy import public_title

    gate = BodyGate(province, level)
    cands = candidates(session, level, province, skipped_urls(evidence))
    gate.available = len(cands)
    if gate.available < GATE_MIN:
        return gate
    picked = pick(cands, seed)
    if fetch:
        ids = [doc.id for doc, _, _ in picked]
        if fetcher is not None:
            fetch_details(session, limit=len(ids), document_ids=ids, fetcher=fetcher)
        else:
            with polite_fetcher() as polite:
                fetch_details(session, limit=len(ids), document_ids=ids, fetcher=polite)
        session.expire_all()
    now = now or datetime.now(UTC)
    for n, (doc, src, kind) in enumerate(picked, 1):
        items = attachment_text.readings(session, doc.id, now)
        body = v1.reading_body(
            session, doc, kind, attachments=bool(items), redact=attachment_text.mask
        )
        ok = body.status == "ok"
        gate.rows.append(
            {
                "序号": n,
                "城市": src.city or "",
                "区县": src.district or "",
                "部门": src.dept,
                "栏目URL": src.url,
                "平台": (platforms or {}).get(src.url) or "未知",
                "文件标题": public_title(doc.title or "")[:60],
                "文件网址": doc.url,
                "类型": kind,
                "能读": "能读" if ok else "读不了",
                "原因": "" if ok else BODY_PROBLEMS.get(body.problem or body.status, "没取到正文"),
            }
        )
    return gate


def write_gate(out: Path, gate: BodyGate) -> Path:
    """写 抽查/<层级>/正文/<省>.csv；不足 20 份的只写表头（记下测过、不够数）。"""
    folder = out / "抽查" / gate.level / "正文"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{gate.province}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS)
        w.writeheader()
        w.writerows(gate.rows)
    return path


def read_gate(folder: Path, province: str) -> tuple[int, int] | None:
    """读 <抽查/层级>/正文/<省>.csv：抽了几份、能读几份；还没测过是 None。"""
    path = folder / "正文" / f"{province}.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return len(rows), sum(r["能读"] == "能读" for r in rows)
