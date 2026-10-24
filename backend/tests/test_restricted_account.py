"""受限测试账号按测试库名区分：几个会话各用自己的库同时跑，账号不互相建失败或删掉。"""

import re

from tests.conftest import restricted_account_name

VALID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def test_default_database_keeps_old_name():
    assert restricted_account_name("youju_test") == "youju_api_test"


def test_different_databases_get_different_names():
    a = restricted_account_name("youju_test_lockfix_a")
    b = restricted_account_name("youju_test_lockfix_b")
    assert a == "youju_api_test_lockfix_a"
    assert b == "youju_api_test_lockfix_b"
    assert a != b


def test_same_database_same_name():
    long_db = "youju_test_" + "x" * 50
    assert restricted_account_name("youju_test_a") == restricted_account_name("youju_test_a")
    assert restricted_account_name(long_db) == restricted_account_name(long_db)


def test_long_names_are_hashed_and_valid():
    longs = ["youju_test_" + "x" * 50, "youju_test_" + "x" * 49 + "y", "youju_test_" + "a" * 18]
    names = [restricted_account_name(db) for db in longs]
    for name in names:
        assert len(name) <= 32 and VALID.fullmatch(name)
        assert name.startswith("youju_api_test-")
    assert len(set(names)) == len(names)
    # 正好 32 个字符的不用摘要；摘要那种带"-"，不会和直接拼出来的名字撞
    exact = restricted_account_name("youju_test_" + "a" * 17)
    assert len(exact) == 32 and "-" not in exact
