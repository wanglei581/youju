"""微信文本内容安全检测（msgSecCheck 2.0），经云托管开放接口服务调用（不用 access_token）。

纠错和问一问共用（问一问开发设计第九节第 5、6 条：内容安全的第二层，提问和问这份通知的回答都查）：

- check_text：问一问用。只有 suggest 是 pass 才算通过，risky、review 都拦下；把 suggest、label
  带回来写违规记录。接口出错、超时、errcode 不是 0、返回的结果看不懂，都抛 CheckDown，不在出错时
  放行。超过 2500 字分几段查，每段都通过才算通过。不在云托管里（本机开发、测试）不调，直接算通过；
- check_feedback：纠错用，和原来 api/me.py 里的完全一样（只看通过没有，出错照原样抛出）。

官方文档（2026-07-29 版，10/28 本机打开）：文本最多 2500 字；openid 要求用户近两小时访问过小程序
（不然 errcode 61010）；suggest 有 risky、pass、review 三种；没上架的小程序每天最多调 100 次。
报错信息里不带要查的文字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from youju.config import get_settings

URL = "http://api.weixin.qq.com/wxa/msg_sec_check"
LIMIT = 2500  # 一次最多查多少字（官方上限）
_OVERLAP = 50  # 分段时前后两段重叠的字数：跨在切口上的词（50 字以内）总有一段里是完整的
_SUGGESTS = ("pass", "risky", "review")
_FAILURES = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError)


class CheckDown(Exception):
    """这次没查成：接口出错、超时、errcode 不是 0、返回的结果看不懂。不能当作通过。"""

    def __init__(self, message: str, errcode: Any = None) -> None:
        super().__init__(message)
        self.errcode = errcode  # 微信返回的 errcode；不是 errcode 引起的为 None


@dataclass(frozen=True)
class Verdict:
    passed: bool  # 只有 suggest 是 pass 才为真
    suggest: str | None  # pass、risky、review；没调接口时是 pass
    label: int | None  # 命中的标签（100 正常、20001 时政、21000 其他……）；没调接口时为 None


def in_cloud(settings: Any) -> bool:
    """是不是在云托管里，要真的调接口。小程序接口服务信任微信请求头（trust_wx_headers，和纠错
    跳过检测的条件一样）；核对台服务（YOUJU_ROLE=admin）不信任请求头，但同样经开放接口服务调
    （发订阅消息就走这条路），问这份通知的回答在那里查。本机开发和测试两样都不是，不调。"""
    return bool(getattr(settings, "trust_wx_headers", False)) or (
        getattr(settings, "role", "all") == "admin"
    )


def _post(openid: str, content: str, scene: int, timeout: float) -> Any:
    response = httpx.post(
        URL,
        json={"openid": openid, "version": 2, "scene": scene, "content": content},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def check_feedback(openid: str, content: str) -> bool:
    """纠错的内容安全，和原来 api/me.py 里的完全一样：只看通过没有；不看在不在云托管里（调用方看
    trust_wx_headers），不分段；出错照原样抛出 httpx.HTTPError、ValueError 等，调用方回 503。"""
    result = _post(openid, content, 2, 5)
    if result.get("errcode") != 0:
        raise ValueError("微信内容安全接口未成功")
    return result.get("result", {}).get("suggest") == "pass"


def _pieces(content: str) -> list[str]:
    """按官方上限切开：每段最多 2500 字，前后两段重叠 50 字。"""
    if len(content) <= LIMIT:
        return [content]
    step = LIMIT - _OVERLAP
    return [content[at : at + LIMIT] for at in range(0, len(content) - _OVERLAP, step)]


def _check_piece(openid: str, piece: str, scene: int, timeout: float) -> Verdict:
    try:
        result = _post(openid, piece, scene, timeout)
        errcode = result.get("errcode")
        outcome = result.get("result")
        suggest = outcome.get("suggest") if isinstance(outcome, dict) else None
        label = outcome.get("label") if isinstance(outcome, dict) else None
    except _FAILURES as exc:
        raise CheckDown("微信内容安全接口出错") from exc
    if errcode != 0:
        raise CheckDown(f"微信内容安全接口没有成功（errcode {errcode!r}）", errcode)
    if suggest not in _SUGGESTS:
        raise CheckDown("微信内容安全接口没有给出检测结果")
    return Verdict(suggest == "pass", suggest, label if type(label) is int else None)


def check_text(openid: str, content: str, *, scene: int = 2, timeout: float = 5.0) -> Verdict:
    """问一问的内容安全复核：openid 是提问的人，scene 默认 2（评论，同纠错），每次调用最多等
    timeout 秒。

    超过 2500 字分几段查：有一段没通过就返回那一段的结果，后面的不再查；都通过返回最后一段的结果。
    没查成抛 CheckDown。不在云托管里不调，返回 Verdict(True, "pass", None)。"""
    if not content.strip():
        raise ValueError("没有要查的文字")
    if not in_cloud(get_settings()):
        return Verdict(True, "pass", None)
    verdict = Verdict(True, "pass", None)
    for piece in _pieces(content):
        verdict = _check_piece(openid, piece, scene, timeout)
        if not verdict.passed:
            break
    return verdict
