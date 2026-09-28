"""表格名称列 AI 识别测试。"""

import httpx

from youju.ai.client import ChatClient
from youju.ai.tables import pick_name_column


def _client(content: str, seen: dict[str, str] | None = None) -> ChatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["body"] = request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return ChatClient(
        "https://example.test",
        "key",
        "qwen",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_picks_name_column_and_maps_past_person_column() -> None:
    seen: dict[str, str] = {}
    client = _client('{"column": 1, "reason": "企业名称"}', seen)
    result = pick_name_column(
        client,
        "企业名单",
        ["联系人", "统一社会信用代码", "企业名称", "地区"],
        [
            [
                "张三 13800138000 身份证11010519491231002X",
                "91320000ABCDEF1234",
                "江苏示例公司",
                "江苏省",
            ],
            ["李四", "91320000ABCDEF5678", "上海示例集团", "上海市"],
        ],
    )
    assert result == 2
    body = seen["body"]
    assert "联系人" not in body and "张三" not in body
    assert "13800138000" not in body and "11010519491231002X" not in body
    assert "企业名称" in body


def test_out_of_range_or_null_is_none() -> None:
    rows = [["示例公司"], ["另一家公司"]]
    assert pick_name_column(_client('{"column": 4, "reason": ""}'), "名单", ["名称"], rows) is None
    assert (
        pick_name_column(_client('{"column": null, "reason": "不确定"}'), "名单", ["名称"], rows)
        is None
    )


def test_column_with_mostly_non_org_values_is_rejected() -> None:
    client = _client('{"column": 0, "reason": "名称"}')
    rows = [["北京"], ["上海"], ["广州"], ["深圳公司"]]
    assert pick_name_column(client, "名单", ["名称"], rows) is None


def test_fewer_than_two_rows_does_not_call_ai() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client = ChatClient(
        "https://example.test", "key", "qwen", transport=httpx.MockTransport(handler)
    )
    assert pick_name_column(client, "名单", ["名称"], [["示例公司"]]) is None
    assert calls == 0


def test_unlabeled_person_name_column_is_not_sent() -> None:
    """没有表头、但多数值像人名的列也不发给 AI。"""
    import json

    seen: dict[str, str] = {}
    client = _client('{"column": 0, "reason": "企业名称"}', seen)
    result = pick_name_column(
        client,
        "拟认定企业名单",
        ["", ""],
        [
            ["王小明", "山东示例科技有限公司"],
            ["李红", "烟台示例电子股份有限公司"],
            ["赵强", "济南示例研究院"],
        ],
    )
    assert result == 1  # 人名列被去掉后，保留列 0 映射回原表第 1 列
    sent = json.loads(seen["body"])["messages"][1]["content"]
    assert "王小明" not in sent and "李红" not in sent and "赵强" not in sent
