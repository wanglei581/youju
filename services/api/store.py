"""SQLite persistence for the local industry-library pilot.

Imports create draft versions. A deliberate admin publish action changes the
version visible to readers; an import never silently changes public answers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


RIGHTS = {"directory_only", "short_excerpt", "evidence_use"}
KINDS = {"policy", "application", "list", "statistics", "report", "other"}
IMPORT_FIELDS = (
    "title", "kind", "industry", "region", "published_at", "original_url",
    "summary", "answer", "evidence_locator",
)


class InputError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: object, maximum: int, field: str, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise InputError(f"{field} 不能为空")
    if len(text) > maximum:
        raise InputError(f"{field} 最多 {maximum} 字")
    if text.startswith(("=", "+", "-", "@")) and field not in ("published_at",):
        raise InputError(f"{field} 不能以表格公式符号开头")
    return text


def clean_url(value: object, field: str = "网址") -> str:
    url = clean_text(value, 1200, field, required=True)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise InputError(f"{field} 必须是无账号密码的 HTTPS 地址")
    return url


def connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def init_db(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sources (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      publisher TEXT NOT NULL,
      homepage_url TEXT NOT NULL,
      rights_level TEXT NOT NULL CHECK(rights_level IN ('directory_only','short_excerpt','evidence_use')),
      rights_basis TEXT NOT NULL DEFAULT '',
      rights_checked_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS import_batches (
      id TEXT PRIMARY KEY,
      source_id INTEGER NOT NULL REFERENCES sources(id),
      filename TEXT NOT NULL,
      rows_json TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('previewed','committed')),
      created_at TEXT NOT NULL,
      committed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS records (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source_id INTEGER NOT NULL REFERENCES sources(id),
      original_url TEXT NOT NULL,
      published_version_id INTEGER,
      draft_version_id INTEGER,
      created_at TEXT NOT NULL,
      UNIQUE(source_id, original_url)
    );
    CREATE TABLE IF NOT EXISTS record_versions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      record_id INTEGER NOT NULL REFERENCES records(id),
      version_no INTEGER NOT NULL,
      title TEXT NOT NULL,
      kind TEXT NOT NULL,
      industry TEXT NOT NULL,
      region TEXT NOT NULL,
      published_at TEXT NOT NULL,
      summary TEXT NOT NULL,
      answer TEXT NOT NULL,
      evidence_locator TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      review_status TEXT NOT NULL DEFAULT 'pending',
      review_note TEXT NOT NULL DEFAULT '',
      reviewed_at TEXT,
      import_batch_id TEXT REFERENCES import_batches(id),
      created_at TEXT NOT NULL,
      UNIQUE(record_id, version_no)
    );
    CREATE TABLE IF NOT EXISTS audit_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      action TEXT NOT NULL,
      object_type TEXT NOT NULL,
      object_id TEXT NOT NULL,
      detail_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_versions_record ON record_versions(record_id);
    CREATE INDEX IF NOT EXISTS idx_records_source ON records(source_id);
    """)
    # The copied pilot schema predates source suspension. Preserve local data.
    columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
    if "status" not in columns:
        db.execute("ALTER TABLE sources ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    db.commit()


def audit(db: sqlite3.Connection, action: str, object_type: str, object_id: object, detail: dict | None = None) -> None:
    db.execute(
        "INSERT INTO audit_events(action,object_type,object_id,detail_json,created_at) VALUES (?,?,?,?,?)",
        (action, object_type, str(object_id), json.dumps(detail or {}, ensure_ascii=False), now_iso()),
    )


def list_sources(db: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY id DESC")]


def get_source(db: sqlite3.Connection, source_id: int) -> dict:
    row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise InputError("来源不存在")
    return dict(row)


def create_source(db: sqlite3.Connection, payload: dict) -> dict:
    name = clean_text(payload.get("name"), 160, "来源名称", True)
    publisher = clean_text(payload.get("publisher"), 160, "发布机构", True)
    homepage_url = clean_url(payload.get("homepage_url"), "来源网址")
    rights_level = str(payload.get("rights_level") or "directory_only")
    if rights_level not in RIGHTS:
        raise InputError("资料权限无效")
    rights_basis = clean_text(payload.get("rights_basis"), 600, "权限依据")
    if rights_level != "directory_only" and not rights_basis:
        raise InputError("开放摘要或证据使用前须填写权限依据")
    checked = now_iso()
    cursor = db.execute(
        "INSERT INTO sources(name,publisher,homepage_url,rights_level,rights_basis,rights_checked_at,created_at) VALUES (?,?,?,?,?,?,?)",
        (name, publisher, homepage_url, rights_level, rights_basis, checked, checked),
    )
    audit(db, "source_created", "source", cursor.lastrowid, {"rights_level": rights_level})
    db.commit()
    return get_source(db, cursor.lastrowid)


def restrict_source(db: sqlite3.Connection, source_id: int, mode: str, reason: str) -> dict:
    """Only reduces access. Restoring rights requires a new reviewed process."""
    source = get_source(db, source_id)
    note = clean_text(reason, 600, "限制原因", True)
    if mode not in ("directory_only", "suspend"):
        raise InputError("限制方式须为 directory_only 或 suspend")
    with db:
        if mode == "suspend":
            db.execute("UPDATE sources SET status='suspended',rights_checked_at=? WHERE id=?", (now_iso(), source_id))
        else:
            if source["status"] != "active":
                raise InputError("已暂停来源不能再改为仅题录")
            db.execute("UPDATE sources SET rights_level='directory_only',rights_checked_at=? WHERE id=?", (now_iso(), source_id))
        audit(db, "source_restricted", "source", source_id, {"mode": mode, "reason": note})
    return get_source(db, source_id)


def validate_import_row(raw: dict, rights_level: str) -> dict:
    row = {
        "title": clean_text(raw.get("title"), 300, "标题", True),
        "kind": clean_text(raw.get("kind"), 40, "类型", True).lower(),
        "industry": clean_text(raw.get("industry"), 100, "行业"),
        "region": clean_text(raw.get("region"), 100, "地区"),
        "published_at": clean_text(raw.get("published_at"), 40, "发布日期"),
        "original_url": clean_url(raw.get("original_url"), "原站网址"),
        "summary": clean_text(raw.get("summary"), 300, "摘要"),
        "answer": clean_text(raw.get("answer"), 1000, "答案"),
        "evidence_locator": clean_text(raw.get("evidence_locator"), 300, "证据位置"),
    }
    if row["kind"] not in KINDS:
        raise InputError("类型须为 policy/application/list/statistics/report/other")
    if row["published_at"]:
        try:
            datetime.strptime(row["published_at"], "%Y-%m-%d")
        except ValueError as exc:
            raise InputError("发布日期须为 YYYY-MM-DD") from exc
    if rights_level == "directory_only" and (row["summary"] or row["answer"] or row["evidence_locator"]):
        raise InputError("仅题录来源不得导入摘要、答案或正文证据")
    if rights_level == "short_excerpt" and (row["answer"] or row["evidence_locator"] or len(row["summary"]) > 150):
        raise InputError("短摘来源只能导入 150 字以内摘要")
    if row["answer"] and not row["evidence_locator"]:
        raise InputError("答案必须填写可定位的证据位置")
    return row


def stage_import(db: sqlite3.Connection, source_id: int, filename: str, rows: list[dict]) -> dict:
    source = get_source(db, source_id)
    if source["status"] != "active":
        raise InputError("来源已暂停，不能导入")
    if not rows:
        raise InputError("文件没有资料行")
    if len(rows) > 500:
        raise InputError("单次最多导入 500 行")
    clean_rows = []
    errors = []
    seen = set()
    for number, raw in enumerate(rows, start=2):
        try:
            row = validate_import_row(raw, source["rights_level"])
            key = row["original_url"].lower()
            if key in seen:
                raise InputError("同一个文件中原站网址重复")
            seen.add(key)
            clean_rows.append(row)
        except InputError as exc:
            errors.append({"row": number, "message": str(exc)})
    if errors:
        return {"batch_id": None, "valid_count": len(clean_rows), "errors": errors, "preview": clean_rows[:5]}
    batch_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO import_batches(id,source_id,filename,rows_json,status,created_at) VALUES (?,?,?,?,?,?)",
        (batch_id, source_id, filename, json.dumps(clean_rows, ensure_ascii=False), "previewed", now_iso()),
    )
    audit(db, "import_previewed", "batch", batch_id, {"source_id": source_id, "count": len(clean_rows)})
    db.commit()
    return {"batch_id": batch_id, "valid_count": len(clean_rows), "errors": [], "preview": clean_rows[:5]}


def commit_import(db: sqlite3.Connection, batch_id: str) -> dict:
    batch = db.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise InputError("导入批次不存在")
    if batch["status"] != "previewed":
        raise InputError("此批次已经提交")
    source = get_source(db, batch["source_id"])
    if source["status"] != "active":
        raise InputError("来源已暂停，不能提交导入")
    rows = json.loads(batch["rows_json"])
    inserted = 0
    unchanged = 0
    with db:
        for raw in rows:
            row = validate_import_row(raw, source["rights_level"])
            existing = db.execute(
                "SELECT * FROM records WHERE source_id=? AND original_url=?",
                (source["id"], row["original_url"]),
            ).fetchone()
            if existing is None:
                cursor = db.execute(
                    "INSERT INTO records(source_id,original_url,created_at) VALUES (?,?,?)",
                    (source["id"], row["original_url"], now_iso()),
                )
                record_id = cursor.lastrowid
            else:
                record_id = existing["id"]
            basis = json.dumps(row, sort_keys=True, ensure_ascii=False)
            content_hash = hashlib.sha256(basis.encode("utf-8")).hexdigest()
            latest = db.execute(
                "SELECT content_hash FROM record_versions WHERE record_id=? ORDER BY version_no DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            # After withdrawal there is no public or pending pointer. A same-
            # content reimport must create a new reviewable revision.
            if latest and latest["content_hash"] == content_hash and (
                existing["published_version_id"] is not None or existing["draft_version_id"] is not None
            ):
                unchanged += 1
                continue
            next_no = db.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM record_versions WHERE record_id=?", (record_id,)
            ).fetchone()[0]
            version = db.execute(
                """INSERT INTO record_versions(
                    record_id,version_no,title,kind,industry,region,published_at,summary,answer,
                    evidence_locator,content_hash,import_batch_id,created_at
                  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, next_no, row["title"], row["kind"], row["industry"], row["region"],
                 row["published_at"], row["summary"], row["answer"], row["evidence_locator"],
                 content_hash, batch_id, now_iso()),
            )
            db.execute("UPDATE records SET draft_version_id=? WHERE id=?", (version.lastrowid, record_id))
            inserted += 1
        db.execute("UPDATE import_batches SET status='committed',committed_at=? WHERE id=?", (now_iso(), batch_id))
        audit(db, "import_committed", "batch", batch_id, {"inserted": inserted, "unchanged": unchanged})
    return {"inserted_drafts": inserted, "unchanged": unchanged}


def list_admin_records(db: sqlite3.Connection) -> list[dict]:
    sql = """SELECT r.id,r.original_url,r.published_version_id,r.draft_version_id,
      s.name AS source_name,s.rights_level,
      d.title AS draft_title,d.kind AS draft_kind,d.answer AS draft_answer,
      d.evidence_locator AS draft_evidence_locator,d.version_no AS draft_version_no,
      p.title AS published_title,p.version_no AS published_version_no
      FROM records r JOIN sources s ON s.id=r.source_id
      LEFT JOIN record_versions d ON d.id=r.draft_version_id
      LEFT JOIN record_versions p ON p.id=r.published_version_id
      ORDER BY r.id DESC"""
    return [dict(row) for row in db.execute(sql)]


def publish_record(db: sqlite3.Connection, record_id: int, review_note: str) -> dict:
    note = clean_text(review_note, 600, "审核说明")
    with db:
        record = db.execute(
            """SELECT r.*,s.rights_level,s.status AS source_status,v.answer,v.evidence_locator,v.title
            FROM records r JOIN sources s ON s.id=r.source_id
            LEFT JOIN record_versions v ON v.id=r.draft_version_id WHERE r.id=?""", (record_id,)
        ).fetchone()
        if record is None or record["draft_version_id"] is None:
            raise InputError("没有可发布的待审版本")
        if record["source_status"] != "active":
            raise InputError("来源已暂停，不能发布")
        if record["answer"]:
            if record["rights_level"] != "evidence_use" or not record["evidence_locator"] or not note:
                raise InputError("发布答案需要证据使用权限、位置和审核说明")
            status = "verified"
        else:
            status = "metadata"
        db.execute(
            "UPDATE record_versions SET review_status=?,review_note=?,reviewed_at=? WHERE id=?",
            (status, note, now_iso(), record["draft_version_id"]),
        )
        updated = db.execute(
            "UPDATE records SET published_version_id=?,draft_version_id=NULL WHERE id=? AND draft_version_id=?",
            (record["draft_version_id"], record_id, record["draft_version_id"]),
        )
        if updated.rowcount != 1:
            raise InputError("待审版本已变化，请刷新后重审")
        audit(db, "record_published", "record", record_id, {"version_id": record["draft_version_id"], "status": status})
    return {"record_id": record_id, "title": record["title"], "review_status": status}


def withdraw_record(db: sqlite3.Connection, record_id: int, reason: str) -> dict:
    reason = clean_text(reason, 600, "撤回原因", True)
    row = db.execute("SELECT published_version_id FROM records WHERE id=?", (record_id,)).fetchone()
    if row is None or row["published_version_id"] is None:
        raise InputError("没有已发布版本")
    with db:
        db.execute("UPDATE records SET published_version_id=NULL WHERE id=?", (record_id,))
        audit(db, "record_withdrawn", "record", record_id, {"reason": reason, "version_id": row["published_version_id"]})
    return {"record_id": record_id, "withdrawn": True}


PUBLIC_SQL = """SELECT r.id,r.original_url,s.name AS source_name,s.publisher,s.homepage_url,
  s.rights_level,v.id AS version_id,v.version_no,v.title,v.kind,v.industry,v.region,
  v.published_at,
  CASE WHEN s.rights_level='directory_only' THEN ''
       WHEN s.rights_level='short_excerpt' THEN substr(v.summary,1,150)
       ELSE v.summary END AS summary,
  CASE WHEN s.rights_level='evidence_use' THEN v.answer ELSE '' END AS answer,
  CASE WHEN s.rights_level='evidence_use' THEN v.evidence_locator ELSE '' END AS evidence_locator,
  CASE WHEN s.rights_level='evidence_use' THEN v.review_status ELSE 'metadata' END AS review_status,
  v.reviewed_at
  FROM records r JOIN sources s ON s.id=r.source_id
  JOIN record_versions v ON v.id=r.published_version_id WHERE s.status='active'"""


def public_record(db: sqlite3.Connection, record_id: int) -> dict | None:
    row = db.execute(PUBLIC_SQL + " AND r.id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def public_search(db: sqlite3.Connection, query: str = "", kind: str = "", industry: str = "", region: str = "", limit: int = 50) -> list[dict]:
    clauses = []
    values: list[object] = []
    if query:
        term = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(v.title LIKE ? ESCAPE '\\' OR v.industry LIKE ? ESCAPE '\\' OR v.summary LIKE ? ESCAPE '\\')")
        values.extend((term, term, term))
    if kind:
        clauses.append("v.kind=?")
        values.append(kind)
    if industry:
        clauses.append("v.industry LIKE ?")
        values.append("%" + industry + "%")
    if region:
        clauses.append("v.region LIKE ?")
        values.append("%" + region + "%")
    sql = PUBLIC_SQL + (" AND " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY v.published_at DESC,r.id DESC LIMIT ?"
    values.append(limit)
    return [dict(row) for row in db.execute(sql, values)]


def public_selected(db: sqlite3.Connection, ids: list[int]) -> list[dict]:
    if not ids or len(ids) > 100 or any(not isinstance(item, int) or item < 1 for item in ids):
        raise InputError("请选择 1–100 条有效资料")
    selected = []
    for record_id in dict.fromkeys(ids):
        row = public_record(db, record_id)
        if row is None:
            raise InputError(f"资料 {record_id} 未发布或已撤回")
        selected.append(row)
    return selected
