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
    # 只在正文里有这个日期：不算核对过，交给人看
    assert sc.date_check(d, "正文写于 2026.11.05 会议")[0] == "原文没标发布日期"
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
    # 写到 <out>/<省>/联网核对_<级>.csv；抽查/<级>/<省>.csv 是主线 coverage-sample 的统一证据表
    assert csv_path.name == "联网核对_市级.csv" and md_path.name == "联网核对_市级.md"
    assert csv_path.parent.name == "江苏"


def test_attribution_uses_issuer_and_strips_site_noise() -> None:
    cell = {"城市": "焦作", "部门": "农业农村"}
    col = "<title>欢迎访问焦作市农业农村局网站！</title>"
    assert sc.attribution_check(cell, col, "")[0] == "本市本部门"
    jieyang = {"城市": "揭阳", "部门": "人社"}
    portal = '<meta name="SiteName" content="揭阳市人民政府门户网站"><title>通知公告</title>'
    doc = (
        "<title>关于某某的公告</title><p>来源：揭阳市人力资源和社会保障局 发布时间：2026-11-03</p>"
    )
    assert sc.attribution_check(jieyang, portal, doc)[0] == "本市本部门"
    zone = {"城市": "潍坊", "部门": "开发区"}
    assert sc.attribution_check(zone, "<title>潍坊高新技术产业开发区</title>", "")[0] == (
        "本市本部门"
    )
    gz = {"城市": "广州", "部门": "发改委"}
    doc2 = "<title>广州市发展和改革委员会关于举办培训的通知 | 广州市发展和改革委员会网站</title>"
    assert sc.attribution_check(gz, "<title>通知公告</title>", doc2)[0] == "本市本部门"


def test_date_check_meta_and_document_dates() -> None:
    d = date(2026, 11, 3)
    meta = '<meta name="PubDate" content="2026-11-03 15:07"><p>正文</p>'
    assert sc.date_check(d, meta)[0] == "一致"
    # "公示时间""征求意见时间"不是发布日期
    assert sc.date_check(d, "公示时间：2026年11月4日至10日 2026-11-03")[0] == "原文没标发布日期"
    only_written = "成文日期：2026-10-28 文号：某府规〔2026〕5号"
    assert sc.date_check(d, only_written)[0] == "原文只标成文日期"
    both = "成文日期 2026年01月28日 发布时间 2026年02月10日"
    verdict, note = sc.date_check(date(2026, 5, 20), both)
    assert verdict == "页面写的是别的日期" and "2026-02-10" in note


def test_attribution_by_portal_domain() -> None:
    cell = {"城市": "邵阳", "部门": "市场监管", "栏目网址": "https://amr.shaoyang.gov.cn/tzgg/"}
    col = "<title>通知公告_市市场监督管理局</title>"
    assert sc.attribution_check(cell, col, "")[0] == "待看"
    verdict, note = sc.attribution_check(cell, col, "", "shaoyang.gov.cn")
    assert verdict == "本市本部门" and "shaoyang.gov.cn" in note


def test_date_labels_inside_sentences_are_ignored() -> None:
    page = "【信息时间：2026-10-28 阅读次数： 】 3.询价公告发布日期：2026年9月8日。"
    assert sc.date_check(date(2026, 10, 28), page)[0] == "一致"


def test_polluted_list_titles_are_not_accepted() -> None:
    page = "<h1>本溪市医疗器械网络销售信息告知（2026年22号）</h1>"
    stored = "标题：本溪市医疗器械网络销售信息告知（2026年22号） 点击数：33 发表时间：2026-11-06"
    assert sc.title_check(stored, page)[0] == "不一致"


def test_reposted_upper_level_document_does_not_prove_department() -> None:
    """市局栏目转载的省厅文件：标题里的"省科学技术厅"不能证明这一格是本市科技局。"""
    cell = {"城市": "许昌", "部门": "科技", "栏目网址": "https://www.xuchang.gov.cn/tzgg/"}
    col = "<title>通知公告</title>"
    doc = "<title>河南省科学技术厅关于2027年度省重点研发专项答辩评审的预通知</title>"
    assert sc.attribution_check(cell, col, doc, "xuchang.gov.cn")[0] == "本市"
    col2 = "<title>通知公告_许昌市科学技术局</title>"
    assert sc.attribution_check(cell, col2, doc, "xuchang.gov.cn")[0] == "本市本部门"


def test_county_site_is_flagged() -> None:
    cell = {"城市": "扬州", "部门": "发改委", "栏目网址": "http://www.yangzhou.gov.cn/gyq/fgw/"}
    col = "<title>通知公告_扬州市广陵区发展和改革局</title>"
    counties = frozenset({"广陵区", "广陵"})
    verdict, note = sc.attribution_check(cell, col, "", "yangzhou.gov.cn", counties)
    assert verdict == "待看" and "广陵" in note


def test_date_uses_first_labeled_publication_date() -> None:
    page = "发布日期：2026-11-05 正文…… 相关文章 发布日期：2026-11-03"
    assert sc.date_check(date(2026, 11, 5), page)[0] == "一致"
    assert sc.date_check(date(2026, 11, 3), page)[0] == "页面写的是别的日期"


