"""把 cloud-logs.cjs 写下的结果文件整理成一行一条的摘要；可以按关键词查有没有、有几条。日志正文不显示。

用法：python3 tools/team/cloud_logs_show.py <结果文件> [关键词…]
（关键词之间是"或"：日志正文里有任何一个就算命中；只显示命中那几行的摘要和条数，正文照样不显示。）

为什么只显示摘要：读日志的是 AI 成员，显示出来的东西会发到境外的模型服务，而隐私保护指引写的是不向境外
传输（个人信息保护影响评估第六节（二）第 6 条、（三）第 7 条）。"认出 IP、编号再遮掉"总有漏的写法（转义换行
夹在中间、%2e 编码、紧贴下划线、前导数字……），所以只显示下面这些格式固定、带不出个人信息的字段，别的一律不显示：
- 时间：形如 2026-10-19 11:00:00(.000) 的才显示，否则显示 ?
- 容器：小写字母、数字、减号组成、又不带 16 位十六进制串的容器名才显示，否则 ?
- 访问日志（JSON，event 是 access）：方法（GET、POST 等七种之一）、路径（去掉 ? 和 # 后面；每一段只留小写英文
  单词、v1 这种版本号、不超过 10 位的纯数字，别的段换成 *）、状态码（100–599 的整数）、耗时（毫秒）
- 其他日志的类别：日志级别和记录器名（只认 youju、uvicorn、sqlalchemy 等几个已知的记录器，如 WARNING
  youju.access_log；uvicorn 的 INFO: 记成 INFO uvicorn）、JSON 日志里 event、action 的名字（只认小写英文单词）、
  异常类名（以 Error、Exception、Warning 结尾的英文类名）、Traceback
- 出错文件：出错类别（api / agent / exception）、错误码和异常名（英文标识符才显示）、ret（整数才显示）
日志正文、出错原文只在本机结果文件里，要看由创始人在本机或云托管控制台的日志页看。
关键词在正文里找；完整的 IP、16 位用户编号、28 位 openid 不能当关键词（按人查由创始人在控制台自己查）。
"""

import ipaddress
import json
import re
import sys

_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?")
_CONTAINER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
# 纯数字最多 10 位：11 位的手机号这类不会当成编号显示出来
_SEGMENT = re.compile(r"[a-z]+(?:[-_][a-z]+)*|v\d{1,2}|\d{1,10}")
# event、action 只认小写英文单词（不带数字：小写的用户编号也混不进来）
_NAME = re.compile(r"[a-z]+(?:[-_][a-z]+){0,5}")
_LOGGER = re.compile(r"(?:youju|uvicorn|sqlalchemy|httpx|httpcore|alembic|apscheduler|asyncio|fastapi|starlette)(?:\.[a-z_]+){0,4}")
_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")
# 行首的日志前缀：程序自己的（时间 级别 记录器[:]）和 uvicorn 的（级别:）
_PY_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) (\S+?):? ")
_UVICORN = re.compile(r"(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s")
# \b 开头：长串大写字母里不会每个位置都重新试一遍；类名最长 70 个字母，再长的不算（也免得整行被当成类名）
_EXCEPTION = re.compile(r"\b[A-Z][A-Za-z]{0,60}(?:Error|Exception|Warning)\b")
# 完整的 IP、用户编号、openid 不能当关键词
_HEX16 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16}(?![0-9A-Fa-f])")
_OPENID = re.compile(r"o[0-9A-Za-z_-]{27}")
_ADDR_RUN = re.compile(r"[0-9A-Fa-f:.]+")


def _parse(text):
    """text 是 JSON 就返回解析结果，不是（或嵌套太深）返回 None。"""
    try:
        return json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None


def _path(value):
    if not isinstance(value, str):
        return "?"
    value = value.split("?", 1)[0].split("#", 1)[0]
    if not value.startswith("/"):
        return "?"
    parts = value[1:].split("/")[:12]
    return "/" + "/".join(p if _SEGMENT.fullmatch(p) else "*" for p in parts)


def _number(value, low, high):
    # bool 是 int 的子类，True 不能当成状态码 1
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        return "?"
    return str(int(value))


def _event(obj):
    names = [obj.get(k) for k in ("event", "action")]
    return "/".join(n for n in names if isinstance(n, str) and _NAME.fullmatch(n))


