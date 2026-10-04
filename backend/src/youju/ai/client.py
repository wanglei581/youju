"""OpenAI 兼容聊天接口客户端。"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx


class AIError(RuntimeError):
    """AI 服务调用失败；错误信息不包含密钥或响应原文。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _add_usage(total: dict[str, int], usage: object) -> None:
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                total[key] += value


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
        """请求 JSON 对象，针对暂时性错误和非法 JSON 自动重试；成功、失败都计入累计用量。"""
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
                if isinstance(envelope, dict):
                    _add_usage(usage_total, envelope.get("usage"))
                content = envelope["choices"][0]["message"]["content"]
                parsed = json.loads(content) if isinstance(content, str) else content
                if not isinstance(parsed, dict):
                    raise ValueError("不是 JSON 对象")
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                last_error = "AI 返回内容不是合法 JSON 对象"
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                raise AIError(last_error) from exc
            return parsed
        raise AIError(last_error)
