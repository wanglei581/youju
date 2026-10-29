"""栏目列表由页面脚本从公开接口取数时，抓取直接请求这个接口（不用浏览器渲染）。

湖南省工业和信息化厅 2026-10-10 前后改版：栏目页只剩页面框架，列表由页面末尾的 list_all.js
向 api.hunan.gov.cn 的检索接口取数（POST 表单，参数写在脚本里）。这个接口不需要登录、口令、
签名参数或验证码，响应头允许任何网站跨域读取，是公开的（2026-10-19 本机核对，D2）。

用哪种接口、哪个栏目编号，在 sources.parser_config 里写明，不从页面推断：
    {"list_api": "hunan_search", "channel_id": "71043"}
栏目编号是栏目页脚本里的 `var channelId = '…'`。来源的网址仍是原来的栏目页：用户看到的、
条目相对链接的基准都不变。每次只取第一页，和页面上显示的一样（20 条）。

每轮入库前都复核接口的回复（read_api_response，改配置前的试跑也用它）：
- 接口拒绝访问（HTTP 401/403，或 HTTP 200 但正文是 {"code":401,…}）记 access_denied：
  按 D-013 是访问控制，不绕过，由人确认后停用这个来源；跳转（不跟随）或回的不是 JSON，
  不去猜是不是登录页，错误里写明可能性，由人打开核对；
- 回显的栏目编号和配置不一致、过半条目不在栏目页目录下，这次不入库，记 parse_empty 并写明原因；
  每条自带的栏目编号不是这个栏目的，丢掉这一条。
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

from youju.crawler.dates import date_from_url, parse_date_detail
from youju.crawler.fetcher import FetchResult
from youju.crawler.listing import ListItem
from youju.crawler.titles import clean_title
from youju.crawler.urls import normalize_url, page_dir, url_key

HUNAN_SEARCH = "hunan_search"
LIST_API_KINDS = (HUNAN_SEARCH,)
CONFIG_KEYS = ("list_api", "channel_id")
API_EMPTY_MESSAGE = "列表接口可以访问，但没有返回列表条目：可能接口或栏目编号变了"
# 要登录、要验证时，网站可能跳到登录页或回一个网页；不去猜是哪一种，写明可能性，由人打开核对
_CHECK_BY_HAND = "请人工打开栏目页核对，要登录或验证的按 D-013 停用，不绕过"
API_NOT_JSON_MESSAGE = f"列表接口回的不是 JSON（可能是登录页、验证页或网站出错）：{_CHECK_BY_HAND}"
_HUNAN_SEARCH_URL = "https://api.hunan.gov.cn/search/common/search/{channel}"
# 照抄 list_all.js 里的 dataparam（jQuery 把嵌套参数编码成 datas[0][key] 这种写法）：
# 只要已发布的（status=4），按发布时间倒序，第一页 20 条
_HUNAN_SEARCH_FORM = (
    ("datas[0][key]", "status"),
    ("datas[0][value]", "4"),
    ("datas[0][join]", "and"),
    ("datas[0][queryType]", "term"),
    ("datas[1][key]", "publishedTime"),
    ("datas[1][sort]", "true"),
    ("datas[1][order]", "desc"),
    ("datas[1][queryType]", "term"),
    ("page", "1"),
    ("_pageSize", "20"),
    ("_isAgg", "true"),
)
# 栏目编号：已见过纯数字（71043）和 32 位十六进制两种
_CHANNEL = re.compile(r"[0-9A-Za-z]{1,64}")
_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
# 拒绝访问：HTTP 状态码，或网关回 HTTP 200 时正文里的业务码（如 {"msg":"invalid token","code":401}）
_DENIED_HTTP = {401, 403}
_DENIED_CODES = {"401", "403"}


@dataclass(frozen=True)
class ApiRequest:
    url: str
    form: tuple[tuple[str, str], ...]


def valid_api_config(raw: object) -> dict[str, str] | None:
    """列表接口配置：只收 list_api 和 channel_id 两个键，接口种类要认识、栏目编号只能是字母和数字；
    不合规返回 None。"""
    if not isinstance(raw, dict) or set(raw) != set(CONFIG_KEYS):
        return None
    kind, channel = raw["list_api"], raw["channel_id"]
    if (
        kind not in LIST_API_KINDS
        or not isinstance(channel, str)
        or not _CHANNEL.fullmatch(channel)
    ):
        return None
    return {"list_api": kind, "channel_id": channel}


def _checked(config: dict | None) -> dict[str, str]:
    checked = valid_api_config(config)
    if checked is None:
        raise ValueError("列表接口配置不对：只能有 list_api、channel_id 两项")
    return checked


def api_request(config: dict | None) -> ApiRequest | None:
    """来源配了列表接口时返回要发的请求；没配返回 None（照常抓栏目页）。

    配置写错时抛 ValueError，不退回抓栏目页：那样只会每轮"没有识别到条目"，看不出是配置的问题。"""
    if not config or "list_api" not in config:
        return None
    channel = _checked(config)["channel_id"]
    return ApiRequest(_HUNAN_SEARCH_URL.format(channel=channel), _HUNAN_SEARCH_FORM)


def _denied(detail: str) -> str:
    return (
        f"列表接口拒绝访问（{detail}）：需要登录或口令，按规矩不绕过，"
        "请人工确认后停用这个来源（D-013）"
    )


def api_http_problem(result: FetchResult) -> tuple[str, str] | None:
    """列表接口回 HTTP 错误时要换的（状态，错误）；不用换返回 None。

    401、403 是拒绝访问，记 access_denied；跳转（如跳到登录页）照旧记 http_error、不跟随，
    错误里写明跳到哪、请人核对。"""
    if result.status != "http_error" or result.http_status is None:
        return None
    if result.http_status in _DENIED_HTTP:
        return "access_denied", _denied(f"HTTP {result.http_status}")
    if 300 <= result.http_status < 400:
        return "http_error", f"{result.error}：可能要求登录，{_CHECK_BY_HAND}"
    return None


def _refusal(data: object) -> str | None:
    if not isinstance(data, dict) or str(data.get("code")) not in _DENIED_CODES:
        return None
    msg = " ".join(str(data.get("msg") or "").split())[:60]
    return _denied(f"code {data['code']}" + (f"：{msg}" if msg else ""))


def api_refusal(text: str) -> str | None:
    """接口回 HTTP 200、正文却是明确的拒绝时返回说明，否则返回 None。

    只认正文是 JSON 对象、顶层业务码 401 或 403 的（正常的回复顶层只有 data，没有 code）；
    其他结构不对的回复按"没有返回列表条目"算，不猜。是拒绝就不再解析条目。"""
    try:
        return _refusal(json.loads(text))
    except ValueError:
        return None


def _title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return clean_title(" ".join(_MARKUP.sub(" ", value).split()))


def _item_url(page_url: str, href: object) -> str | None:
    """按栏目页补全条目网址；写得不对的（括号不配对的主机、端口超范围等）返回 None，
    只跳过这一条，不连累整批。"""
    if not isinstance(href, str):
        return None
    try:
        url = normalize_url(page_url, href)
        if url is not None:
            url_key(url)  # 入库时要算规范键，算不出的这里先剔掉
    except ValueError:
        return None
    return url


@dataclass(frozen=True)
class _Parsed:
    items: list[ListItem]
    # 接口回了几条（丢掉栏目编号不对的、写坏的之前）
    returned: int
    # 回复里 data.channelId 回显的栏目编号；接口按请求的栏目编号回显
    channel: str | None
    # 收下的条目自带的栏目名（如"文件通知"），用来核对栏目编号有没有填错
    names: tuple[str, ...]


def _parse(
    data: object, page_url: str, channel_id: str | None, today: date | None
) -> _Parsed | None:
    """解析接口回复（已经按 JSON 读出来的）；没有 data.results 列表时返回 None。

    给了 channel_id 时，每条自带的栏目编号（channelId）不是它的就丢掉（没写的也丢：核对不了）。"""
    payload = data.get("data") if isinstance(data, dict) else None
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return None
    echoed = payload.get("channelId")
    items: list[ListItem] = []
    names: dict[str, None] = {}
    seen: set[str] = set()
    for obj in results:
        if not isinstance(obj, dict):
            continue
        if channel_id is not None and str(obj.get("channelId")) != channel_id:
            continue
        title = _title(obj.get("title"))
        url = _item_url(page_url, obj.get("url"))
        if not title or not url or url in seen:
            continue
        published = obj.get("publishedTimeStr")
        found, full = parse_date_detail(published if isinstance(published, str) else "", today)
        url_date = date_from_url(url, today)
        if found is None or (not full and url_date):
            found = url_date or found
        seen.add(url)
        items.append(ListItem(title, url, found))
        if isinstance(obj.get("channelName"), str) and obj["channelName"].strip():
            names.setdefault(obj["channelName"].strip(), None)
    return _Parsed(items, len(results), None if echoed is None else str(echoed), tuple(names))


def parse_api_items(
    text: str, page_url: str, today: date | None = None, *, channel_id: str | None = None
) -> list[ListItem]:
    """把接口返回的 JSON 换成列表条目（标题、网址、发布日期），按接口给的顺序，同一网址只留一次。

    网址按栏目页网址补全（接口给的是 /gxt/xxgk_71033/tzgg/202610/t20261016_….html 这样的站内路径）；
    发布日期取 publishedTimeStr（页面上显示的就是它的前 10 位），没有时用网址里的日期。
    不是 JSON、没有 data.results 时返回空列表。"""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    parsed = _parse(data, page_url, channel_id, today)
    return parsed.items if parsed else []


def _outside_column(items: list[ListItem], page_url: str) -> str | None:
    """过半条目（严格多于一半）要和栏目页同一主机、在栏目页所在目录下，否则返回原因。

    栏目编号在湖南各政府网站间通用，填了别的网站的编号时，站内路径按本站补全会全部指错；
    栏目页就在网站根目录的，任何站内路径都"在目录下"，核对不了，也不收。"""
    directory = page_dir(page_url)
    if directory == "/":
        return "栏目页在网站根目录，核对不了条目是不是这个栏目的"
    host = urlsplit(page_url).netloc.lower()
    inside = sum(
        1
        for item in items
        if urlsplit(item.url).netloc.lower() == host
        and urlsplit(item.url).path.startswith(directory)
    )
    if inside * 2 <= len(items):
        return f"过半条目不在栏目页所在目录（{directory}）下，可能是别的网站或别的栏目的编号"
    return None


def read_api_response(
    text: str, page_url: str, config: dict, today: date | None = None
) -> tuple[str, str | None, list[ListItem], tuple[str, ...]]:
    """读一次接口回复，返回（状态，错误，条目，条目自带的栏目名）。定时抓取和改配置前的试跑共用。

    依次看：不是 JSON → parse_empty（写明可能是登录页、验证页）；明确拒绝访问 → access_denied；
    结构不对 → parse_empty；回显的栏目编号和配置不一致（或回了条目却没回显）→ parse_empty、不收；
    每条栏目编号不对的丢掉，一条都不剩 → parse_empty；
    过半条目不在栏目页目录下 → parse_empty、不收。"""
    try:
        data = json.loads(text)
    except ValueError:
        return "parse_empty", API_NOT_JSON_MESSAGE, [], ()
    refusal = _refusal(data)
    if refusal:
        return "access_denied", refusal, [], ()
    channel = _checked(config)["channel_id"]
    parsed = _parse(data, page_url, channel, today)
    if parsed is None:
        return "parse_empty", API_EMPTY_MESSAGE, [], ()
    if parsed.channel != channel and (parsed.channel is not None or parsed.returned):
        echoed = parsed.channel or "无"
        return (
            "parse_empty",
            f"列表接口回显的栏目编号（{echoed}）和配置的 {channel} 不一致，这次不入库",
            [],
            (),
        )
    if not parsed.items:
        return "parse_empty", API_EMPTY_MESSAGE, [], ()
    outside = _outside_column(parsed.items, page_url)
    if outside:
        return "parse_empty", f"{outside}，这次不入库", [], parsed.names
    return "ok", None, parsed.items, parsed.names


@dataclass(frozen=True)
class ApiTrial:
    status: str
    error: str | None
    items: list[ListItem]
    # 条目自带的栏目名（如"文件通知"），用来核对栏目编号有没有填错
    channel_names: tuple[str, ...]


def trial_api(page_url: str, config: dict, fetcher, today: date | None = None) -> ApiTrial:
    """按配置请求一次接口并识别条目，不写数据库（改配置前核对用）。fetcher 用礼貌抓取器。"""
    request = api_request(config)
    if request is None:
        raise ValueError("没有配置列表接口")
    result = fetcher.fetch(request.url, form=request.form)
    if result.status != "ok":
        status, error = api_http_problem(result) or (result.status, result.error)
        return ApiTrial(status, error, [], ())
    status, error, items, names = read_api_response(result.text, page_url, config, today)
    return ApiTrial(status, error, items, names)


def trial_problem(trial: ApiTrial, column_name: str, min_items: int) -> str | None:
    """试跑结果能不能写进来源配置；不能时返回原因。

    除了 read_api_response 每轮都做的复核，还要：识别出至少 min_items 条带日期的条目；
    条目自带的栏目名里有来源登记的栏目名（防止填了别的栏目的编号）。"""
    if trial.status != "ok":
        return trial.error or f"试跑没有成功（{trial.status}）"
    dated = sum(1 for item in trial.items if item.publish_date)
    if dated < min_items:
        return f"带日期的条目少于 {min_items} 条"
    if column_name not in trial.channel_names:
        names = "、".join(trial.channel_names) or "无"
        return f"接口返回的栏目名（{names}）里没有来源登记的“{column_name}”，请核对栏目编号"
    return None
