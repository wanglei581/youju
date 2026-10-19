"""把 cloud-logs.cjs 写下的结果文件整理成"时间 容器 内容"，可按关键词过滤；显示前一律遮蔽。

用法：python3 tools/team/cloud_logs_show.py <结果文件> [关键词…]
（关键词之间是"或"：一行里有任何一个就显示；每行内容最多显示前 300 字。）

为什么遮蔽：读日志的是 AI 成员，显示出来的内容会发到境外的模型服务，而隐私保护指引写的是不向境外
传输（个人信息保护影响评估第六节（二）第 6 条、（三）第 7 条）。遮蔽规则就是下面这几条：
- 文件编号：cloud://… 整段遮掉。
- 访问日志的 ip、uid 字段（JSON、转义成字符串的 JSON、Python 字典写法都认）：ip 是 IP 的按下一条遮，
  不是 IP 的整个换成 *；uid 留前 4 位（ab12*）。
- 来源 IP，出现在任何文字里的都遮：IPv4 留前两段（203.0.*.*），IPv6 留前两组（2408:8207:*）。
- 单独出现的 16 位十六进制串（用户编号的格式，如批量工具结果文件路径 advisor/out/<编号>/）留前 4 位。
- 以 o 开头的 28 位微信用户标识（openid、unionid 的格式，比如数据库报错里带出来的）留前 4 位。
留下的几位够判断是不是同一来源。关键词在遮蔽后的内容里找：拿完整的 IP 或编号当关键词逐个试，试不出来。
没有不遮蔽的显示方式。要看完整的 IP、按某个用户查，由创始人在云托管控制台的日志页自己查。
"""

import ipaddress
import json
import re
import sys

_FILE_ID = re.compile(r"cloud://\S+")
_FIELD = re.compile(
    r"""(?P<kq>\\?["'])(?P<key>ip|uid)(?P=kq)(?P<sep>\s*:\s*)(?P<vq>\\?["'])(?P<val>.*?)(?P=vq)""",
    re.IGNORECASE,
)
_OPENID = re.compile(r"(?<![0-9A-Za-z_-])o[0-9A-Za-z_-]{27}(?![0-9A-Za-z_-])")
_HEX16 = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{16}(?![0-9A-Za-z])")
# 只看前后是不是数字：句末的点、端口的冒号挨着也照样认
_IPV4 = re.compile(r"(?<!\d)(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}(?!\d)")
# 一整串十六进制、冒号和点（整串一次匹配，长串也不回溯）；是不是 IPv6 由 ipaddress 判断
_V6_RUN = re.compile(r"[0-9A-Fa-f:.]+")


def _ip_prefix(addr):
    if addr.version == 4:
        a, b = str(addr).split(".")[:2]
        return f"{a}.{b}.*.*"
    a, b = addr.exploded.split(":")[:2]
    return f"{int(a, 16):x}:{int(b, 16):x}:*"


def _keep4(value):
    return value[:4] + "*" if len(value) > 4 else "*"


def _field(m):
    val = m.group("val")
    if m.group("key").lower() == "uid":
        val = _keep4(val) if val else val
    elif val:
        try:
            val = _ip_prefix(ipaddress.ip_address(val))
        except ValueError:
            val = "*"
    return f"{m.group('kq')}{m.group('key')}{m.group('kq')}{m.group('sep')}{m.group('vq')}{val}{m.group('vq')}"


def _v6(m):
    run = m.group(0)
    if run.count(":") < 2:
        return run
    body = run.rstrip(".")
    head, tail = "", run[len(body) :]
    # 粘在地址前后的单个冒号（"addr:2408:…"、"…::1:"）不算地址；"::" 是地址的一部分
    if body.startswith(":") and not body.startswith("::"):
        head, body = ":", body[1:]
    if body.endswith(":") and not body.endswith("::"):
        body, tail = body[:-1], ":" + tail
    try:
        addr = ipaddress.IPv6Address(body)
    except ValueError:
        return run
    return head + _ip_prefix(addr) + tail


def mask(text):
    text = _FILE_ID.sub("cloud://…", text)
    text = _FIELD.sub(_field, text)
    text = _OPENID.sub(lambda m: _keep4(m.group(0)), text)
    text = _HEX16.sub(lambda m: _keep4(m.group(0)), text)
    text = _IPV4.sub(r"\1.\2.*.*", text)
    return _V6_RUN.sub(_v6, text)


def lines(data, keys=()):
    """结果文件里的每一行日志，遮蔽后整理成"时间 容器 内容"；给了关键词就只要含关键词的。"""
    for r in data.get("results") or []:
        content = r.get("Content", "")
        try:
            j = json.loads(content)
        except (TypeError, ValueError):
            j = None
        if not isinstance(j, dict):
            j = {"__CONTENT__": content}
        tag = j.get("__TAG__")
        container = tag.get("container_name", "?") if isinstance(tag, dict) else "?"
        msg = mask(str(j.get("__CONTENT__", "")).replace("\n", " "))
        if keys and not any(k in msg for k in keys):
            continue
        # 先遮再截：截断处正好落在 IP 中间时，残缺的 IP 认不出来
        yield f"{r.get('Timestamp')} {mask(str(container))} {msg[:300]}"


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    with open(argv[1], encoding="utf-8") as f:
        data = json.load(f)
    if "error" in data:
        print(mask(json.dumps(data, ensure_ascii=False)))
        return 1
    print(
        f"# 共 {data.get('count')} 行，listOver={data.get('listOver')}，pages={data.get('pages')}"
        f"{'，到了 20 页上限没取完' if data.get('truncated') else ''}；IP、用户编号、文件编号已遮蔽"
    )
    for line in lines(data, argv[2:]):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
