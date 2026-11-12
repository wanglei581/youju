"""每省抽查证据表（D-122、D-126）：自动核五项、结论按证据表算。"""

from datetime import UTC, date, datetime

import pytest

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
    (tmp_path / "四川.csv").write_text(
        head
        + "".join(f"省级,四川,c{n},自动通过,,,,\n" for n in (1, 2, 3))
        + "省级,四川,c4,待人工,年报,王某,不通过,读成年报\n"
        + "".join(
            f"省级,四川,c{n},自动通过,,王某,不能抓（列表靠页面脚本另取）,访问控制\n"
            for n in (5, 6, 7)
        ),
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
    # 不能抓（按访问控制不读）不算进分母：3 个通过、1 个不通过、3 个不能抓 → 3/4 = 75%，不合格
    zj2 = res["四川"]
    assert (zj2.blocked, zj2.rate, zj2.accepted) == (3, 0.75, False)


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
    assert final_verdict({**auto, "复核结论": "不能抓（访问控制）"}) == "不能抓"
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


@pytest.mark.parametrize(
    ("province", "city", "dept", "full", "ok"),
    [
        # 地市部门全称写本市名称、省名连市名、只写本级前缀"市"：算本市
        ("江苏", "南京市", "发改委", "南京市发展和改革委员会", True),
        ("江苏", "南京市", "发改委", "江苏省南京市发展和改革委员会", True),
        ("江苏", "南京市", "数据局", "市大数据局", True),
        ("湖北", "恩施土家族苗族自治州", "农业农村", "恩施州农业农村局", True),
        ("湖北", "恩施土家族苗族自治州", "农业农村", "湖北省恩施州农业农村局", True),
        ("内蒙古", "阿拉善盟", "发改委", "盟发展和改革委员会", True),
        # 地市门户的写法
        ("江苏", "南京市", "政府办", "中共南京市委 南京市人民政府", True),
        ("江苏", "南京市", "政府办", "南京市政府门户网站", True),
        ("江苏", "南京市", "政府办", "南京政府网", True),
        ("内蒙古", "阿拉善盟", "政府办", "阿拉善盟行政公署", True),
        # 只写省名的是省直部门；别的市的部门、下辖区县的政府：都不算
        ("江苏", "南京市", "发改委", "江苏省发展和改革委员会", False),
        ("江苏", "南京市", "发改委", "苏州市发展和改革委员会", False),
        ("江苏", "南京市", "政府办", "南京市某某区人民政府", False),
        ("江苏", "南京市", "政府办", "南京市某某区政府", False),
        ("江苏", "南京市", "政府办", "南京市某某县人民政府", False),
        # 市名和省名一样（吉林市）、和别的省撞上（青海海南州）：以省份全称开头的要紧跟本市名称
        ("吉林", "吉林市", "发改委", "吉林市发展和改革委员会", True),
        ("吉林", "吉林市", "发改委", "吉林省吉林市发展和改革委员会", True),
        ("吉林", "吉林市", "发改委", "吉林省发展和改革委员会", False),
        ("吉林", "吉林市", "市场监管", "吉林省市场监督管理厅", False),
        ("青海", "海南藏族自治州", "发改委", "海南州发展和改革委员会", True),
        ("青海", "海南藏族自治州", "发改委", "海南省发展和改革委员会", False),
        # 本市名称后面紧跟下辖区县全称的是区县的部门（区县名单取自行政区划清单）
        ("吉林", "吉林市", "发改委", "吉林省吉林市丰满区发展和改革局", False),
        ("江苏", "苏州市", "政府办", "苏州市常熟市人民政府", False),
        ("江苏", "南京市", "发改委", "南京市玄武区发展和改革委员会", False),
        ("山东", "济南市", "发改委", "市中区发展和改革局", False),
        ("湖北", "恩施土家族苗族自治州", "农业农村", "恩施市农业农村局", False),
        ("湖北", "荆州市", "发改委", "荆州区发展和改革局", False),
        ("湖北", "荆州市", "发改委", "荆州市发展和改革委员会", True),
        ("江苏", "苏州市", "政府办", "苏州市人民政府", True),
        # 库里地级市写简称（以"州"结尾的也是地级市）
        ("江苏", "苏州", "政府办", "苏州市常熟市人民政府", False),
        ("江苏", "苏州", "政府办", "苏州市人民政府", True),
        ("江苏", "苏州", "发改委", "州发展和改革委员会", False),
        ("江苏", "南京", "发改委", "南京市玄武区发展和改革委员会", False),
        ("江苏", "南京", "发改委", "南京市发展和改革委员会", True),
        # 自治州写简称（"恩施州"）也认本级前缀"州"
        ("湖北", "恩施州", "农业农村", "州农业农村局", True),
        ("湖北", "恩施州", "农业农村", "恩施州农业农村局", True),
        ("湖北", "恩施州", "农业农村", "恩施市农业农村局", False),
    ],
)
def test_city_dept_names(province, city, dept, full, ok):
    """11/11 地市一组报：部门核对原来只认省名，568 个地市栏目里 552 个卡在"部门"。"""
    from types import SimpleNamespace

    from youju.govsites.sample import _dept_ok

    counties = {
        "吉林市": {"丰满区", "船营区", "永吉县", "蛟河市"},
        "苏州市": {"姑苏区", "常熟市", "昆山市"},
        "苏州": {"姑苏区", "常熟市", "昆山市"},
        "南京市": {"玄武区", "秦淮区", "浦口区"},
        "南京": {"玄武区", "秦淮区", "浦口区"},
        "济南市": {"历下区", "市中区", "平阴县"},
        "恩施土家族苗族自治州": {"恩施市", "利川市", "建始县"},
        "恩施州": {"恩施市", "利川市", "建始县"},
        "荆州市": {"荆州区", "沙市区", "沙市", "公安县"},
    }.get(city, set())
    s = SimpleNamespace(level="市级", province=province, city=city, dept=dept, dept_full_name=full)
    assert _dept_ok(s, frozenset(counties)) is ok


def test_counties_by_city_keeps_full_names(tmp_path):
    """区县名单只取带区、县、市、旗的写法（"公安县"的"公安"会碰上"荆州市公安局"）；城市认全称和
    简称；清单不存在时报错。"""
    from youju.govsites.sample import counties_by_city

    path = tmp_path / "行政区划.csv"
    path.write_text(
        "区划代码,名称,类型,层级,省份,城市,区县,上级代码\n"
        "421000,荆州市,地级市,市级,湖北,荆州,,420000\n"
        "421003,荆州区,市辖区,区县级,湖北,荆州,荆州区,421000\n"
        "421022,公安县,县,区县级,湖北,荆州,公安县,421000\n"
        "422800,恩施土家族苗族自治州,自治州,市级,湖北,恩施土家族苗族自治州,,420000\n"
        "422801,恩施市,县级市,区县级,湖北,恩施土家族苗族自治州,恩施市,422800\n",
        encoding="utf-8-sig",
    )
    got = counties_by_city(path)
    assert got[("湖北", "荆州")] == got[("湖北", "荆州市")] == {"荆州区", "公安县"}
    for city in ("恩施土家族苗族自治州", "恩施州", "恩施"):
        assert got[("湖北", city)] == {"恩施市"}
    with pytest.raises(FileNotFoundError):
        counties_by_city(tmp_path / "没有.csv")


def test_city_sample_rows_use_division_list(session):
    """用仓库里的行政区划清单走 sample_rows：来源写"吉林市"还是"吉林"都认得出下辖区县的部门。"""
    from pathlib import Path

    from youju.govsites.sample import counties_by_city

    divisions = Path(__file__).parents[2] / "docs" / "数据来源" / "全国覆盖" / "行政区划.csv"
    urls = {}
    for city, full in (
        ("吉林市", "吉林省吉林市丰满区发展和改革局"),
        ("吉林", "吉林市船营区发展和改革局"),
        ("苏州", "苏州市常熟市人民政府"),
        ("吉林市", "吉林市发展和改革委员会"),
    ):
        dept = "政府办" if full.endswith("人民政府") else "发改委"
        s = Source(
            level="市级",
            province="江苏" if city == "苏州" else "吉林",
            city=city,
            dept=dept,
            dept_full_name=full,
            column_name="通知公告",
            url=f"https://example{len(urls)}.gov.cn/tzgg/",
            fetch_mode="static",
            enabled=False,
            parser_config={},
            verification_status="试跑可读（全国覆盖 v1，未启用）",
        )
        session.add(s)
        session.flush()
        _check(session, s, 12)
        for n, day in enumerate((date(2026, 11, 5), date(2026, 10, 1), date(2026, 9, 1))):
            _doc(session, s, n + 10 * len(urls), day)
        urls[full] = s.url
    session.commit()
    rows = {
        r.source.url: r
        for r in sample_rows(session, "市级", now=NOW, counties=counties_by_city(divisions))
    }
    for full, url in urls.items():
        assert ("部门" in rows[url].failed) is (full != "吉林市发展和改革委员会"), full


def test_city_sample_needs_division_list(tmp_path, capsys):
    """没有行政区划清单就不做地市抽查（少了它会把区县的部门当成本市的）；不指定时用 --out 目录里的。"""
    import argparse

    from youju.govsites.cli import _cmd_sample

    args = argparse.Namespace(
        level="市级", province=None, size=None, out=tmp_path, divisions=tmp_path / "没有.csv"
    )
    assert _cmd_sample(args) == 1
    assert "没有.csv" in capsys.readouterr().out
    args.divisions = None
    assert _cmd_sample(args) == 1
    assert str(tmp_path / "行政区划.csv") in capsys.readouterr().out


def test_autonomous_matches_division_list():
    """地级市以"州"结尾的都是两个字、自治州和盟写全称：_autonomous 和行政区划清单的类型对得上。"""
    import csv
    from pathlib import Path

    from youju.govsites.sample import _autonomous

    path = Path(__file__).parents[2] / "docs" / "数据来源" / "全国覆盖" / "行政区划.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        cities = [r for r in csv.DictReader(fh) if r["层级"] == "市级"]
    assert len(cities) > 300
    for r in cities:
        assert _autonomous(r["城市"]) is (r["类型"] in ("自治州", "盟")), r["城市"]
    assert _autonomous("恩施州") and not _autonomous("苏州")


@pytest.mark.parametrize(
    ("full", "ok"),
    [
        ("江苏省人民政府", True),
        ("江苏省人民政府办公厅", True),
        # 地市门户的新写法、去掉"中共"只给市级：省级照旧
        ("中共江苏省委 江苏省人民政府", False),
        ("江苏省政府门户网站", False),
    ],
)
def test_province_portal_names_unchanged(full, ok):
    from types import SimpleNamespace

    from youju.govsites.sample import _dept_ok

    s = SimpleNamespace(level="省级", province="江苏", city="", dept="政府办", dept_full_name=full)
    assert _dept_ok(s) is ok


def _round_of(who):
    return who.removeprefix("联网核对").removeprefix("第").removesuffix("次") or "1"


def _city_evidence(folder, province, cells, sample, *, level="市级", body=(60, 58), log="auto"):
    """写一省的证据表：cells 个格各一个栏目、都通过；sample 是 [(格号, 复核人, 结论[, 复核备注])]，
    复核备注默认写这一次的种子（"种子：s1""种子：s2"……）。log 是联网核对记录的行
    [(层, 次数, 种子, 凭据)]，"auto" 照样本里出现的每层每次各记一行（种子 s1、s2……，重抽写凭据），
    None 不写。body 是正文能读率门（抽了几份、能读几份），None 是还没测。"""
    head = "层级,省份,城市,区县,部门,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"
    marked = {}
    rounds = set()
    for n, who, verdict, *note in sample:
        default = f"种子：s{_round_of(who)}"
        marked[n] = (who, verdict, note[0] if note else default)
        layer = "门户" if level == "区县级" and n % 3 == 0 else "部门"
        rounds.add((layer, _round_of(who)))
    if log == "auto":
        log = [(lay, r, f"s{r}", "" if r == "1" else "改了列表规则") for lay, r in sorted(rounds)]
    record = folder / "联网核对" / f"{province}.csv"
    record.unlink(missing_ok=True)
    if log is not None:
        record.parent.mkdir(exist_ok=True)
        rows = "".join(
            f"{lay},{r},{seed},{rest[1] if len(rest) > 1 else '2026-11-12'},{rest[0]}\n"
            for lay, r, seed, *rest in log
        )
        record.write_text("层,次数,种子,日期,凭据\n" + rows, encoding="utf-8-sig")
    lines = []
    for n in range(cells):
        who, verdict, note = marked.get(n, ("", "", ""))
        dept = "政府办" if level == "区县级" and n % 3 == 0 else f"部门{n}"
        lines.append(
            f"{level},{province},城{n % 7},区{n},{dept},u{n},自动通过,,{who},{verdict},{note}\n"
        )
    (folder / f"{province}.csv").write_text(head + "".join(lines), encoding="utf-8-sig")
    gate = folder / "正文"
    (gate / f"{province}.csv").unlink(missing_ok=True)
    if body is not None:
        gate.mkdir(exist_ok=True)
        rows = "".join(
            f"{i},,,,u{i},平台,标题{i},d{i},政策,{'能读' if i < body[1] else '读不了'},\n"
            for i in range(body[0])
        )
        (gate / f"{province}.csv").write_text(
            "序号,城市,区县,部门,栏目URL,平台,文件标题,文件网址,类型,能读,原因\n" + rows,
            encoding="utf-8-sig",
        )


def test_city_sample_line(tmp_path):
    """地市：联网核对第 1 次抽 40 格、不通过不超过 3 格才放；重抽 60 格、不超过 5 格，只看最近一次。"""
    from youju.govsites.sample import evidence_results

    def result(sample, cells=120, body=(60, 58)):
        _city_evidence(tmp_path, "河南", cells, sample, body=body)
        [res] = evidence_results(tmp_path)
        return res

    first = [(n, "联网核对", "不通过" if n < 3 else "通过") for n in range(40)]
    assert result(first).accepted
    line = result(first).sample_lines[0]
    assert (line.round, line.judged, line.failed, line.need, line.allow) == (1, 40, 3, 40, 3)
    four = [(n, "联网核对", "不通过" if n < 4 else "通过") for n in range(40)]
    assert not result(four).accepted  # 错 4 格：不放（全表读得准的照样过八成）
    assert result(four).rate >= 0.8
    assert not result(first[:39]).accepted  # 不够 40 格
    # 待重试的格不算样本
    retry = [*first[:39], (39, "联网核对", "待重试")]
    assert not result(retry).accepted
    # 重抽：看最近一次（第 2 次 60 格、错 5 格过；第 1 次的错不算进来）
    redraw = four + [(40 + n, "联网核对第2次", "不通过" if n < 5 else "通过") for n in range(60)]
    res = result(redraw)
    assert res.accepted and res.sample_lines[0].need == 60
    cn = [(40 + n, "联网核对第二次", "不通过" if n < 5 else "通过", "") for n in range(60)]
    assert result(four + cn).sample_lines[0].round == 2 and result(four + cn).accepted
    assert not result(four + [(40 + n, "联网核对第2次", "通过") for n in range(59)]).accepted
    # 一层的格比 40 少：全抽
    small = [(n, "联网核对", "通过") for n in range(25)]
    assert result(small, cells=25).accepted
    # 正文能读率门：没测、不到 90% 都不放
    assert not result(first, body=None).accepted
    assert not result(first, body=(60, 53)).accepted
    assert "正文能读率" in result(first).sample_note()


def test_city_sample_round_evidence(tmp_path):
    """最近一次只要写了就算最近一次（全是待重试也一样）；联网核对记录要每次一行、种子不重复、
    重抽写凭据，证据表里的种子要和记录对上；空着的复核结论不算；第 4 次不算。"""
    from youju.govsites.sample import evidence_results

    def result(sample, log="auto"):
        _city_evidence(tmp_path, "河南", 200, sample, log=log)
        [res] = evidence_results(tmp_path)
        return res

    first = [(n, "联网核对", "不通过" if n < 3 else "通过") for n in range(40)]
    assert result(first).accepted
    # 第 2 次刚开头、全是待重试：按第 2 次算（0 格），不再按第 1 次放
    pending = first + [(40 + n, "联网核对第2次", "待重试") for n in range(60)]
    line = result(pending).sample_lines[0]
    assert (line.round, line.judged) == (2, 0) and not result(pending).accepted
    # 联网核对记录：没有、没写种子、记了两遍
    assert "没有" in result(first, log=None).sample_note()
    assert "没写种子" in result(first, log=[("部门", "1", "", "")]).sample_note()
    twice = [("部门", "1", "s1", ""), ("部门", "1", "s1", "")]
    assert "记了几遍" in result(first, log=twice).sample_note()
    # 重抽：要换种子、要写凭据；证据表里写的种子要和记录对上
    four = [(n, "联网核对", "不通过" if n < 4 else "通过") for n in range(40)]
    redraw = four + [(40 + n, "联网核对第2次", "通过") for n in range(60)]
    assert result(redraw).accepted
    reused = [("部门", "1", "s1", ""), ("部门", "2", "s1", "改了列表规则")]
    assert "用过" in result(redraw, log=reused).sample_note()
    no_proof = [("部门", "1", "s1", ""), ("部门", "2", "s2", "")]
    assert "没写凭据" in result(redraw, log=no_proof).sample_note()
    other = four + [(40 + n, "联网核对第2次", "通过", "种子：s9") for n in range(60)]
    assert "对不上" in result(other).sample_note()
    # 证据表里不写种子也行（以记录为准）
    assert result(four + [(40 + n, "联网核对第2次", "通过", "") for n in range(60)]).accepted
    # 空着的复核结论不会退回自动结论：这一格没做完
    blank = first[:39] + [(39, "联网核对", "")]
    assert result(blank).sample_lines[0].judged == 39 and not result(blank).accepted
    # 重抽两次还不过：第 4 次、第十一次都不算
    fourth = [(100 + n, "联网核对第4次", "通过") for n in range(60)]
    assert "整省逐格排查" in result(fourth).sample_note() and not result(fourth).accepted
    eleventh = [(100 + n, "联网核对第十一次", "通过") for n in range(60)]
    assert result(eleventh).sample_lines[0].round == 11 and not result(eleventh).accepted
    # 次数认不出（"第2回"、记录里次数空着）：不放，不会退回第 1 次
    odd = [(n, "联网核对第2回", "通过") for n in range(40)]
    assert "认不出" in result(odd, log=[("部门", "1", "s1", "")]).sample_note()
    assert (
        "认不出"
        in result(first, log=[("部门", "1", "s1", ""), ("部门", "", "s2", "")]).sample_note()
    )
    # 第 0 次（复核人、记录里）也认不出
    zero = [(n, "联网核对第0次", "通过") for n in range(40)]
    assert "认不出" in result(zero, log=[("部门", "1", "s1", "")]).sample_note()
    assert (
        "认不出"
        in result(first, log=[("部门", "1", "s1", ""), ("部门", "0", "s0", "")]).sample_note()
    )
    # 记录里写了证据表里没有的层（地市的重抽记成"门户"）：不放
    wrong_layer = [("部门", "1", "s1", ""), ("门户", "2", "s2", "改了列表规则")]
    assert "层" in result(first, log=wrong_layer).sample_note()
    assert not result(first, log=wrong_layer).accepted
    # 层空着的也不放（不默认当"部门"）
    assert "没写层" in result(first, log=[("", "1", "s1", "")]).sample_note()
    # 记录里已经记了第 2 次、证据表还没填：按第 2 次算，不再按第 1 次放
    ahead = [("部门", "1", "s1", ""), ("部门", "2", "s2", "改了列表规则")]
    line = result(first, log=ahead).sample_lines[0]
    assert (line.round, line.judged) == (2, 0) and not result(first, log=ahead).accepted
    # 日期要写对；以前那次证据表里写的种子也要和记录对上
    assert "日期不对" in result(first, log=[("部门", "1", "s1", "", "")]).sample_note()
    old_seed = [(n, w, v, "种子：s7") for n, w, v in four] + redraw[40:]
    assert "第 1 次写的种子" in result(old_seed).sample_note()


def test_city_sample_counts_cells(tmp_path):
    """按格算：同一格两个栏目都填了联网核对，一个不通过就算这一格不通过，只算一格。"""
    from youju.govsites.sample import evidence_results

    head = "层级,省份,城市,区县,部门,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"
    rows = "".join(
        f"市级,河南,城{n},,部门,u{n},自动通过,,联网核对,通过,种子：s1\n" for n in range(38)
    )
    rows += "市级,河南,城38,,部门,a,自动通过,,联网核对,通过,种子：s1\n"
    rows += "市级,河南,城38,,部门,b,自动通过,,联网核对,不通过,种子：s1\n"
    # 同一格一个通过、一个待重试：这一格没做完，不算样本
    rows += "市级,河南,城39,,部门,c,自动通过,,联网核对,通过,种子：s1\n"
    rows += "市级,河南,城39,,部门,d,自动通过,,联网核对,待重试,种子：s1\n"
    (tmp_path / "河南.csv").write_text(head + rows, encoding="utf-8-sig")
    (tmp_path / "联网核对").mkdir()
    (tmp_path / "联网核对" / "河南.csv").write_text(
        "层,次数,种子,日期,凭据\n部门,1,s1,2026-11-12,\n", encoding="utf-8-sig"
    )
    [res] = evidence_results(tmp_path)
    line = res.sample_lines[0]
    assert (line.judged, line.failed, line.problem) == (39, 1, "") and not line.ok


def test_county_sample_two_layers(tmp_path):
    """区县分两层各自判：县直部门 40 格 ≤3、县政府门户 30 格 ≤2，门户好盖不住部门的毛病。"""
    from youju.govsites.sample import evidence_results

    # 150 格：n % 3 == 0 的是门户（50 格），其余是部门（100 格）
    gov = [n for n in range(150) if n % 3 == 0][:30]
    dept = [n for n in range(150) if n % 3][:40]
    sample = [(n, "联网核对", "不通过" if i < 2 else "通过") for i, n in enumerate(gov)]
    sample += [(n, "联网核对", "不通过" if i < 3 else "通过") for i, n in enumerate(dept)]
    _city_evidence(tmp_path, "湖南", 150, sample, level="区县级")
    [res] = evidence_results(tmp_path)
    assert res.accepted and [line.layer for line in res.sample_lines] == ["部门", "门户"]
    sample[0] = (gov[0], "联网核对", "不通过")
    sample[2] = (gov[2], "联网核对", "不通过")  # 门户错 3 格
    _city_evidence(tmp_path, "湖南", 150, sample, level="区县级")
    [res] = evidence_results(tmp_path)
    assert not res.accepted


def test_province_level_keeps_full_check(tmp_path):
    """国家级、省级照旧全检：不要抽样线，也不要正文能读率门；有联网核对行时，空着的复核结论照旧
    按自动结论算（"只认人工结论"只用在地市、区县）。"""
    from youju.govsites.sample import evidence_results

    _city_evidence(tmp_path, "浙江", 10, [], level="省级", body=None)
    [res] = evidence_results(tmp_path)
    assert res.accepted and res.sample_note() == ""
    _city_evidence(tmp_path, "浙江", 10, [(n, "联网核对", "") for n in range(3)], level="省级")
    [res] = evidence_results(tmp_path)
    assert res.sample_judged == 3 and res.sample_rate == 1.0 and res.accepted


def test_city_personal_flag(session):
    """个人事项门：地市、区县栏目最新 20 条里个人名单类、招聘考试任免类占 60% 以上的交给人看；
    省级不加这一项；不足 5 条的不判。"""
    from youju.govsites.sample import personal_share

    def source(url, level, titles):
        s = Source(
            level=level,
            province="江苏",
            city="南京" if level == "市级" else None,
            dept="人社",
            dept_full_name="南京市人力资源和社会保障局" if level == "市级" else "江苏省人社厅",
            column_name="公示公告",
            url=url,
            fetch_mode="static",
            enabled=False,
            parser_config={},
            verification_status="试跑可读（全国覆盖 v1，未启用）",
        )
        session.add(s)
        session.flush()
        _check(session, s, 20)
        for n, title in enumerate(titles):
            doc_url = f"https://{url.split('/')[2]}/art/{s.id}_{n}.html"
            session.add(
                Document(
                    source_id=s.id,
                    title=title,
                    url=doc_url,
                    url_key=url_key(doc_url),
                    publish_date=date(2026, 11, 1 + n % 9),
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )
        return s

    personal = [f"2026年第{n}批提前退休人员情况公示" for n in range(13)]
    policies = [f"关于开展第{n}期职业技能培训的通知" for n in range(7)]
    heavy = source("https://rsj.nanjing.gov.cn/gsgg/", "市级", personal + policies)
    mixed = source("https://rsj.nanjing.gov.cn/tzgg/", "市级", personal[:5] + policies * 2)
    source("https://jshrss.jiangsu.gov.cn/gsgg/", "省级", personal + policies)
    session.commit()
    city = {r.source.url: r for r in sample_rows(session, "市级", now=NOW)}
    assert "个人事项" in city[heavy.url].failed  # 13/20 = 65%
    assert "个人事项" not in city[mixed.url].failed  # 5/19
    assert "个人事项" not in sample_rows(session, "省级", now=NOW)[0].failed
    assert personal_share(personal[:4]) is None  # 不足 5 条不判
    # 不认"遴选"（选承保机构、招标代理也叫遴选），处罚、失信这类也不算个人名单
    assert (
        personal_share(["承保机构公开遴选公告"] * 5 + ["拖欠农民工工资失信联合惩戒对象名单"] * 5)
        == 0
    )


def test_development_zone_cells_by_full_name(tmp_path):
    """开发区一个城市有几格（部门都记"开发区"）：按部门全称分开算；别的部门写法不同也算同一格。"""
    from youju.govsites.sample import evidence_results

    head = "层级,省份,城市,区县,部门,部门全称,栏目URL,自动结论,没过的项,复核人,复核结论,复核备注\n"
    rows = "".join(
        f"市级,四川,南充,,开发区,南充{name},z{n},自动通过,,联网核对,通过,\n"
        for n, name in enumerate(("经济开发区管委会", "高新技术产业园区管委会"))
    )
    rows += "市级,四川,南充,,发改委,南充市发展和改革委员会,a,自动通过,,联网核对,通过,\n"
    rows += "市级,四川,南充,,发改委,南充市发改委,b,自动通过,,联网核对,通过,\n"
    (tmp_path / "四川.csv").write_text(head + rows, encoding="utf-8-sig")
    [res] = evidence_results(tmp_path)
    assert len(res.cells["部门"]) == 3 and res.sample_lines[0].judged == 3


def test_city_upper_flag(session):
    """上级粗筛：地市、区县栏目最新 10 条里像上级文件的占六成以上，记"上级"、转人工；
    混着本级文件的不记；区县栏目里市里的文件也算上级。"""
    from youju.govsites.sample import upper_level_title

    def source(url, titles, level="市级", district=None):
        s = Source(
            level=level,
            province="江苏",
            city="南京",
            district=district,
            dept="工信",
            dept_full_name="南京市工业和信息化局"
            if level == "市级"
            else "南京市玄武区工业和信息化局",
            column_name="政策法规",
            url=url,
            fetch_mode="static",
            enabled=False,
            parser_config={},
            verification_status="试跑可读（全国覆盖 v1，未启用）",
        )
        session.add(s)
        session.flush()
        _check(session, s, len(titles))
        for n, title in enumerate(titles):
            doc_url = f"https://{url.split('/')[2]}/art/{s.id}_{n}.html"
            session.add(
                Document(
                    source_id=s.id,
                    title=title,
                    url=doc_url,
                    url_key=url_key(doc_url),
                    publish_date=date(2026, 11, 1 + n % 9),
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )
        return s

    provincial = [f"江苏省工业和信息化厅关于第{n}批项目的通知" for n in range(5)]
    provincial += ["省财政厅关于下达专项资金的通知", "国务院办公厅关于促进制造业发展的意见"]
    own = [f"南京市工业和信息化局关于第{n}批项目的通知" for n in range(6)]
    upper = source("https://gxj.nanjing.gov.cn/zcfg/", provincial + own[:3])
    mixed = source("https://gxj.nanjing.gov.cn/tzgg/", provincial[:4] + own)
    county = source(
        "https://www.xuanwu.gov.cn/gxj/",
        [f"南京市工业和信息化局关于第{n}批项目的通知" for n in range(7)]
        + [f"玄武区工业和信息化局关于第{n}批项目的通知" for n in range(3)],
        level="区县级",
        district="玄武区",
    )
    session.commit()
    city = {r.source.url: r for r in sample_rows(session, "市级", now=NOW)}
    assert "上级" in city[upper.url].failed  # 7/10
    assert "上级" not in city[mixed.url].failed  # 4/10
    [row] = [r for r in sample_rows(session, "区县级", now=NOW) if r.source.url == county.url]
    assert "上级" in row.failed  # 区县栏目里市里的文件 7/10
    assert not upper_level_title(upper, "南京市人民政府办公厅关于转发《江苏省……》的通知")
    assert upper_level_title(upper, "《关于促进民营经济发展的若干措施》")
