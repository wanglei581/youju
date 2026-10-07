import socket
import threading

import httpx
import pytest

from youju.crawler import fetcher as fetcher_module
from youju.crawler.fetcher import (
    MAX_RESPONSE_BYTES,
    PoliteFetcher,
    decode_html,
    non_public_address,
    resolve_host,
)

LIST_HTML = "<ul><li><a href='/a.html'>关于开展申报工作的通知</a><span>2026-09-24</span></li></ul>"


def public_dns(host: str) -> list[str]:
    """测试不做真实 DNS 查询：所有域名都当作解析到公网地址。"""
    return ["1.2.3.4"]


def make_fetcher(
    handler, interval: float = 0.0, clock=None, sleep=None, resolver=public_dns
) -> PoliteFetcher:
    kwargs = {}
    if clock:
        kwargs["clock"] = clock
    if sleep:
        kwargs["sleep"] = sleep
    return PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=interval,
        transport=httpx.MockTransport(handler),
        resolver=resolver,
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


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_robots_is_respected_when_content_is_not_decoded(prefix: str) -> None:
    """下载附件的抓取器不解码内容（decode_text=False），robots.txt 照样要解码、解析。"""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            body = f"{prefix}User-agent: *\nDisallow: /files/\n".encode()
            return httpx.Response(200, content=body, headers={"content-type": "text/plain"})
        return httpx.Response(200, content=b"%PDF-1.4 ...")

    with PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
        decode_text=False,
    ) as f:
        blocked = f.fetch("https://a.gov.cn/files/guide.pdf")
        allowed = f.fetch("https://a.gov.cn/other/guide.pdf")
    assert blocked.status == "robots_disallowed"
    assert allowed.status == "ok" and allowed.text == "" and allowed.content.startswith(b"%PDF")
    assert "/files/guide.pdf" not in requested, "被 robots 禁止的附件不能发出请求"


def test_redirect_to_disallowed_host_is_not_requested() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://example.com/page.html"})

    with PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        url_allowed=lambda url: (httpx.URL(url).host or "").endswith(".gov.cn"),
        resolver=public_dns,
    ) as fetcher:
        result = fetcher.fetch("https://a.gov.cn/page.html")
    assert result.status == "unsupported_host"
    assert all("example.com" not in url for url in requested)


def test_robots_redirect_to_disallowed_host_skips_page() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert request.url.path == "/robots.txt"
        return httpx.Response(302, headers={"location": "https://example.com/robots.txt"})

    with PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        url_allowed=lambda url: (httpx.URL(url).host or "").endswith(".gov.cn"),
        resolver=public_dns,
    ) as fetcher:
        result = fetcher.fetch("https://a.gov.cn/page.html")
    assert result.status == "unsupported_host"
    assert requested == ["https://a.gov.cn/robots.txt"]


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


def test_pdf_size_limit_applies_while_streaming() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=b"%PDF-" + b"x" * MAX_RESPONSE_BYTES,
            headers={"content-type": "application/pdf"},
        )

    with make_fetcher(handler) as f:
        result = f.fetch("https://a.gov.cn/large.pdf")
    assert result.status == "too_large"
    assert result.content == b""


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
        resolver=public_dns,
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
        resolver=public_dns,
    )
    with fetcher as f:
        result = f._get("https://bad-cert.gov.cn/list.html")
    assert result.status == "ssl_error"
    assert "证书" in result.error
    assert compat_calls == []


def test_redirect_is_followed_hop_by_hop_with_robots_check() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            if request.url.host == "b.gov.cn":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
            return httpx.Response(404)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://a.gov.cn/new"})
        if request.url.path == "/new":
            return httpx.Response(200, text=LIST_HTML)
        if request.url.path == "/to-b":
            return httpx.Response(302, headers={"location": "https://b.gov.cn/private/x.html"})
        raise AssertionError(f"不应请求 {request.url}")

    with make_fetcher(handler) as f:
        ok = f.fetch("https://a.gov.cn/old")
        blocked = f.fetch("https://a.gov.cn/to-b")
    assert ok.status == "ok"
    assert ok.url == "https://a.gov.cn/old"
    assert ok.final_url == "https://a.gov.cn/new"
    # 跳到另一个网站时，按该网站的 robots.txt 判断；被禁止的页面不发请求
    assert blocked.status == "robots_disallowed"
    assert "b.gov.cn/private/x.html" not in requested


def test_redirect_loop_is_cut_off() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "/loop"})

    with make_fetcher(handler) as f:
        result = f.fetch("https://a.gov.cn/loop")
    assert result.status == "network_error"
    assert "重定向" in result.error


