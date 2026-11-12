"""正文能读率门（D-129）：每省每级取 60 份申报通知、政策试取正文，能读 90% 以上才放。"""

from datetime import UTC, datetime
from itertools import count

from youju.crawler.fetcher import FetchResult
from youju.crawler.urls import url_key
from youju.govsites.bodygate import read_gate, run_gate, write_gate
from youju.models import Document, Source

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
GOOD = (
    "<html><meta name='PubDate' content='2026-11-05'><body><div id='zoom'>"
    "<p>苏工信〔2026〕1号</p><p>各有关单位：现将申报事项通知如下，请按要求组织申报，材料报送截止时间为"
    "2026年12月1日，逾期不予受理。</p><p>江苏省某某局</p><p>2026年11月5日</p></div></body></html>"
)
EMPTY = "<html><body><div class='nav'>首页 | 机构 | 联系我们</div></body></html>"
_SERIAL = count()


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return FetchResult(url, "ok", final_url=url, http_status=200, text=self.pages[url])


def _source(session, n, status="试跑可读（全国覆盖 v1，未启用）", province="江苏"):
    s = Source(
        level="市级",
        province=province,
        city="南京",
        dept="工信",
        dept_full_name="南京市工业和信息化局",
        column_name="通知公告",
        url=f"https://gxj.nanjing.gov.cn/tzgg{n}/",
        fetch_mode="static",
        enabled=False,
        parser_config={},
        verification_status=status,
    )
    session.add(s)
    session.flush()
    return s


def _docs(session, s, titles):
    out = []
    for title in titles:
        url = f"https://gxj.nanjing.gov.cn/art/{s.id}_{next(_SERIAL)}.html"
        d = Document(
            source_id=s.id,
            title=title,
            url=url,
            url_key=url_key(url),
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        session.add(d)
        out.append(d)
    session.flush()
    return out


def test_body_gate_reads_bodies_and_writes_evidence(session, tmp_path):
    s = _source(session, 1)
    notices = _docs(
        session, s, [f"关于组织申报2026年第{n}批专精特新中小企业的通知" for n in range(22)]
    )
    _docs(session, s, ["关于召开全市安全生产会议的通知"] * 5)  # 资讯与指南：不抽
    other = _source(session, 2, status="待核")  # 没试抓过的来源：不算
    _docs(session, other, ["某市促进制造业高质量发展的若干政策措施"] * 30)
    session.commit()
    pages = {d.url: GOOD for d in notices}
    pages[notices[0].url] = EMPTY
    pages[notices[1].url] = EMPTY
    fake = FakeFetcher(pages)
    gate = run_gate(
        session, "市级", "江苏", seed="t", platforms={s.url: "大汉"}, fetcher=fake, now=NOW
    )
    # 22 份不足 60：全取；20 份能读 → 91% 过
    assert gate.available == 22 and len(gate.rows) == 22 and len(fake.calls) == 22
    assert gate.readable == 20 and gate.passed
    assert gate.by_platform() == [("大汉", 20, 22)]
    bad = [r for r in gate.rows if r["能读"] == "读不了"]
    assert len(bad) == 2 and all(r["原因"] for r in bad)
    path = write_gate(tmp_path, gate)
    assert path == tmp_path / "抽查" / "市级" / "正文" / "江苏.csv"
    assert read_gate(tmp_path / "抽查" / "市级", "江苏") == (22, 20)
    # 重跑不再取已取过的正文
    again = run_gate(session, "市级", "江苏", seed="t", fetcher=fake, now=NOW)
    assert again.readable == 20 and len(fake.calls) == 22


def test_body_gate_needs_twenty(session, tmp_path):
    """不足 20 份先不放，也不取正文；证据表里记了不通过的栏目不算。"""
    s = _source(session, 1)
    _docs(session, s, [f"关于组织申报第{n}批项目的通知" for n in range(15)])
    skipped = _source(session, 2)
    _docs(session, skipped, [f"关于组织申报第{n}批项目的通知" for n in range(10)])
    session.commit()
    evidence = tmp_path / "江苏.csv"
    evidence.write_text(
        f"层级,省份,栏目URL,自动结论,复核人,复核结论\n市级,江苏,{skipped.url},待人工,某某,不通过\n",
        encoding="utf-8-sig",
    )
    fake = FakeFetcher({})
    gate = run_gate(session, "市级", "江苏", seed="t", evidence=evidence, fetcher=fake, now=NOW)
    assert gate.available == 15 and not gate.rows and not gate.passed and not fake.calls
    assert "不足 20 份" in gate.text()
    write_gate(tmp_path, gate)
    assert read_gate(tmp_path / "抽查" / "市级", "江苏") == (0, 0)
    assert read_gate(tmp_path / "抽查" / "市级", "浙江") is None


def test_body_gate_only_on_named_local_trial_db(session, capsys):
    """取正文要写库：要写明试抓库的库名、连的是本机库、库里没有在收的来源，不然不取。"""
    import argparse

    from youju.govsites.bodygate import check_trial_db
    from youju.govsites.cli import _cmd_body_gate

    name = session.get_bind().url.database
    assert "--trial-db" in check_trial_db(session, None)
    assert "--trial-db" in check_trial_db(session, "youju")
    assert check_trial_db(session, name) is None
    live = _source(session, 9)
    live.enabled = True
    session.commit()
    assert "在收的来源" in check_trial_db(session, name)
    args = argparse.Namespace(
        level="市级", province=["江苏"], seed="t", no_fetch=False, trial_db=None, out=None
    )
    assert _cmd_body_gate(args) == 1
    assert "不取正文" in capsys.readouterr().out


def test_body_gate_fetcher_waits_three_seconds(monkeypatch):
    """同一网站两次请求至少隔 3 秒，不管配置里调成多少。"""
    from youju.config import get_settings
    from youju.govsites.bodygate import polite_fetcher

    monkeypatch.setattr(get_settings(), "per_host_interval", 0.5)
    with polite_fetcher() as fetcher:
        assert fetcher._interval == 3.0


def test_body_gate_command_shares_one_fetcher(session, tmp_path, monkeypatch, capsys):
    """整次命令共用一个抓取器：几个省访问同一个网站时，限速记录接得上。"""
    import argparse
    from contextlib import contextmanager

    import youju.govsites.bodygate as bodygate
    from youju.govsites.cli import _cmd_body_gate

    pages = {}
    for n, prov in enumerate(("江苏", "浙江")):
        s = _source(session, 10 + n, province=prov)
        for d in _docs(session, s, [f"关于组织申报第{i}批项目的通知" for i in range(20)]):
            pages[d.url] = GOOD
    session.commit()
    fake = FakeFetcher(pages)
    opened = []

    @contextmanager
    def one_fetcher():
        opened.append(1)
        yield fake

    monkeypatch.setattr(bodygate, "polite_fetcher", one_fetcher)
    monkeypatch.setattr(bodygate, "check_trial_db", lambda session, name: None)
    args = argparse.Namespace(
        level="市级",
        province=["江苏", "浙江"],
        seed="t",
        no_fetch=False,
        trial_db="x",
        out=tmp_path,
    )
    assert _cmd_body_gate(args) == 0
    assert opened == [1] and len(fake.calls) == 40
    assert capsys.readouterr().out.count("能读 20 份") == 2
