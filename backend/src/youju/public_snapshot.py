"""公开内容快照：本机测试用线上最新的数据，用户数据不出线上（创始人 10/25）。

- 表：只导出 PUBLIC_TABLES（白名单）——来源、通知、正文、附件和附件文字、截止日期、名单、AI 质检、
  公众号周报方案等，都是政府公开发布的内容和据此整理出的结果。其余表一律不导出：用户、关注、纠错、
  订阅、订单、发票、AI 工具任务等用户数据，密钥，下架记录、周报期刊，访问日志和运维记录
  （EXCLUDED_TABLES）。新加的表两边都没写就不导出，测试也会失败，逼着写明归哪一类。
- 行：下架的文件不导出（和小程序同一口径：归并组里有一份下架，整组都不对外。下架多是有人投诉，
  按个保法第二十七条属于个人明确拒绝，不能再以"已公开"为由处理），挂在它们下面的正文、附件、
  名单、截止日期、AI 结果等一起不导出，只被它们用到的附件副本和附件文字、名单文件、转发组也不导出。
  截止日期里由用户提交的线索生成的行（origin='user'，依据是用户填的）不导出。
- 字段：恢复上架的文件，下架、恢复备注是人工填写的，可能写到投诉人，导出时清空。
- 格式和整库备份相同（gzip JSON Lines：说明行、记录、结尾各表 SHA-256），说明行的 format 不同，
  整库恢复和公开快照导入互相认不出对方的文件。说明行另记线上代码版本（code_revision），本机用同一
  版本的代码建表导入，表结构才对得上。
- 存放：对象存储 public-snapshots/youju-public-日期.jsonl.gz（北京时间的日期，同一天重跑就覆盖），
  保留 7 天，和 backups/ 分开。admin 每天 4:50 生成；本机用 tools/team/fresh-test-db.sh 下载最新
  一份、建一个新测试库导入。
"""

import gzip
import hashlib
import json
import tempfile
import time as clock
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import httpx
from sqlalchemy import Connection, Engine, Table, and_, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from youju.backup import (
    BEIJING,
    BackupError,
    _line,
    _load,
    _retrying,
    _revision,
    _verify,
    delete_files,
    upload,
)
from youju.models import Base, Document
from youju.takedown import available_condition
from youju.version import build_revision

FORMAT = "youju-public-snapshot"
VERSION = 1
KEEP_DAYS = 7
# 每次删掉这些天之前的（多删几天，漏跑或某天没删掉的也能清掉）
_PRUNE_DAYS = range(KEEP_DAYS + 1, KEEP_DAYS + 11)
_BATCH = 1000
PREFIX = "public-snapshots/"

# 导出：政府公开发布的内容，以及据此整理、核对出的结果（不含任何用户提交的内容）
PUBLIC_TABLES = frozenset(
    {
        # 来源和抓取记录
        "sources",
        "source_checks",
        # 通知、正文、附件
        "documents",
        "document_details",
        "document_attachments",
        "document_merges",
        "attachment_copies",
        "attachment_text_versions",
        "attachment_text_segments",
        "attachment_text_urls",
        # 截止日期、字段、申报条件、转发归组
        "document_deadlines",
        "document_fields",
        "condition_lists",
        "condition_items",
        "repost_groups",
        "repost_group_members",
        # 名单
        "list_entries",
        "list_parses",
        "list_evidence_sources",
        "list_entry_locations",
        # AI 对通知的质检和"能不能做 AI 解读"
        "ai_checks",
        "ai_eligibility",
        # 公众号周报的方案（期刊本身不导出，见下）
        "digest_profiles",
    }
)

# 不导出：用户数据、密钥、日志和运维记录
EXCLUDED_TABLES = frozenset(
    {
        # 用户数据
        "users",
        "follows",
        "follow_repost_notifications",
        "feedback",
        "deadline_clues",
        "subscriptions",
        "events",
        "advisor_tasks",
        "advisor_usage",
        "advisor_file_trash",
        "boost_trials",
        "pass_rewards",
        "workbench_passes",
        "pay_orders",
        "pay_events",
        "invoice_requests",
        # 密钥
        "app_keys",
        "signing_keys",
        # 下架、恢复的操作记录；周报期刊（正文里有当时的文件标题和链接，可能包括后来下架的）
        "document_takedown_events",
        "digest_issues",
        # 访问日志和运维记录
        "access_logs",
        "access_daily",
        "ai_heartbeat",
        "ai_usage",
        "job_runs",
        "pay_reconciliations",
    }
)