def test_robots_txt_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http" and request.url.path == "/robots.txt":
            return httpx.Response(301, headers={"location": "https://a.gov.cn/robots.txt"})
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError("robots 全站禁止时不应抓取页面")

    with make_fetcher(handler) as f:
        assert f.fetch("http://a.gov.cn/list.html").status == "robots_disallowed"


def test_redirect_target_host_is_rate_limited() -> None:
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
        if request.url.host == "a.gov.cn":
            return httpx.Response(302, headers={"location": "https://b.gov.cn/list.html"})
        return httpx.Response(200, text=LIST_HTML)

    with make_fetcher(handler, interval=3.0, clock=clock, sleep=sleep) as f:
        assert f.fetch("https://a.gov.cn/go").status == "ok"
    # a：robots → 等 3 秒 → 页面（302）；b：robots → 等 3 秒 → 页面
    assert sleeps == [3.0, 3.0]


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",  # 云服务器元数据地址
        "100.64.0.1",  # 运营商级 NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:10.0.0.1",
        "64:ff9b::a00:1",  # NAT64 里嵌的 10.0.0.1
    ],
)
def test_non_public_addresses(address: str) -> None:
    assert non_public_address(address)


@pytest.mark.parametrize(
    "address", ["1.2.3.4", "2400:3200::1", "::ffff:8.8.8.8", "64:ff9b::808:808"]
)
def test_public_addresses(address: str) -> None:
    assert not non_public_address(address)


def test_host_resolving_to_internal_address_is_never_requested() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(404)

    def dns(host: str) -> list[str]:
        # 同时有公网和内网地址时也不连：实际连接可能落到任何一个
        return ["1.2.3.4", "10.8.0.3"] if host == "oa.a.gov.cn" else ["1.2.3.4"]

    with make_fetcher(handler, resolver=dns) as f:
        result = f.fetch("https://oa.a.gov.cn/list.html")
    assert result.status == "unsupported_host"
    assert "oa.a.gov.cn 解析到内网地址" in result.error
    assert requested == []


def test_redirect_to_internal_host_is_blocked() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://meta.a.gov.cn/latest/"})

    def dns(host: str) -> list[str]:
        return ["169.254.169.254"] if host == "meta.a.gov.cn" else ["1.2.3.4"]

    with make_fetcher(handler, resolver=dns) as f:
        result = f.fetch("https://a.gov.cn/page.html")
    assert result.status == "unsupported_host"
    assert requested == ["a.gov.cn/robots.txt", "a.gov.cn/page.html"]


def test_robots_redirect_to_internal_host_skips_site() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(302, headers={"location": "https://in.a.gov.cn/robots.txt"})

    def dns(host: str) -> list[str]:
        return ["192.168.3.4"] if host == "in.a.gov.cn" else ["1.2.3.4"]

    with make_fetcher(handler, resolver=dns) as f:
        result = f.fetch("https://a.gov.cn/page.html")
    assert result.status == "unsupported_host"
    assert "in.a.gov.cn 解析到内网地址" in result.error
    assert requested == ["a.gov.cn/robots.txt"]


def test_dns_failure_counts_as_connection_failure() -> None:
    lookups: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("解析失败时不应发请求")

    def dns(host: str) -> list[str]:
        lookups.append(host)
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    with make_fetcher(handler, resolver=dns) as f:
        result = f.fetch("https://gone.gov.cn/list.html")
    # 和连接失败一样重试一次；robots.txt 取不到，按规范整站跳过
    assert result.status == "robots_error"
    assert lookups == ["gone.gov.cn", "gone.gov.cn"]


def test_dns_lookup_has_a_time_limit(monkeypatch) -> None:
    release = threading.Event()

    def stuck(*args, **kwargs):
        release.wait(5)
        return []

    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", stuck)
    try:
        # 超时是 OSError，和解析失败一样按连接失败处理
        with pytest.raises(OSError):
            resolve_host("slow.gov.cn", timeout=0.05)
    finally:
        release.set()


class _FakeNetworkStream:
    def __init__(self, address: tuple[str, int]) -> None:
        self.address = address

    def get_extra_info(self, name: str):
        return self.address if name == "server_addr" else None


