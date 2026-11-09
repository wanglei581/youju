"""覆盖抽查（govsites/spotcheck.py）的纯函数：不连数据库、不联网。"""

from datetime import date

from youju.govsites import spotcheck as sc


def _row(code: str, dept: str, state: str = "已收录", level: str = "市级") -> dict:
    return {
        "层级": level,
        "省份": "江苏",
        "城市": "南京",
        "区划代码": code,
        "部门": dept,
        "状态": state,
        "栏目网址": f"https://x{code}.gov.cn/{dept}/",
    }


def test_pick_cells_reproducible_and_filtered() -> None:
    rows = [_row(str(320100 + i), "发改委") for i in range(30)]
    rows += [_row("320199", "工信", state="需人工"), _row("320000", "科技", level="省级")]
    first = sc.pick_cells(rows, n=20)
    assert len(first) == 20
    assert first == sc.pick_cells(list(reversed(rows)), n=20)
    assert all(r["状态"] == "已收录" and r["层级"] == "市级" for r in first)
    assert sc.pick_cells(rows, n=20, seed="另一批") != first
    assert len(sc.pick_cells(rows[:5], n=20)) == 5


def test_norm_title_strips_truncation_and_punctuation() -> None:
    assert sc.norm_title("关于开展2026年“专精特新”企业申报的通知...") == (
        "关于开展2026年专精特新企业申报的通知"
    )
    assert sc.norm_title("《南京市　促进办法》（试行）……") == "南京市促进办法试行"
    assert sc.norm_title("ＡＢＣ１２３ 通知") == "ABC123通知"


def test_title_check() -> None:
    page = (
        "<html><title>首页</title><body><h1>关于开展2026年度专精特新企业申报工作的通知</h1></body>"
    )
    assert sc.title_check("关于开展2026年度专精特新企业申报工作的通...", page)[0] == "一致"
    assert sc.title_check("关于开展2026年度“专精特新”企业申报工作的通知", page)[0] == "一致"
    verdict, note = sc.title_check("关于开展2026年度专精特新企业申报工作的补充通知", page)
    assert verdict == "基本一致" and "%" in note
    assert sc.title_check("市场监管局公示名单", page)[0] == "不一致"
    assert sc.title_check("", page) == ("不一致", "库里没有标题")


def test_date_check_forms_and_labels() -> None:
    d = date(2026, 11, 5)
    assert sc.date_check(d, "<span>发布日期：</span><span>2026-11-05</span>")[0] == "一致"
    assert sc.date_check(d, "发布时间: 2026/11/5 10:00")[0] == "一致"
    assert sc.date_check(d, "成文日期：2026年11月5日")[0] == "一致"
    verdict, note = sc.date_check(d, "发布日期：2026-11-04")
    assert verdict == "页面写的是别的日期" and "2026-11-04" in note
    assert sc.date_check(d, "正文写于 2026.11.05 会议")[0] == "一致"
    assert sc.date_check(d, "正文没有日期")[0] == "页面上找不到日期"
    assert sc.date_check(d, "编号 2026-11-050")[0] == "页面上找不到日期"
    assert sc.date_check(None, "发布日期：2026-11-05") == ("页面上找不到日期", "库里没有发布日期")


def test_attribution_check() -> None:
    cell = {"城市": "南京", "部门": "发改委"}
    col = '<meta name="SiteName" content="南京市发展和改革委员会"><title>通知公告</title>'
    assert sc.attribution_check(cell, col, "<title>某通知</title>")[0] == "本市本部门"
    portal = '<meta name="SiteName" content="南京市人民政府"><title>部门频道_南京市人民政府</title>'
    assert sc.attribution_check(cell, portal, "")[0] == "本市"
    assert sc.attribution_check({"城市": "南京", "部门": "政府办"}, portal, "")[0] == "本市本部门"
    other = '<meta name="SiteName" content="江苏省发展和改革委员会">'
    verdict, note = sc.attribution_check(cell, other, "")
    assert verdict == "待看" and "南京" in note
    enshi = {"城市": "恩施土家族苗族自治州", "部门": "科技"}
    page = "<title>恩施州科学技术局</title>"
    assert sc.attribution_check(enshi, page, "")[0] == "本市本部门"


def test_render_markdown_escapes_pipes() -> None:
    row = {f: "" for f in sc.FIELDS}
    row.update(
        序号=1,
        城市="南京",
        部门="发改委",
        栏目网址="https://fgw.nanjing.gov.cn/col/1/",
        文件标题="关于A|B的通知",
        文件网址="https://fgw.nanjing.gov.cn/art/1.html",
        发布日期="2026-11-05",
        标题核对="一致",
        日期核对="一致",
        归属核对="本市本部门",
        程序结论=sc.OK,
    )
    text = sc.render_markdown("江苏", "市级", 12, "2026-11", [row], date(2026, 11, 9))
    assert "关于A\\|B的通知" in text
    assert "程序判合格 1/1" in text
    assert "不足 20 个" in text


def test_write_outputs_names_files_by_level(tmp_path) -> None:
    row = {f: "" for f in sc.FIELDS}
    row.update(序号=1, 程序结论=sc.OK)
    csv_path, md_path = sc.write_outputs(
        tmp_path, "江苏", "市级", 1, "2026-11", [row], date(2026, 11, 9)
    )
    assert csv_path.name == "江苏_市级.csv" and md_path.name == "江苏_市级.md"
    assert csv_path.parent.name == "抽查"