# 字段：人工填写、可能写到投诉人的备注，有内容的导出时清空（可为空的写 NULL，不可为空的写空串）
CLEARED_COLUMNS: dict[str, tuple[str, ...]] = {
    "documents": ("takedown_note", "restore_note"),
}

# 用 document_id 指向通知的公开表：下架文件的行不导出
_DOCUMENT_CHILDREN = frozenset(
    {
        "ai_checks",
        "ai_eligibility",
        "condition_lists",
        "document_attachments",
        "document_details",
        "document_fields",
        "list_entries",
        "list_parses",
        "repost_group_members",
    }
)


@dataclass(frozen=True)
class _LeftOut:
    """这次不导出的：下架的文件，以及只被它们用到的附件链接、附件文字版本、名单文件和转发组。
    在导出的同一个可重复读事务里算一次，条数和导出的行用的是同一份。"""

    documents: frozenset[int] = frozenset()
    attachment_urls: frozenset[str] = frozenset()
    text_versions: frozenset[int] = frozenset()
    list_urls: frozenset[str] = frozenset()
    repost_groups: frozenset[int] = frozenset()

    @classmethod
    def compute(cls, conn: Connection) -> "_LeftOut":
        docs = frozenset(conn.scalars(select(Document.id).where(~available_condition(Document))))
        if not docs:
            return cls()
        tables = Base.metadata.tables

        def only_hidden(url, document_id) -> frozenset[str]:
            # 空网址不算：NOT IN 里有 NULL 时一行也选不出来
            hidden = set(
                conn.scalars(select(url).where(document_id.in_(docs), url.is_not(None)).distinct())
            )
            if not hidden:
                return frozenset()
            public = conn.scalars(
                select(url).where(url.in_(hidden), document_id.not_in(docs)).distinct()
            )
            return frozenset(hidden - set(public))

        da, lp = tables["document_attachments"], tables["list_parses"]
        attachment_urls = only_hidden(da.c.url, da.c.document_id)
        list_urls = only_hidden(lp.c.source_url, lp.c.document_id)
        versions: set[int] = set()
        if attachment_urls:
            atu = tables["attachment_text_urls"]
            versions = set(
                conn.scalars(
                    select(atu.c.version_id)
                    .where(atu.c.url.in_(attachment_urls), atu.c.version_id.is_not(None))
                    .distinct()
                )
            )
            if versions:
                versions -= set(
                    conn.scalars(
                        select(atu.c.version_id)
                        .where(atu.c.version_id.in_(versions), atu.c.url.not_in(attachment_urls))
                        .distinct()
                    )
                )
        rgm = tables["repost_group_members"]
        touched = set(
            conn.scalars(select(rgm.c.group_id).where(rgm.c.document_id.in_(docs)).distinct())
        )
        if touched:
            touched -= set(
                conn.scalars(
                    select(rgm.c.group_id)
                    .where(rgm.c.group_id.in_(touched), rgm.c.document_id.not_in(docs))
                    .distinct()
                )
            )
        return cls(docs, attachment_urls, frozenset(versions), list_urls, frozenset(touched))


def _not_in(column, values):
    """column 不在 values 里；列可为空时空值照常保留（NOT IN 遇到 NULL 会把这一行也排除掉）。"""
    condition = column.not_in(values)
    return or_(column.is_(None), condition) if column.nullable else condition


