"""OpenAI 兼容聊天接口客户端。"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx


class AIError(RuntimeError):
    """AI 服务调用失败；错误信息不包含密钥或响应原文。code 是可选的稳定分类。"""

    def __init__(self, message: str, *, retryable: bool = False, code: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def _add_usage(total: dict[str, int], usage: object) -> None:
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                total[key] += value


_JSON_PREFIX = "AI 返回内容不是合法 JSON 对象"


def _json_error(code: str) -> str:
    return f"{_JSON_PREFIX}（{code}）"


def _parse_json_object(envelope: object) -> tuple[dict[str, Any] | None, str | None]:
    """取出 JSON 对象。失败时返回 (None, code)，不抛出带响应正文的异常。

    信封先要有 choices[0].message。finish_reason 缺失或为 None 时沿用原来的内容判断。
    length 一律算截断，哪怕内容刚好能解析成对象。其它显式非 stop 记为 non_stop_finish，
    不把 reason 原文写进错误。不剥围栏、不补括号、不二次解码。
    """
    if not isinstance(envelope, dict):
        return None, "invalid_envelope"
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, "invalid_envelope"
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return None, "invalid_envelope"
    finish = choice.get("finish_reason")
    if finish == "length":
        return None, "truncated"
    if finish is not None and finish != "stop":
        return None, "non_stop_finish"
    content = message.get("content")
    if content is None or (isinstance(content, str) and not content.strip()):
        return None, "empty_content"
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except ValueError:
            return None, "invalid_json"
    else:
        # 旧接口可能已经把 content 解成对象；列表、数字等仍不是对象
        parsed = content
    if not isinstance(parsed, dict):
        return None, "not_object"
    return parsed, None


class ChatClient:
    """调用兼容 OpenAI 格式的 JSON 输出接口。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # 按模型方附加的请求参数，来自配置；
        # 例如 DeepSeek 关闭思考：{"thinking": {"type": "disabled"}}
        self.extra_body = dict(extra_body or {})
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.sleep = sleep
        # 最近一次 complete_json 花掉的用量（失败时也有），调用方据此记账
        self.last_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        # 这个客户端从创建起的累计：调用次数、出错次数、用量（命令结束时记到 AI 用量台账）
        self.totals: dict[str, int] = {
            "calls": 0,
            "errors": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        self._client = httpx.Client(transport=transport, timeout=timeout, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ChatClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def complete_json(self, system: str, user: str, max_tokens: int = 800) -> dict[str, Any]:
        """请求 JSON 对象。暂时性错误和非法 JSON 会重试；截断和非正常结束不重试。用量都累计。"""
        self.totals["calls"] += 1
        # 先清零：这次调用在拿到任何响应前就失败时，不会把上一次的用量再记一遍
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        try:
            return self._complete_json(system, user, max_tokens)
        except AIError:
            self.totals["errors"] += 1
            raise
        finally:
            for key in ("prompt_tokens", "completion_tokens"):
                self.totals[key] += self.last_usage.get(key, 0)

    def _complete_json(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        if "json" not in f"{system} {user}".lower():
            system = f"{system}\n请严格返回 JSON 对象。"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            # 百炼的 JSON 输出只支持非思考模式；千问混合模型显式关掉思考，免得默认值变了出错
            "enable_thinking": False,
            **self.extra_body,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # 这次调用花掉的用量：每次拿到响应都累计（含格式不对要重试的那几次），失败时也保留
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        self.last_usage = usage_total
        last_error = "AI 请求失败"
        last_code = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload
                )
            # 超时、连不上、对方中途断开（RemoteProtocolError）、代理出错等传输层错误都算网络失败
            except httpx.TransportError as exc:
                last_error = "AI 网络请求失败"
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                raise AIError(last_error, retryable=True) from exc
            if response.status_code >= 400:
                # 出错的响应如果也带了用量，同样计入
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict):
                    _add_usage(usage_total, body.get("usage"))
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"AI 服务暂时不可用（HTTP {response.status_code}）"
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                raise AIError(last_error, retryable=True)
            if response.status_code >= 400:
                raise AIError(f"AI 请求被拒绝（HTTP {response.status_code}）")
            try:
                envelope = response.json()
            except ValueError:
                envelope = None
            if isinstance(envelope, dict):
                _add_usage(usage_total, envelope.get("usage"))
            parsed, code = _parse_json_object(envelope)
            if code is not None:
                # 分类错误不链出解码异常，避免 JSONDecodeError 夹带响应正文。
                # 用量已经计入。截断和非正常结束再请求同一预算不会变好，立即失败。
                last_error = _json_error(code)
                last_code = code
                if code in {"truncated", "non_stop_finish"}:
                    raise AIError(last_error, retryable=False, code=code) from None
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                raise AIError(last_error, code=code) from None
            return parsed
        raise AIError(last_error, code=last_code) from None
