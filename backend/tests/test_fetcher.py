import httpx

from youju.crawler.fetcher import PoliteFetcher, decode_html

LIST_HTML = "<ul><li><a href='/a.html'>关于开展申报工作的通知</a><span>2026-09-24</span></li></ul>"


def make_fetcher(handler, interval: float = 0.0, clock=None, sleep=None) -> PoliteFetcher:
    kwargs = {}
    if clock:
        kwargs["clock"] = clock
    if sleep:
        kwargs["sleep"] = sleep
    return PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=interval,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_ok_page_and_user_agent() -> None:
    seen_agents = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_agents.append(request.headers["user-agent"])
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, text=LIST_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    with make_fetcher(handler) as f:
        result = f.fetch("https://a.gov.cn/tzgg/")
    assert result.status == "ok"
    assert "关于开展申报工作的通知" in result.text
    assert all("YoujuBot" in ua for ua in seen_agents)


def test_robots_disallow_is_respected() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text=LIST_HTML)

    with make_fetcher(handler) as f:
        blocked = f.fetch("https://a.gov.cn/private/list.html")
        allowed = f.fetch("https://a.gov.cn/public/list.html")
    assert blocked.status == "robots_disallowed"
    assert allowed.status == "ok"
    assert "/private/list.html" not in requested, "被 robots 禁止的页面不能发出请求"
    assert requested.count("/robots.txt") == 1, "robots.txt 应缓存"


def test_robots_server_error_means_skip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        raise AssertionError("robots.txt 无法获取时不应抓取页面")

    with make_fetcher(handler) as f:
        assert f.fetch("https://a.gov.cn/list.html").status == "robots_error"


def test_robots_4xx_or_html_means_allowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if request.url.host == "a.gov.cn":
                return httpx.Response(403)
            return httpx.Response(200, text="<html>页面不存在</html>")
        return httpx.Response(200, text=LIST_HTML)

    with make_fetcher(handler) as f:
        assert f.fetch("https://a.gov.cn/list.html").status == "ok"
        assert f.fetch("https://b.gov.cn/list.html").status == "ok"


def test_http_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(412)

    with make_fetcher(handler) as f:
        result = f.fetch("https://a.gov.cn/list.html")
    assert result.status == "http_error"
    assert result.http_status == 412


def test_network_error_retries_once() -> None:
    calls = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["page"] += 1
        raise httpx.ConnectTimeout("timed out", request=request)

    with make_fetcher(handler) as f:
        result = f.fetch("https://a.gov.cn/list.html")
    assert result.status == "network_error"
    assert calls["page"] == 2


def test_per_host_interval() -> None:
    now = {"t": 0.0}
    sleeps: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=LIST_HTML)

    with make_fetcher(handler, interval=3.0, clock=clock, sleep=sleep) as f:
        f.fetch("https://a.gov.cn/1.html")
        f.fetch("https://a.gov.cn/2.html")
        f.fetch("https://b.gov.cn/1.html")
    # 读取 robots.txt 也算对该网站的一次请求：
    # a.gov.cn：robots → 等 3 秒 → 第一页 → 等 3 秒 → 第二页
    # b.gov.cn：不用等 a.gov.cn；robots → 等 3 秒 → 第一页
    assert sleeps == [3.0, 3.0, 3.0]


def test_decode_html_charsets() -> None:
    text = "关于开展申报工作的通知"
    gbk = f"<meta charset='gb2312'><p>{text}</p>".encode("gbk")
    assert text in decode_html(gbk, None)
    assert text in decode_html(text.encode("utf-8"), "text/html; charset=UTF-8")
    # 没有声明编码、又不是 UTF-8 时回退到 GB18030
    assert text in decode_html(text.encode("gb18030"), None)


def test_ssl_handshake_error_retries_in_compat_mode_and_remembers_host() -> None:
    default_calls: list[str] = []
    compat_calls: list[str] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        default_calls.append(request.url.path)
        raise httpx.ConnectError("[SSL: BAD_ECPOINT] bad ecpoint (_ssl.c:1010)", request=request)

    def compat_handler(request: httpx.Request) -> httpx.Response:
        compat_calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=LIST_HTML)

    fetcher = PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0.0,
        transport=httpx.MockTransport(default_handler),
        compat_transport=httpx.MockTransport(compat_handler),
    )
    with fetcher as f:
        assert f.fetch("https://old.gov.cn/1.html").status == "ok"
        assert f.fetch("https://old.gov.cn/2.html").status == "ok"
    # 默认模式只失败一次（robots.txt），之后该网站一直走兼容模式
    assert default_calls == ["/robots.txt"]
    assert compat_calls == ["/robots.txt", "/1.html", "/2.html"]


def test_certificate_error_is_never_bypassed() -> None:
    compat_calls: list[str] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate",
            request=request,
        )

    def compat_handler(request: httpx.Request) -> httpx.Response:
        compat_calls.append(request.url.path)
        return httpx.Response(200, text=LIST_HTML)

    fetcher = PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0.0,
        transport=httpx.MockTransport(default_handler),
        compat_transport=httpx.MockTransport(compat_handler),
    )
    with fetcher as f:
        result = f._get("https://bad-cert.gov.cn/list.html")
    assert result.status == "ssl_error"
    assert "证书" in result.error
    assert compat_calls == []