def _row_filter(table: Table, left: _LeftOut):
    """这张表要导出的行的条件；None 表示整张表都导出。"""
    tables = Base.metadata.tables
    c, name, docs = table.c, table.name, sorted(left.documents)
    keep = []
    if name == "document_deadlines":
        keep.append(c.origin != "user")
    if docs:
        if name == "documents":
            keep.append(_not_in(c.id, docs))
        elif name == "document_merges":
            keep += [_not_in(c.canonical_id, docs), _not_in(c.duplicate_id, docs)]
        elif name == "document_deadlines":
            keep += [_not_in(c.document_id, docs), _not_in(c.changed_to_document_id, docs)]
        elif name in _DOCUMENT_CHILDREN:
            keep.append(_not_in(c.document_id, docs))
        elif name == "condition_items":
            lists = tables["condition_lists"]
            hidden = select(lists.c.id).where(lists.c.document_id.in_(docs))
            keep.append(_not_in(c.list_id, hidden))
        elif name == "list_entry_locations":
            entries = tables["list_entries"]
            hidden = select(entries.c.id).where(entries.c.document_id.in_(docs))
            keep.append(_not_in(c.entry_id, hidden))
    if name in ("attachment_copies", "attachment_text_urls") and left.attachment_urls:
        keep.append(_not_in(c.url, sorted(left.attachment_urls)))
    if name == "attachment_text_versions" and left.text_versions:
        keep.append(_not_in(c.id, sorted(left.text_versions)))
    if name == "attachment_text_segments" and left.text_versions:
        keep.append(_not_in(c.version_id, sorted(left.text_versions)))
    if name == "list_evidence_sources" and left.list_urls:
        keep.append(_not_in(c.url, sorted(left.list_urls)))
    if name == "list_entry_locations" and left.list_urls:
        sources = tables["list_evidence_sources"]
        hidden = select(sources.c.id).where(sources.c.url.in_(sorted(left.list_urls)))
        keep.append(_not_in(c.source_id, hidden))
    if name == "repost_groups" and left.repost_groups:
        keep.append(_not_in(c.id, sorted(left.repost_groups)))
    return and_(*keep) if keep else None


def public_tables() -> list[Table]:
    return [t for t in Base.metadata.sorted_tables if t.name in PUBLIC_TABLES]


@dataclass
class SnapshotReport:
    counts: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    path: str = ""
    file_id: str = ""
    pruned: int = 0


def object_path(day: date) -> str:
    return f"{PREFIX}youju-public-{day:%Y%m%d}.jsonl.gz"


def _count(conn: Connection, table: Table, left: _LeftOut) -> int:
    stmt = select(func.count()).select_from(table)
    condition = _row_filter(table, left)
    if condition is not None:
        stmt = stmt.where(condition)
    return conn.scalar(stmt) or 0


def _rows(conn: Connection, table: Table, left: _LeftOut) -> Iterator[dict]:
    stmt = select(table)
    condition = _row_filter(table, left)
    if condition is not None:
        stmt = stmt.where(condition)
    blanks = {
        name: (None if table.c[name].nullable else "")
        for name in CLEARED_COLUMNS.get(table.name, ())
    }
    result = conn.execution_options(stream_results=True, yield_per=_BATCH).execute(
        stmt.order_by(*table.primary_key.columns)
    )
    for row in result.mappings():
        item = dict(row)
        for name, blank in blanks.items():
            if item[name]:
                item[name] = blank
        yield item


def dump(engine: Engine, out: IO[bytes], *, now: datetime | None = None) -> dict[str, int]:
    """把公开内容表按主键顺序导出到 out（gzip JSON Lines），返回各表条数。"""
    tables = public_tables()
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        # 先定不导出哪些、再数条数：可重复读下，后面读到的和这里算的是同一时刻的数据
        left = _LeftOut.compute(conn)
        counts = {t.name: _count(conn, t, left) for t in tables}
        header = {
            "format": FORMAT,
            "version": VERSION,
            "revision": _revision(conn),
            "code_revision": build_revision(),
            "created_at": (now or datetime.now(UTC)).isoformat(),
            "tables": counts,
            "not_exported": sorted(EXCLUDED_TABLES),
            "rows_left_out": {
                "documents": (
                    f"下架的文件 {len(left.documents)} 份（和小程序同一口径）及挂在它们下面的行"
                ),
                "document_deadlines": "origin = 'user'（用户线索生成的）",
            },
            "cleared": {name: list(cols) for name, cols in CLEARED_COLUMNS.items()},
        }
        digests: dict[str, str] = {}
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            gz.write((json.dumps(header, ensure_ascii=False) + "\n").encode())
            for table in tables:
                digest = hashlib.sha256()
                for row in _rows(conn, table, left):
                    line = _line(table.name, row)
                    digest.update(line)
                    gz.write(line)
                digests[table.name] = digest.hexdigest()
            gz.write((json.dumps({"end": True, "sha256": digests}) + "\n").encode())
    return counts


def read_header(src: IO[bytes]) -> dict:
    """只读说明行（本机脚本用来看数据时间和版本）。"""
    try:
        with gzip.GzipFile(fileobj=src, mode="rb") as gz:
            header = json.loads(gz.readline() or b"null")
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise BackupError("公开快照文件损坏或不完整") from exc
    if (
        not isinstance(header, dict)
        or header.get("format") != FORMAT
        or header.get("version") != VERSION
    ):
        raise BackupError("不是有据的公开快照文件，或格式版本不认识")
    return header


