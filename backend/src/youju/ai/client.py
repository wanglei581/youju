"""OpenAI 兼容聊天接口客户端。"""

import json
import threading
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
        stream: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # 流式返回（SSE，D-098）：云开发 AI 网关对不走流式、1 分钟内没生成完的请求不返回结果。
        # 打开后边生成边收，收完拼成和非流式一样的信封，后面的检查和重试规则都不变；
        # 整次请求最多 timeout 秒：到点由定时器关掉连接（流式时每段都在到、或者对方停发又不断开，
        # 单靠读超时都拦不住），按网络失败处理
        self.stream = stream
        self.clock = clock
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

    def _send(self, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, object]:
        """一次性返回：(状态码, 解析出的 JSON；不是 JSON 时为 None)。"""
        response = self._client.post(
            f"{self.base_url}/chat/completions", headers=headers, json=payload
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    def _send_stream(self, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, object]:
        """流式返回（SSE）：逐段收 data 行，把 delta.content 拼起来，
        记下最后的 finish_reason 和 usage，拼成和一次性返回一样的信封。
        出错的状态码照一次性返回处理；流里来了错误事件按 502 处理（AI 服务暂时不可用），
        已收到的内容不用。某一段不是 JSON、或流没有正常结束（既没有 [DONE] 也没有
        finish_reason）时信封里不放 choices（按 invalid_envelope 处理）。用量都照样计入。
        错误事件的原文不带出去（和一次性返回一样，报错里不放响应原文）。"""
        body = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        deadline = self.clock() + self.timeout
        with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers={**headers, "Accept": "text/event-stream"},
            json=body,
        ) as response:
            if response.status_code >= 400:
                response.read()
                try:
                    return response.status_code, response.json()
                except ValueError:
                    return response.status_code, None
            fired = threading.Event()

            def stop() -> None:
                fired.set()
                response.close()

            timer = threading.Timer(max(0.0, deadline - self.clock()), stop)
            timer.daemon = True
            timer.start()
            try:
                return self._read_stream(response, deadline, fired)
            except Exception as exc:
                # 定时器到点关了连接：不管读到哪一步抛了什么，都按超时处理
                if fired.is_set():
                    raise httpx.ReadTimeout("AI 流式返回超时") from exc
                raise
            finally:
                timer.cancel()

    def _read_stream(
        self, response: httpx.Response, deadline: float, fired: threading.Event
    ) -> tuple[int, object]:
        """读 SSE 的每一行，拼成信封（见 _send_stream）。"""
        parts: list[str] = []
        finish = None
        usage = None
        done = False
        for raw in response.iter_lines():
            if self.clock() > deadline:
                raise httpx.ReadTimeout("AI 流式返回超时")
            line = raw.strip()
            # 空行、注释行（": ..."）、event: 行都跳过
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                return response.status_code, {"usage": usage}
            if not isinstance(chunk, dict):
                return response.status_code, {"usage": usage}
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            # 流里的错误事件：不用已收到的内容
            if chunk.get("error") is not None:
                return 502, {"usage": usage}
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            # 思考内容（reasoning_content）不拼进结果
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                parts.append(delta["content"])
            if choices[0].get("finish_reason") is not None:
                finish = choices[0]["finish_reason"]
        if fired.is_set():
            raise httpx.ReadTimeout("AI 流式返回超时")
        # 没有正常结束（中途断了）：半截内容就算碰巧是合法 JSON 也不用
        if not done and finish is None:
            return response.status_code, {"usage": usage}
        envelope: dict[str, Any] = {
            "choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish}]
        }
        if usage is not None:
            envelope["usage"] = usage
        return response.status_code, envelope

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
                send = self._send_stream if self.stream else self._send
                status, envelope = send(payload, headers)
            # 超时、连不上、对方中途断开（RemoteProtocolError）、代理出错等传输层错误都算网络失败
            except httpx.TransportError as exc:
                last_error = "AI 网络请求失败"
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                raise AIError(last_error, retryable=True) from exc
            # 响应（含出错的响应）如果带了用量，都计入
            if isinstance(envelope, dict):
                _add_usage(usage_total, envelope.get("usage"))
            if status == 429 or status >= 500:
                last_error = f"AI 服务暂时不可用（HTTP {status}）"
                if attempt < self.max_retries:
                    self.sleep(2**attempt)
                    continue
                raise AIError(last_error, retryable=True)
            if status >= 400:
                raise AIError(f"AI 请求被拒绝（HTTP {status}）")
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
