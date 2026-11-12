from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from youju.models import Base


def test_migrations_match_models(db_engine) -> None:
    """迁移脚本建出的表结构必须和模型定义一致，防止改了模型忘记写迁移。"""
    with db_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def _versions(conn) -> list[str]:
    from sqlalchemy import text

    return sorted(conn.scalars(text("SELECT version_num FROM alembic_version")))


def _set_versions(conn, versions: list[str]) -> None:
    """alembic_version 一行一个迁移号（主键）：整表换掉再逐行写回，代码有多个 head 时也成立。"""
    from sqlalchemy import text

    conn.execute(text("DELETE FROM alembic_version"))
    for version in versions:
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": version})


def _code_heads() -> list[str]:
    from alembic.script import ScriptDirectory

    from youju.migrations import alembic_config

    from .conftest import TEST_DB_URL

    return sorted(ScriptDirectory.from_config(alembic_config(TEST_DB_URL)).get_heads())


def test_upgrade_skips_when_the_database_is_ahead_of_the_code(db_engine) -> None:
    """库里的迁移号这版代码不认识（刚把 admin 回退到旧版本）：不迁移、不报错，返回两边的迁移号；
    原来 alembic 报"找不到这个版本"，启动脚本直接退出，回退的 admin 起不来。"""
    from youju.migrations import AheadOfCode, upgrade

    from .conftest import TEST_DB_URL

    with db_engine.begin() as conn:
        saved = _versions(conn)
        _set_versions(conn, ["9999_future"])
    try:
        assert upgrade(TEST_DB_URL) == AheadOfCode(
            unknown=["9999_future"], known=[], code_heads=_code_heads()
        )
        with db_engine.connect() as conn:
            assert _versions(conn) == ["9999_future"]
    finally:
        with db_engine.begin() as conn:
            _set_versions(conn, saved)
    # 恢复后照常：已是最新结构，什么也不做
    assert upgrade(TEST_DB_URL) is None
    with db_engine.connect() as conn:
        assert _versions(conn) == saved == _code_heads()


def test_upgrade_reports_known_heads_when_heads_are_mixed(db_engine) -> None:
    """库里有两个迁移号、一个这版代码认识、一个不认识：一样不迁移，提醒里两个都写出来。"""
    from youju.migrations import upgrade

    from .conftest import TEST_DB_URL

    with db_engine.begin() as conn:
        saved = _versions(conn)
        _set_versions(conn, [*saved, "9999_future"])
    try:
        ahead = upgrade(TEST_DB_URL)
        assert ahead is not None
        assert (ahead.unknown, ahead.known, ahead.code_heads) == (
            ["9999_future"],
            saved,
            _code_heads(),
        )
        with db_engine.connect() as conn:
            assert _versions(conn) == sorted([*saved, "9999_future"])
    finally:
        with db_engine.begin() as conn:
            _set_versions(conn, saved)
    assert upgrade(TEST_DB_URL) is None


def test_db_upgrade_alerts_and_still_succeeds_when_the_database_is_ahead(monkeypatch, capsys):
    from youju import alerts, cli, migrations

    ahead = migrations.AheadOfCode(unknown=["0099"], known=["0032"], code_heads=["0032"])
    monkeypatch.setattr(migrations, "upgrade", lambda: ahead)
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    assert cli.main(["db-upgrade"]) == 0
    assert len(sent) == 1
    assert "0099" in sent[0] and "比这版代码新" in sent[0] and "这版代码最新是 0032" in sent[0]
    assert "库里另有这版代码认识的迁移版本 0032" in sent[0]
    assert "没有迁移" in capsys.readouterr().out
    # 提醒没发出去、甚至直接出异常：也照常返回成功（启动脚本 set -e，失败就起不来了）
    monkeypatch.setattr(alerts, "send_alert", lambda message: False)
    assert cli.main(["db-upgrade"]) == 0

    def boom(message):
        raise RuntimeError("机器人地址配错了")

    monkeypatch.setattr(alerts, "send_alert", boom)
    assert cli.main(["db-upgrade"]) == 0
    # 平常（库不比代码新）：不提醒
    monkeypatch.setattr(migrations, "upgrade", lambda: None)
    monkeypatch.setattr(alerts, "send_alert", lambda message: sent.append(message) or True)
    assert cli.main(["db-upgrade"]) == 0 and len(sent) == 1


def test_repost_signals_migration_upgrade_downgrade(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "alembic/versions/0054_repost_signals.py"
    spec = importlib.util.spec_from_file_location("repost_signals_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    calls = []
    monkeypatch.setattr(
        migration.op, "create_table", lambda name, *cols: calls.append((name, cols))
    )
    monkeypatch.setattr(migration.op, "drop_table", lambda name: calls.append((name,)))
    executed = []
    monkeypatch.setattr(migration.op, "execute", executed.append)
    migration.upgrade()
    migration.downgrade()
    assert migration.down_revision == "0053"
    # 现有文件都补一行"处理过"的标记（同步据此放行）
    assert len(executed) == 1 and "INSERT INTO repost_signals" in executed[0]
    assert "FROM documents" in executed[0]
    assert calls[0][0] == calls[1][0] == "repost_signals"
    assert {col.name for col in calls[0][1]} == {
        "document_id",
        "cited",
        "body_forward",
        "has_body",
        "body_at",
        "grouped_at",
        "computed_at",
    }


def test_repost_signals_database_downgrade_and_upgrade(db_engine):
    from alembic import command
    from sqlalchemy import inspect, text

    from youju.migrations import alembic_config

    from .conftest import TEST_DB_URL

    config = alembic_config(TEST_DB_URL)
    try:
        command.downgrade(config, "0053")
        assert "repost_signals" not in inspect(db_engine).get_table_names()
        # 迁移前已有的文件：升级时补一行"处理过"的标记（同步据此放行，不然老文件会被扣住）
        with db_engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            conn.execute(text("DELETE FROM documents"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            conn.execute(
                text(
                    "INSERT INTO sources (level, province, dept, dept_full_name, column_name, url,"
                    " fetch_mode, enabled, parser_config, rights_level, health,"
                    " consecutive_failures) VALUES ('省级', '陕西', '工信', '陕西省工信厅',"
                    " '通知公告', 'https://migr.example.gov.cn/tzgg/', 'static', 1, '{}',"
                    " 1, 'unknown', 0)"
                )
            )
            source_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            for n in (1, 2):
                conn.execute(
                    text(
                        "INSERT INTO documents (source_id, url, url_key, title, first_seen_at,"
                        " last_seen_at, is_baseline, status) VALUES (:s, :u, :k, '迁移前的文件',"
                        " UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 0, 'discovered')"
                    ),
                    {"s": source_id, "u": f"https://migr.example.gov.cn/{n}", "k": f"migr/{n}"},
                )
    finally:
        command.upgrade(config, "head")
    assert "repost_signals" in inspect(db_engine).get_table_names()
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT s.cited, s.has_body, s.body_at, s.grouped_at FROM repost_signals s"
                " JOIN documents d ON d.id = s.document_id WHERE d.url_key LIKE 'migr/%'"
            )
        ).all()
    assert len(rows) == 2
    assert all(r.cited in ("[]", []) and not r.has_body and r.body_at is None for r in rows)
    assert all(r.grouped_at is not None for r in rows)
