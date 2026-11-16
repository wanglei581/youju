"""跨进程限速：几个进程共用一个目录下的锁文件和时间戳，同一网站隔够间隔、同一地址同时在飞的
不超过名额。全部用模拟传输，不发真实网络请求；子进程用 spawn 起，跑本模块里的函数。"""

import multiprocessing
import os
import time

import httpx
import pytest

from youju.crawler.fetcher import PoliteFetcher, _SharedLimits

ADDRESS = "1.2.3.4"  # 只当固定连接的地址用，模拟传输不连网


def _fetcher(
    shared, *, host_interval=0.0, address_interval=0.0, concurrency=2, record=None, hold=0.0
):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        start = time.time()
        if hold:
            time.sleep(hold)
        if record is not None:
            record.put((request.url.host, start, time.time()))
        return httpx.Response(200, text="<html>ok</html>", request=request)

    return PoliteFetcher(
        "YoujuBot-test",
        per_host_interval=host_interval,
        transport=httpx.MockTransport(handler),
        real_network=False,
        resolver=lambda host: [ADDRESS],
        per_address_interval=address_interval,
        per_address_concurrency=concurrency,
        shared_limits_dir=shared,
    )


def _worker(shared, host, record, ready, go, host_interval, address_interval, concurrency, hold):
    with _fetcher(
        shared,
        host_interval=host_interval,
        address_interval=address_interval,
        concurrency=concurrency,
        record=record,
        hold=hold,
    ) as fetcher:
        ready.put(os.getpid())
        go.wait(30)
        result = fetcher.fetch(f"https://{host}/page")
        record.put(("done", result.status, 0.0))


def _run(tmp_path, hosts, **kw):
    ctx = multiprocessing.get_context("spawn")
    record, ready, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    args = (
        kw.get("host_interval", 0.0),
        kw.get("address_interval", 0.0),
        kw.get("concurrency", 2),
        kw.get("hold", 0.0),
    )
    procs = [
        ctx.Process(target=_worker, args=(str(tmp_path), host, record, ready, go, *args))
        for host in hosts
    ]
    for p in procs:
        p.start()
    for _ in procs:
        ready.get(timeout=60)
    go.set()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    rows = []
    while not record.empty():
        rows.append(record.get())
    done = [r for r in rows if r[0] == "done"]
    assert [r[1] for r in done] == ["ok"] * len(hosts)
    return sorted((r for r in rows if r[0] != "done"), key=lambda r: r[1])


def test_same_host_across_processes_waits_interval(tmp_path):
    pages = _run(tmp_path, ["a.example.gov.cn", "a.example.gov.cn"], host_interval=0.4)
    assert len(pages) == 2
    # 前一个请求结束到后一个开始至少隔 per_host_interval
    assert pages[1][1] - pages[0][2] >= 0.38


def test_same_address_across_processes_spaces_starts(tmp_path):
    pages = _run(tmp_path, ["a.example.gov.cn", "b.example.gov.cn"], address_interval=0.4)
    assert len(pages) == 2
    assert pages[1][1] - pages[0][1] >= 0.38


def test_same_address_concurrency_across_processes(tmp_path):
    hosts = ["a.example.gov.cn", "b.example.gov.cn", "c.example.gov.cn"]
    pages = _run(tmp_path, hosts, concurrency=2, hold=0.6)
    assert len(pages) == 3
    # 任一时刻同时在飞的不超过 2 个：第三个必须等前面某个结束才开始
    events = sorted([(start, 1) for _, start, _ in pages] + [(end, -1) for _, _, end in pages])
    flying = peak = 0
    for _, step in events:
        flying += step
        peak = max(peak, flying)
    assert peak <= 2
    assert pages[2][1] >= min(end for _, _, end in pages[:2]) - 0.01


def test_unusable_dir_stops_instead_of_falling_back(tmp_path):
    from youju.crawler.fetcher import SharedLimitError

    # 配了跨进程限速的目录却用不了：停下不抓（不悄悄只管本进程，整机的同站间隔就保证不了）
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    with pytest.raises(SharedLimitError):
        _fetcher(str(blocked))


def test_read_only_dir_midway_stops(tmp_path):
    from youju.crawler.fetcher import SharedLimitError

    shared = tmp_path / "shared"
    shared.mkdir()
    with _fetcher(str(shared), host_interval=0.1) as fetcher:
        for f in shared.iterdir():
            f.unlink()
        shared.chmod(0o500)  # 中途写不进去了
        try:
            with pytest.raises(SharedLimitError):
                fetcher.fetch("https://a.example.gov.cn/page")
        finally:
            shared.chmod(0o700)