def test_response_from_internal_peer_is_discarded() -> None:
    # 检查时解析到公网地址，实际连接却落到内网（DNS 在两次解析之间被改动）：丢弃响应，不读正文
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                404, extensions={"network_stream": _FakeNetworkStream(("1.2.3.4", 443))}
            )
        return httpx.Response(
            200,
            text=LIST_HTML,
            extensions={"network_stream": _FakeNetworkStream(("10.0.0.7", 443))},
        )

    with make_fetcher(handler) as f:
        result = f.fetch("https://a.gov.cn/list.html")
    assert result.status == "unsupported_host"
    assert "实际连接到内网地址" in result.error
    assert result.text == ""


def test_missing_peer_address_is_refused_when_required() -> None:
    # 走真实网络时拿不到实际连接的地址（例如以后换了传输方式）就不抓，而不是放行
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                404, extensions={"network_stream": _FakeNetworkStream(("1.2.3.4", 443))}
            )
        return httpx.Response(200, text=LIST_HTML)

    with PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
        real_network=True,
    ) as f:
        result = f.fetch("https://a.gov.cn/list.html")
    assert result.status == "unsupported_host"
    assert "拿不到实际连接的地址" in result.error


def test_real_network_requires_peer_address_by_default() -> None:
    with PoliteFetcher("TestAgent YoujuBot/0.1") as real:
        assert real._real == {"main": True, "compat": True}
    with make_fetcher(lambda request: httpx.Response(404)) as mocked:
        assert mocked._real == {"main": False, "compat": False}
    # 只注入兼容模式的传输时，默认那一路仍是真实网络，照样检查
    compat_only = httpx.MockTransport(lambda request: httpx.Response(404))
    with PoliteFetcher("TestAgent YoujuBot/0.1", compat_transport=compat_only) as f:
        assert f._real == {"main": True, "compat": False}


def test_stuck_dns_lookups_do_not_block_later_lookups(monkeypatch) -> None:
    release = threading.Event()

    def lookup(host, *args, **kwargs):
        if host == "stuck.gov.cn":
            release.wait(5)
            return []
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))]

    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", lookup)
    try:
        # 十个卡住的解析都超时，之后的解析照样马上返回
        for _ in range(10):
            with pytest.raises(OSError):
                resolve_host("stuck.gov.cn", timeout=0.01)
        assert resolve_host("ok.gov.cn", timeout=1) == ["1.2.3.4"]
    finally:
        release.set()


def test_real_network_connects_to_the_checked_address() -> None:
    # 走真实网络时：连接固定到检查过的 IP，Host 头和 SNI 仍是原域名；结果里的网址仍是原网址
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.url.host, request.headers["host"], request.extensions.get("sni_hostname"))
        )
        peer = {"network_stream": _FakeNetworkStream(("93.184.216.34", 443))}
        if request.url.path == "/robots.txt":
            return httpx.Response(404, extensions=peer)
        return httpx.Response(200, text=LIST_HTML, extensions=peer)

    def dns(host: str) -> list[str]:
        return ["93.184.216.34"] if host == "a.gov.cn" else ["1.2.3.4"]

    with PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=dns,
        real_network=True,
    ) as f:
        result = f.fetch("https://a.gov.cn/list.html")
    assert result.status == "ok"
    assert result.final_url == "https://a.gov.cn/list.html"
    assert seen == [
        ("93.184.216.34", "a.gov.cn", "a.gov.cn"),
        ("93.184.216.34", "a.gov.cn", "a.gov.cn"),
    ]


def test_pending_dns_lookups_are_capped(monkeypatch) -> None:
    # 解析卡住时，同时在等的解析有上限，守护线程不会无限增加
    release = threading.Event()

    def stuck(*args, **kwargs):
        release.wait(5)
        return []

    monkeypatch.setattr(fetcher_module.socket, "getaddrinfo", stuck)
    monkeypatch.setattr(fetcher_module, "_DNS_SLOTS", threading.BoundedSemaphore(2))
    try:
        for _ in range(2):
            with pytest.raises(OSError):
                resolve_host("slow.gov.cn", timeout=0.01)
        with pytest.raises(OSError, match="过多"):
            resolve_host("slow.gov.cn", timeout=5)
    finally:
        release.set()


def test_empty_dns_answer_never_falls_back_to_hostname() -> None:
    # 解析没有得到地址：按解析失败处理（重试一次），不能退回按域名连接
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("没有检查过的地址时不应发请求")

    with PoliteFetcher(
        "TestAgent YoujuBot/0.1",
        per_host_interval=0,
        transport=httpx.MockTransport(handler),
        resolver=lambda host: [],
        real_network=True,
    ) as f:
        result = f._get("https://a.gov.cn/list.html")
    assert result.status == "network_error"
    assert "没有得到地址" in result.error