def restore(engine: Engine, src: IO[bytes]) -> dict[str, int]:
    """把公开快照导入 engine 指向的库：这个库要已迁移到和快照相同的版本，公开内容表都是空的。
    锁表后检查、写入、核对（条数、外键、各表 SHA-256），全部对上才提交；否则回滚并报错。"""
    tables = {t.name: t for t in public_tables()}
    parent_locks = [
        f"`{table.name}` AS `{table.name}_{fk.parent.name}_parent` READ"
        for table in tables.values()
        for fk in table.foreign_keys
        if fk.column.table is table
    ]
    locks = ", ".join(
        [f"`{name}` WRITE" for name in tables] + parent_locks + ["`alembic_version` READ"]
    )
    try:
        with gzip.GzipFile(fileobj=src, mode="rb") as gz:
            header = json.loads(gz.readline() or b"null")
            if (
                not isinstance(header, dict)
                or header.get("format") != FORMAT
                or header.get("version") != VERSION
            ):
                raise BackupError("不是有据的公开快照文件，或格式版本不认识")
            counts_ok = isinstance(header.get("tables"), dict) and all(
                isinstance(k, str) and type(v) is int and v >= 0
                for k, v in header["tables"].items()
            )
            if not counts_ok or not isinstance(header.get("revision"), (str, type(None))):
                raise BackupError("公开快照的说明行格式不对")
            with engine.connect() as conn:
                # 锁住全部公开内容表：检查空库到写完提交之间，别的程序写不进来
                conn.execute(text(f"LOCK TABLES {locks}"))
                try:
                    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
                    trailer = _load(conn, gz, header, tables)
                    counts = _verify(conn, header, trailer, tables)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    # 会话级设置一定改回来，免得这个连接回到连接池后一直不查外键
                    conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
                    conn.execute(text("UNLOCK TABLES"))
                    conn.commit()
    except BackupError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError, zlib.error) as exc:
        raise BackupError("公开快照文件损坏或不完整") from exc
    except SQLAlchemyError as exc:
        raise BackupError(f"写入目标库失败：{exc.__class__.__name__}") from exc
    return counts


def _prune(client: httpx.Client, env: str, file_id: str, path: str, today: date) -> int:
    """删掉 8–17 天前的公开快照，返回删掉的个数（本来就没有的不算错）。"""
    prefix = file_id[: -len(path)] if file_id.endswith(path) else None
    if not prefix:
        raise BackupError("文件 ID 的格式和预期不同，没有删除旧的公开快照")
    old = [prefix + object_path(today - timedelta(days=d)) for d in _PRUNE_DAYS]
    return delete_files(client, env, old, what="旧的公开快照")


def run(
    engine: Engine,
    env: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = clock.sleep,
) -> SnapshotReport:
    """导出公开内容、上传到对象存储、删掉 7 天前的（网络中断、服务繁忙时重试两次）。
    失败抛 BackupError（由 job.sh 发提醒）。"""
    now = now or datetime.now(UTC)
    today = now.astimezone(BEIJING).date()
    report = SnapshotReport(path=object_path(today))
    own = client is None
    client = client or httpx.Client(trust_env=False)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / Path(report.path).name
            with local.open("wb") as out:
                report.counts = dump(engine, out, now=now)
            report.bytes = local.stat().st_size
            report.file_id = _retrying(
                lambda: upload(client, env, report.path, local, what="公开快照"), sleep
            )
        try:
            report.pruned = _retrying(
                lambda: _prune(client, env, report.file_id, report.path, today), sleep
            )
        except BackupError as exc:
            raise BackupError(f"今天的公开快照已上传，但删除 7 天前的失败：{exc}") from exc
    finally:
        if own:
            client.close()
    return report


def describe(header: dict[str, Any]) -> str:
    """给人看的一行：数据是北京时间哪天几点的、多少条、哪个迁移版本。"""
    created = datetime.fromisoformat(header["created_at"]).astimezone(BEIJING)
    total = sum(header.get("tables", {}).values())
    return (
        f"线上公开内容 {created:%m/%d %H:%M}（北京时间），共 {total} 条，"
        f"迁移版本 {header.get('revision')}"
    )
