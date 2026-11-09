"""每省抽查证据表（D-122、D-126）：自动核五项、结论按证据表算。"""

from datetime import UTC, date, datetime

from youju.crawler.urls import url_key
from youju.govsites.sample import evidence_results, sample_rows, to_csv_row
from youju.models import Document, Source, SourceCheck

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def _source(session, url, full_name, dept="科技", column="通知公告", province="浙江"):
    s = Source(
        level="省级",
        province=province,
        dept=dept,
        dept_full_name=full_name,
        column_name=column,
        url=url,
        fetch_mode="static",
        enabled=False,
        parser_config={},
        verification_status="试跑可读（全国覆盖 v1，未启用）",
    )
    session.add(s)
    session.flush()
    return s


def _check(session, s, items):
    session.add(
        SourceCheck(
            source_id=s.id,
            started_at=NOW,
            finished_at=NOW,
            status="ok",
            items_found=items,
            items_new=items,
        )
    )


def _doc(session, s, n, day, host=None):
    url = f"https://{host or s.url.split('/')[2]}/art/{n}.html"
    session.add(
        Document(
            source_id=s.id,
            title=f"关于第 {n} 批项目的通知",
            url=url,
            url_key=url_key(url),
            publish_date=day,
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )


def test_sample_checks_five_items(session):
    good = _source(session, "https://kjt.zj.gov.cn/tzgg/", "省科学技术厅")  # 只带本级前缀也算本省
    _check(session, good, 12)
    for n, day in enumerate((date(2026, 11, 5), date(2026, 10, 1), date(2026, 9, 1))):
        _doc(session, good, n, day)
    foreign = _source(session, "https://kw.beijing.gov.cn/tzgg/", "北京市科学技术委员会")
    _check(session, foreign, 10)
    _doc(session, foreign, 10, date(2026, 11, 1))
    stale = _source(
        session, "https://czt.zj.gov.cn/zcwj/", "浙江省财政厅", dept="财政", column="政策文件"
    )
    _check(session, stale, 8)
    _doc(session, stale, 20, date(2024, 1, 1))
    _doc(session, stale, 21, date(2023, 6, 1), host="www.gov.cn")
    session.commit()
    rows = {r.source.url: r for r in sample_rows(session, "省级", now=NOW)}
    assert rows[good.url].verdict == "自动通过"
    assert rows[foreign.url].failed == ["部门"]  # 外省部门
    assert rows[stale.url].failed == ["新鲜", "本站"]
    line = to_csv_row(rows[good.url])
    assert line[7] == "通知公告" and line[16] == "自动通过" and line[-3:] == ["", "", ""]


def test_evidence_results_count_reviews(tmp_path):
    head = "层级,省份,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"
    (tmp_path / "浙江.csv").write_text(
        head
        + "省级,浙江,u1,自动通过,,,,\n"
        + "省级,浙江,u2,待人工,新鲜,王某,通过,更新慢\n"
        + "省级,浙江,u3,待人工,部门,王某,不通过,挂错\n"
        + "省级,浙江,u4,自动通过,,,,\n"
        + "省级,浙江,u5,自动通过,,,,\n"
        + "省级,浙江,u6,自动通过,,独立复核,不通过,读成年报\n",
        encoding="utf-8-sig",
    )
    (tmp_path / "江苏.csv").write_text(head + "省级,江苏,v1,待人工,条目,,,\n", encoding="utf-8-sig")
    # 这次没抓到的记待重试：不算进通过率；读得对、但不在收录范围的记不收：算读得对
    # （5 个里 3 个通过、1 个不收、1 个待重试 → 4/4 = 100%）
    (tmp_path / "广东.csv").write_text(
        head
        + "省级,广东,w1,自动通过,,,,\n"
        + "省级,广东,w2,自动通过,,,,\n"
        + "省级,广东,w3,自动通过,,,,\n"
        + "省级,广东,w4,待人工,条目,王某,待重试,robots.txt 暂时取不到\n"
        + "省级,广东,w5,自动通过,,协调会话,不收（整栏招聘公示）,和企业政策无关\n",
        encoding="utf-8-sig",
    )
    # 不收算读得对：3 个通过、1 个不通过、1 个不收 → 4/5 = 80%，合格（不算进分母是 3/4、
    # 算错是 3/5，都不合格）——第 1 批上海就是这样
    (tmp_path / "上海.csv").write_text(
        head
        + "省级,上海,s1,自动通过,,,,\n"
        + "省级,上海,s2,自动通过,,,,\n"
        + "省级,上海,s3,自动通过,,,,\n"
        + "省级,上海,s4,待人工,要闻,王某,不通过,时政要闻\n"
        + "省级,上海,s5,自动通过,,王某,不收,整栏是个人事项公示\n",
        encoding="utf-8-sig",
    )
    res = {r.province: r for r in evidence_results(tmp_path)}
    zj, js = res["浙江"], res["江苏"]
    # 自动通过但被复核否掉的算不通过：6 个里通过 4 个，67% 不合格
    assert (zj.total, zj.passed, zj.failed, zj.accepted) == (6, 4, 2, False)
    assert (js.pending, js.accepted) == (1, False)  # 没复核完不算合格
    gd = res["广东"]
    assert (gd.retry, gd.excluded, gd.rate, gd.accepted) == (1, 1, 1.0, True)
    sh = res["上海"]
    assert (sh.passed, sh.failed, sh.excluded, sh.rate, sh.accepted) == (3, 1, 1, 0.8, True)


def test_batch_keeps_only_passed_columns_of_accepted_provinces(tmp_path):
    import argparse
    import csv

    from youju.govsites.cli import _cmd_batch
    from youju.govsites.report import COLUMN_FIELDS

    def column(prov, url, status="试跑可读（全国覆盖 v1，未启用）"):
        row = [""] * len(COLUMN_FIELDS)
        row[0], row[1], row[7], row[11] = "省级", prov, url, status
        return row

    lists = tmp_path / "栏目清单"
    lists.mkdir()
    for prov, urls in {"浙江": ["a1", "a2", "a3", "a4", "a5"], "福建": ["b1", "b2"]}.items():
        with (lists / f"{prov}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMN_FIELDS)
            w.writerows(column(prov, u) for u in urls)
    evid = tmp_path / "抽查" / "省级"
    evid.mkdir(parents=True)
    head = "层级,省份,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"
    (evid / "浙江.csv").write_text(
        head
        + "".join(f"省级,浙江,a{n},自动通过,,,,\n" for n in (1, 2, 3))
        + "省级,浙江,a4,待人工,年报,王某,不通过,读成年报\n"
        + "省级,浙江,a5,自动通过,,协调会话,不收,整栏是事业单位招聘公示\n",
        encoding="utf-8-sig",
    )
    (evid / "福建.csv").write_text(
        head + "省级,福建,b1,自动通过,,,,\n省级,福建,b2,自动通过,,王某,不通过,上级文件\n",
        encoding="utf-8-sig",
    )
    args = argparse.Namespace(level=["省级"], name="第1批", out=tmp_path)
    assert _cmd_batch(args) == 0
    with (tmp_path / "批次" / "第1批.csv").open(encoding="utf-8-sig", newline="") as fh:
        urls = [r[7] for r in list(csv.reader(fh))[1:]]
    # 浙江 4/5 合格（a5 不收：读得对，算进通过率，但不进批次），只收通过的 3 个；
    # 福建 1/2 不合格，整省不收
    assert urls == ["a1", "a2", "a3"]


def test_final_verdict_reads_wording_by_prefix():
    from youju.govsites.sample import final_verdict

    auto = {"自动结论": "自动通过"}
    assert final_verdict({**auto, "复核结论": "不通过（部门挂错）"}) == "不通过"
    assert final_verdict({**auto, "复核结论": " 通过 "}) == "通过"
    assert final_verdict({**auto, "复核结论": "有问题"}) == "待复核"  # 认不出的词要人看
    assert final_verdict({**auto, "复核结论": "待重试（没抓到）"}) == "待重试"
    assert final_verdict({**auto, "复核结论": "不收（整栏招聘公示）"}) == "不收"
    assert final_verdict({**auto, "复核结论": ""}) == "通过"
    assert final_verdict({"自动结论": "待人工", "复核结论": ""}) == "待复核"


def test_batch_skips_lists_already_live(tmp_path):
    import argparse
    import csv

    from youju.govsites.cli import _cmd_batch
    from youju.govsites.report import COLUMN_FIELDS

    out = tmp_path / "全国覆盖"
    (out / "栏目清单").mkdir(parents=True)
    row = [""] * len(COLUMN_FIELDS)
    row[0], row[1], row[7], row[11] = "省级", "浙江", "https://kjt.zj.gov.cn/tzgg/", "试跑可读"
    row2 = list(row)
    row2[7] = "https://czt.zj.gov.cn/tzgg/"
    with (out / "栏目清单" / "浙江.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMN_FIELDS)
        w.writerows([row, row2])
    (out / "抽查" / "省级").mkdir(parents=True)
    (out / "抽查" / "省级" / "浙江.csv").write_text(
        "层级,省份,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"
        "省级,浙江,https://kjt.zj.gov.cn/tzgg/,自动通过,,,,\n"
        "省级,浙江,https://czt.zj.gov.cn/tzgg/,自动通过,,,,\n",
        encoding="utf-8-sig",
    )
    # 线上已在收的来源清单里是 http 写法：同一个列表，不进本批
    (tmp_path / "全国来源清单_P0_v1.csv").write_text(
        "栏目URL\nhttp://kjt.zj.gov.cn/tzgg/\n", encoding="utf-8-sig"
    )
    assert _cmd_batch(argparse.Namespace(level=["省级"], name="第1批", out=out)) == 0
    with (out / "批次" / "第1批.csv").open(encoding="utf-8-sig", newline="") as fh:
        assert [r[7] for r in list(csv.reader(fh))[1:]] == ["https://czt.zj.gov.cn/tzgg/"]


def test_committed_batch_matches_evidence():
    """提交的第 1 批导入文件要和用证据表重算的一致：复核结论改了却忘了重生成批次，这里会失败。"""
    import csv
    from pathlib import Path

    from youju.govsites.cli import DEFAULT_OUT, build_batch

    path = DEFAULT_OUT / "批次" / "第1批_国家级和省级.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        committed = list(csv.reader(fh))[1:]
    plan = build_batch(Path(DEFAULT_OUT), ["国家级", "省级"])
    assert plan.unknown == []
    assert plan.rows == committed


def test_batch_urls_drop_rows_failed_after_batch(tmp_path, capsys):
    import argparse

    from youju.govsites.cli import _batch_urls, _cmd_enable

    (tmp_path / "批次").mkdir()
    (tmp_path / "抽查" / "省级").mkdir(parents=True)
    batch = tmp_path / "批次" / "第1批.csv"
    batch.write_text(
        "层级,省份,栏目URL\n省级,浙江,u1\n省级,浙江,u2\n省级,江苏,v1\n", encoding="utf-8-sig"
    )
    (tmp_path / "抽查" / "省级" / "浙江.csv").write_text(
        "层级,省份,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"
        "省级,浙江,u1,自动通过,,,,\n"
        "省级,浙江,u2,自动通过,,创始人,不通过（部门不对）,复看\n",
        encoding="utf-8-sig",
    )
    # 批次生成后复核改成不通过的不开；只取这个省这一级
    assert _batch_urls(batch, "浙江", "省级") == ["u1"]
    # --verified 不带 --batch 不开
    args = argparse.Namespace(
        province="浙江", level="省级", by="王某", limit=10, dry_run=True, verified=True, batch=None
    )
    assert _cmd_enable(args) == 1
    assert "--batch" in capsys.readouterr().out
