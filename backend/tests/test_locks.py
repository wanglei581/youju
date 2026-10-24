"""MySQL 命名锁的锁名（youju.db.lock_name）。

命名锁在整台 MySQL 服务器上生效、不分库。线上锁名一个字都不能变：发布时新旧版本同时跑约 15 分钟，
靠同一个锁名互相拦着。测试按测试库名加前缀（tests/conftest.py），几个会话各用自己的测试库同时跑，
同名的锁不再互相抢（10/23 两条附件测试因此偶发失败）。
"""

import os
import subprocess
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, event, make_url, text

import youju
from youju import advisor_tasks, migrations
from youju import attachments as copies
from youju.ai import eligibility
from youju.api import app as app_module
from youju.db import lock_name

from .conftest import TEST_DB_URL

DB = make_url(TEST_DB_URL).database
# 另一个会话的测试库，锁名前缀按 tests/conftest.py 的规矩是"库名:"
OTHER_PREFIX = "youju_test_other_session:"


def _attachments(session) -> bool:
    with copies._exclusive(session) as got:
        return got


def _ai_cap(session) -> bool:
    try:
        with advisor_tasks.ai_cap_lock(session):
            return True
    except advisor_tasks.AdvisorBusy:
        return False


def _eligibility(session) -> bool:
    with eligibility.writer_lock(session, 0) as got:
        return got


def _source_create(session) -> bool:
    try:
        with app_module._source_create_lock(session):
            return True
    except HTTPException:
        return False


def _migration(session) -> bool:
    try:
        migrations.upgrade(TEST_DB_URL)
        return True
    except RuntimeError:
        return False


# 每处命名锁：用代码里的原函数拿锁（拿到给 True，被别人拿着给 False），和它线上的锁名
SITES = {
    "attachments": (_attachments, "youju_attachment_copies"),
    "ai_cap": (_ai_cap, "youju_ai_advisor_daily_cap"),
    "eligibility": (_eligibility, f"youju_ai_eligibility:{DB}"[:64]),
    "source_create": (_source_create, "youju_source_create"),
    "migration": (_migration, "youju_migration"),
}


@contextmanager
def _sent_lock_names():
    """记下代码发给 MySQL 的锁名（GET_LOCK、RELEASE_LOCK），发出去之前换成本测试库专用的名字：
    不设前缀时代码用的就是线上的锁名，它在整台服务器上生效，测试不去占（别的会话可能正用着）。"""
    sent: list[tuple[str, ...]] = []

    def swap(conn, cursor, statement, parameters, context, executemany):
        call = next((f for f in ("GET_LOCK", "RELEASE_LOCK") if f + "(" in statement), None)
        if call is None:
            return statement, parameters
        sent.append((call, *(v for v in parameters.values() if isinstance(v, str))))
        private = {
            key: sha256(f"{DB}:{value}".encode()).hexdigest() if isinstance(value, str) else value
            for key, value in parameters.items()
        }
        return statement, private

    event.listen(Engine, "before_cursor_execute", swap, retval=True)
    try:
        yield sent
    finally:
        event.remove(Engine, "before_cursor_execute", swap)


@pytest.mark.parametrize("site", SITES)
def test_lock_names_unchanged_without_prefix(site, session, monkeypatch) -> None:
    """线上不设 YOUJU_LOCK_PREFIX：每处发给 MySQL 的锁名和原来一字不差。"""
    acquire, original = SITES[site]
    monkeypatch.delenv("YOUJU_LOCK_PREFIX")
    with _sent_lock_names() as sent:
        assert acquire(session)
    assert sent == [("GET_LOCK", original), ("RELEASE_LOCK", original)]


@pytest.mark.parametrize("site", SITES)
def test_tests_prefix_lock_names_with_their_database(site, session) -> None:
    """测试里（tests/conftest.py 按测试库名设了前缀）每处锁名前都带着测试库名。"""
    acquire, original = SITES[site]
    assert os.environ["YOUJU_LOCK_PREFIX"] == f"{DB}:"
    with _sent_lock_names() as sent:
        assert acquire(session)
    assert sent == [("GET_LOCK", lock_name(original)), ("RELEASE_LOCK", lock_name(original))]