def test_visible_date_beats_meta_and_city_named_counties_ignored() -> None:
    page = '<meta name="PubDate" content="2026-11-04 19:32"><p>成文日期: 2026-11-03 发布日期: 2026-11-03</p>'
    assert sc.date_check(date(2026, 11, 3), page)[0] == "一致"
    cell = {
        "城市": "淮安",
        "部门": "文旅",
        "栏目网址": "https://wgj.huaian.gov.cn/zwgk/gggs/list.html",
    }
    col = "<title>淮安市文化广电和旅游局</title>"
    counties = frozenset({"淮安区", "淮安", "清江浦区", "清江浦"})
    assert sc.attribution_check(cell, col, "", "huaian.gov.cn", counties)[0] == "本市本部门"


def test_date_next_to_title_beats_sidebar_dates() -> None:
    """阜新：侧栏新闻的"发布时间"排在正文前面；以标题后面紧挨着的日期为准。"""
    page = (
        "<div>辽宁信息 更多 某某会议召开 发布时间：2026-10-13</div>"
        "<h1>关于开展2026年全市政策性粮油库存检查工作的通知</h1><p>日期： 2026-04-27 浏览量：270</p>"
    )
    title = "关于开展2026年全市政策性粮油 库存检查工作的通知"
    assert sc.date_check(date(2026, 4, 27), page, title)[0] == "一致"
    assert sc.date_check(date(2026, 10, 13), page, title)[0] == "页面写的是别的日期"
    assert sc.date_check(date(2026, 11, 3), "信息发布日期 : 2026-11-03")[0] == "一致"


def test_more_page_shapes_from_manual_review() -> None:
    # 标题下方紧挨着的日期、注释里的旧日期、"主题：…日期："的污染标题
    page = "<h1>关于开展政府开放日活动的通知</h1> 2026-09-24 来源：市发改委"
    assert sc.date_check(date(2026, 9, 24), page, "关于开展政府开放日活动的通知")[0] == "一致"
    old = "<!--<span>发布时间：2022-11-01</span>--><span>发布时间： 2026-11-06</span>"
    assert sc.date_check(date(2026, 11, 6), old)[0] == "一致"
    polluted = "主题：2026年重点企业线上招聘会（第三十九期） 日期：2026-11-09"
    assert (
        sc.title_check(polluted, "<h1>2026年重点企业线上招聘会（第三十九期）</h1>")[0] == "不一致"
    )


def test_date_inside_title_does_not_count_and_publication_beats_writing_date() -> None:
    title = "关于召开2026年5月1日座谈会的通知"
    page = f"<h1>{title}</h1> 发布日期：2026-04-20"
    assert sc.date_check(date(2026, 5, 1), page, title)[0] == "页面写的是别的日期"
    assert sc.date_check(date(2026, 4, 20), page, title)[0] == "一致"
    both = "成文日期：2026-04-10 发布日期：2026-04-20"
    assert sc.date_check(date(2026, 4, 10), both)[0] == "页面写的是别的日期"
    only_written = "成文日期：2026-04-10"
    assert sc.date_check(date(2026, 4, 10), only_written)[0] == "一致"


def test_seed_in_file_name(tmp_path) -> None:
    row = {f: "" for f in sc.FIELDS}
    row.update(序号=1, 程序结论=sc.OK)
    a, _ = sc.write_outputs(tmp_path, "江苏", "市级", 1, sc.DEFAULT_SEED, [row], date(2026, 11, 9))
    b, _ = sc.write_outputs(tmp_path, "江苏", "市级", 1, "2026-11-b", [row], date(2026, 11, 9))
    assert a.name == "联网核对_市级.csv" and b.name == "联网核对_市级_2026-11-b.csv"


def test_tail_date_after_other_labels_and_market_bureau_prefix() -> None:
    title = "关于开展专项资金申报的通知"
    page = f"<h1>{title}</h1> 申报截止时间 2026-11-20 正文……"
    assert sc.date_check(date(2026, 11, 20), page, title)[0] != "一致"
    cell = {"城市": "南京", "部门": "市场监管", "栏目网址": "https://amr.nanjing.gov.cn/tzgg/"}
    doc = "<title>市场监管总局关于开展专项检查的通知</title>"
    assert sc.attribution_check(cell, "<title>通知公告</title>", doc, "nanjing.gov.cn")[0] == "本市"


def test_tail_date_needs_the_whole_title() -> None:
    """列表里截断的标题只对上前 10 个字时，不看标题下方没写字样的日期。"""
    page = "<h1>关于做好2026年度专项资金项目申报工作和2026-11-20前报送材料的通知</h1>"
    stored_title = "关于做好2026年度专项资金项目申报工作的补充通知..."
    assert sc.date_check(date(2026, 11, 20), page, stored_title)[0] != "一致"


def test_dates_followed_by_before_or_from_and_enumerated_time_labels() -> None:
    title = "关于报送年度统计数据的通知"
    page = f"<h1>{title}</h1> 请于2026年10月31日前报送 发布日期：2026-10-20"
    assert sc.date_check(date(2026, 10, 31), page, title)[0] != "一致"
    page2 = f"<h1>{title}</h1> 一、时间：2026年11月20日 二、地点"
    assert sc.date_check(date(2026, 11, 20), page2, title)[0] != "一致"