def test_not_configured_dir_keeps_process_limits(tmp_path):
    # 没配目录（空串）的照旧只管本进程，不报错
    with _fetcher(None) as fetcher:
        assert fetcher.fetch("https://a.example.gov.cn/page").status == "ok"


def test_gate_closing_after_wait_releases_everything(tmp_path):
    calls = []

    def gate(url, *, record):
        calls.append(record)
        # 页面请求：等之前那次（只查）放行，等完以后那次（要记）不放行
        return not (url.endswith("/page") and record)

    with _fetcher(str(tmp_path), host_interval=0.1, address_interval=0.1) as fetcher:
        result = fetcher.fetch("https://a.example.gov.cn/page", hop_allowed=gate)
        assert result.status == "site_paused"
        # 名额、锁都放掉了：后面的请求照常，不会卡住
        started = time.monotonic()
        assert fetcher.fetch("https://a.example.gov.cn/next").status == "ok"
        assert time.monotonic() - started < 5
    assert True in calls and False in calls


def test_wall_clock_rollback_does_not_wait_long(tmp_path):
    limits = _SharedLimits(str(tmp_path))
    # 别的进程记下的时间比现在晚一小时（墙上时间往回调过）：等满一个间隔，不会等出一小时
    future = repr(time.time() + 3600)
    limits.path("h", "a.example.gov.cn").write_text(future)
    limits.path("a", ADDRESS).write_text(future)
    with _fetcher(str(tmp_path), host_interval=0.5, address_interval=0.5) as fetcher:
        started = time.monotonic()
        assert fetcher.fetch("https://a.example.gov.cn/page").status == "ok"
        # robots.txt 和页面两次请求：同一主机隔 0.5 秒，同一地址隔 0.5 秒，不会等出一小时
        assert time.monotonic() - started < 3


def test_empty_stamp_is_a_new_record(tmp_path):
    limits = _SharedLimits(str(tmp_path))
    limits.path("h", "a.example.gov.cn").write_text("")
    with _fetcher(str(tmp_path), host_interval=0.2) as fetcher:
        assert fetcher.fetch("https://a.example.gov.cn/page").status == "ok"
    # 定长写、不先清空：写完的记录不会是空的
    assert float(limits.path("h", "a.example.gov.cn").read_text().strip()) > 0


@pytest.mark.parametrize(
    # 后两种比定长（32 字节）长：修过以后尾巴不能还在（不然以后每次运行都停）
    "value",
    ["abc", "nan", "inf", "\x00\x00", "x" * 50, "1700000000.0" + "x" * 40],
)
def test_bad_stamp_is_repaired_then_stops(tmp_path, value):
    from youju.crawler.fetcher import SharedLimitError

    limits = _SharedLimits(str(tmp_path))
    limits.path("h", "a.example.gov.cn").write_text(value)
    with _fetcher(str(tmp_path), host_interval=0.3) as fetcher:
        # 读不出来的记录（写到一半被杀、乱写）：先改成现在的时间再停下，报错写明是哪个文件
        with pytest.raises(SharedLimitError) as raised:
            fetcher.fetch("https://a.example.gov.cn/page")
        assert str(limits.path("h", "a.example.gov.cn")) in str(raised.value)
        assert float(limits.path("h", "a.example.gov.cn").read_text().strip()) > 0
        # 修好以后照常，而且从修好的时间起等满一个间隔
        started = time.monotonic()
        assert fetcher.fetch("https://a.example.gov.cn/page").status == "ok"
        assert time.monotonic() - started >= 0.2


def test_host_stamp_written_right_before_sending(tmp_path):
    # 真正发出前记一次开始时间：请求进行中别的进程读到的就是这次的开始（进程在请求中途被杀，下一个
    # 也照样要隔够间隔），不是上一个请求结束的时间
    limits = _SharedLimits(str(tmp_path))
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        seen.append(float(limits.path("h", "a.example.gov.cn").read_text().strip()))
        return httpx.Response(200, text="<html>ok</html>", request=request)

    with PoliteFetcher(
        "YoujuBot-test",
        per_host_interval=0.05,
        transport=httpx.MockTransport(handler),
        real_network=False,
        resolver=lambda host: [ADDRESS],
        per_address_interval=0.0,
        per_address_concurrency=2,
        shared_limits_dir=str(tmp_path),
    ) as fetcher:
        assert fetcher.fetch("https://a.example.gov.cn/1").status == "ok"
        time.sleep(0.2)
        started = time.time()
        assert fetcher.fetch("https://a.example.gov.cn/2").status == "ok"
    assert seen[-1] >= started