@pytest.mark.parametrize("site", SITES)
def test_same_lock_in_another_test_database_does_not_block(
    site, session, db_engine, monkeypatch
) -> None:
    """另一个会话在它的测试库里拿着同名的锁（前缀是它的库名）：这个库照样拿得到；
    同一个库里照样互相拦着（等锁时间改成 0，不用干等；AI 每日上限固定等 2 秒）。"""
    acquire, original = SITES[site]
    monkeypatch.setattr(app_module, "_CREATE_LOCK_WAIT", 0)
    monkeypatch.setattr(migrations, "_LOCK_WAIT_SECONDS", 0)
    own = lock_name(original)
    with monkeypatch.context() as other_session:
        other_session.setenv("YOUJU_LOCK_PREFIX", OTHER_PREFIX)
        theirs = lock_name(original)
    assert theirs != own
    with db_engine.connect() as other:
        try:
            assert other.scalar(text("SELECT GET_LOCK(:k, 0)"), {"k": theirs}) == 1
            assert acquire(session)
            assert other.scalar(text("SELECT GET_LOCK(:k, 0)"), {"k": own}) == 1
            assert not acquire(session)
        finally:
            other.execute(text("SELECT RELEASE_ALL_LOCKS()"))
    assert acquire(session)


def test_lock_name(db_engine, monkeypatch) -> None:
    monkeypatch.delenv("YOUJU_LOCK_PREFIX")
    assert lock_name("youju_migration") == "youju_migration"
    monkeypatch.setenv("YOUJU_LOCK_PREFIX", "")
    assert lock_name("youju_migration") == "youju_migration"
    monkeypatch.setenv("YOUJU_LOCK_PREFIX", "youju_test_a:")
    assert lock_name("youju_migration") == "youju_test_a:youju_migration"
    # MySQL 锁名最长 64 个字符：太长时换成摘要，还是一个名字一个，MySQL 照样认
    monkeypatch.setenv("YOUJU_LOCK_PREFIX", "youju_test_" + "x" * 50 + ":")
    long_a, long_b = lock_name("youju_migration"), lock_name("youju_source_create")
    assert len(long_a) == len(long_b) == 64 and long_a != long_b
    assert lock_name("youju_migration") == long_a
    with db_engine.connect() as conn:
        assert conn.scalar(text("SELECT GET_LOCK(:k, 0)"), {"k": long_a}) == 1
        assert conn.scalar(text("SELECT RELEASE_LOCK(:k)"), {"k": long_a}) == 1


def test_every_named_lock_goes_through_lock_name() -> None:
    """新加的命名锁也要用 youju.db.lock_name 取名（不然几个测试库同时跑又会互相抢），
    并加进上面的 SITES，让线上锁名也有人看着。"""
    src = Path(youju.__file__).parent
    found = {}
    for path in sorted(src.rglob("*.py")):
        count = path.read_text(encoding="utf-8").count("GET_LOCK(")
        if count:
            found[path.relative_to(src).as_posix()] = count
    assert found == {
        "advisor_tasks.py": 1,
        "ai/eligibility.py": 1,
        "api/app.py": 1,
        "attachments.py": 1,
        "migrations.py": 1,
    }


def test_start_script_refuses_lock_prefix() -> None:
    """线上（deploy/start.sh）设了 YOUJU_LOCK_PREFIX 不启动：锁名一变，发布时新旧版本互相拦不住。"""
    start = Path(__file__).parents[1] / "deploy" / "start.sh"
    done = subprocess.run(
        ["sh", str(start)],
        env={"PATH": os.environ["PATH"], "YOUJU_LOCK_PREFIX": "youju_test:"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 1 and "YOUJU_LOCK_PREFIX" in done.stderr