def _category(content, obj):
    """不是访问日志的那几行：级别、记录器、event/action、异常类名、Traceback。"""
    words = []
    m = _PY_PREFIX.match(content)
    if m:
        words.append(m.group(1))
        if _LOGGER.fullmatch(m.group(2)):
            words.append(m.group(2))
        body = content[m.end():]
        if obj is None and body.startswith("{"):
            obj = _parse(body)
    else:
        m = _UVICORN.match(content)
        if m:
            words += [m.group(1), "uvicorn"]
    if isinstance(obj, dict):
        event = _event(obj)
        if event:
            words.append(event)
    if "Traceback (most recent call last)" in content:
        words.append("Traceback")
    seen = []
    for name in _EXCEPTION.findall(content):
        if name not in seen:
            seen.append(name)
        if len(seen) == 3:
            break
    return " ".join(words + seen) or "-"


def summary(record):
    """一条结果的摘要："时间 容器 方法 路径 状态码 耗时ms"，不是访问日志的换成类别。"""
    if not isinstance(record, dict):
        return "? ? -"
    ts = record.get("Timestamp")
    ts = ts if isinstance(ts, str) and _TIME.fullmatch(ts) else "?"
    raw = record.get("Content")
    outer = _parse(raw) if isinstance(raw, str) else raw
    content, container = raw if isinstance(raw, str) else "", "?"
    if isinstance(outer, dict):
        tag = outer.get("__TAG__")
        name = tag.get("container_name") if isinstance(tag, dict) else None
        container = name if isinstance(name, str) and _CONTAINER.fullmatch(name) and not _HEX16.search(name) else "?"
        inner = outer.get("__CONTENT__")
        content = inner if isinstance(inner, str) else ""
        obj = _parse(inner) if isinstance(inner, str) else inner
    else:
        obj = None
    if isinstance(obj, dict) and obj.get("event") == "access":
        method = obj.get("method") if obj.get("method") in _METHODS else "?"
        fields = f"{method} {_path(obj.get('path'))} {_number(obj.get('status'), 100, 599)} {_number(obj.get('ms'), 0, 10**7)}ms"
        return f"{ts} {container} {fields}"
    return f"{ts} {container} {_category(content, obj)}"


def _text(record):
    """关键词在这里找：结果原文，以及解析出来的正文。"""
    if not isinstance(record, dict):
        return ""
    raw = record.get("Content")
    raw = raw if isinstance(raw, str) else ""
    outer = _parse(raw)
    inner = outer.get("__CONTENT__") if isinstance(outer, dict) else None
    return raw + "\n" + (inner if isinstance(inner, str) else "")


def bad_keyword(key):
    """完整的 IP、16 位十六进制串（用户编号的格式）、o 开头的 28 位标识（openid）不能当关键词。"""
    if _HEX16.search(key) or _OPENID.search(key):
        return True
    for run in _ADDR_RUN.findall(key):
        for cand in (run, run.strip(".:")):
            try:
                ipaddress.ip_address(cand)
                return True
            except ValueError:
                pass
    return False


def lines(data, keys=()):
    for record in data.get("results") or []:
        if keys and not any(k in _text(record) for k in keys):
            continue
        yield summary(record)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    keys = argv[2:]
    if any(bad_keyword(k) for k in keys):
        print("# 完整的 IP、用户编号、openid 不能当关键词：按人查由创始人在云托管控制台的日志页自己查")
        return 2
    try:
        with open(argv[1], encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError) as e:
        print(f"# 结果文件读不了（{type(e).__name__}）")
        return 1
    if not isinstance(data, dict):
        print("# 这不是 cloud-logs.cjs 写下的结果文件")
        return 1
    if "error" in data:
        kind = data.get("error") if data.get("error") in ("api", "agent", "exception") else "?"
        parts = [f"# 出错：{kind}"]
        for key in ("code", "name"):
            value = data.get(key)
            if isinstance(value, str) and _IDENT.fullmatch(value) and not bad_keyword(value):
                parts.append(f"{key}={value}")
        if isinstance(data.get("ret"), int) and not isinstance(data.get("ret"), bool):
            parts.append(f"ret={data['ret']}")
        print("，".join(parts) + "；出错原文在结果文件里，这里不显示")
        return 1
    count = _number(data.get("count"), 0, 10**9)
    pages = _number(data.get("pages"), 0, 1000)
    over = data.get("listOver") if isinstance(data.get("listOver"), bool) else "?"
    print(
        f"# 共 {count} 行，listOver={over}，pages={pages}"
        f"{'，到了 20 页上限没取完' if data.get('truncated') is True else ''}"
        "；只显示时间、容器、接口和类别，日志正文不显示（原文在结果文件里）"
    )
    shown = 0
    for line in lines(data, keys):
        print(line)
        shown += 1
    if keys:
        print(f"# 含关键词的有 {shown} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
