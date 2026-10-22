from datetime import date

import pytest

from tests.conftest import read_fixture_html
from youju.crawler.detail import (
    MAX_ATTACHMENTS,
    _split_issuer_line,
    parse_detail,
    redact_sensitive,
)


@pytest.mark.parametrize(
    ("fixture", "url", "opening", "issuer", "number", "attachments", "published"),
    [
        (
            "detail_liaoning_344",
            "https://gxt.ln.gov.cn/gxt/tztg/2026092416504833875/index.shtml",
            "辽工信投资〔2026〕130号\n各市工业和信息化局",
            "辽宁省工业和信息化厅",
            "辽工信投资〔2026〕130号",
            2,
            date(2026, 9, 24),
        ),
        (
            "detail_liaoning_343",
            "https://gxt.ln.gov.cn/gxt/tztg/2026091717062662168/index.shtml",
            "各市（不含大连）工业和信息化局",
            "辽宁省工业和信息化厅、辽宁省财政厅、国家税务总局辽宁省税务局",
            None,
            1,
            date(2026, 9, 17),
        ),
        (
            "detail_jiangsu_510",
            "https://kxjst.jiangsu.gov.cn/art/2026/9/23/art_82540_11833785.html",
            "",  # 原站只有“点击正文.pdf”，HTML 无正文。
            None,
            None,
            1,
            date(2026, 9, 23),
        ),
        (
            "detail_fujian_915",
            "https://gxt.fujian.gov.cn/zwgk/gsgg/202609/t20260924_7218179.htm",
            "各设区市（不含厦门）工信局",
            "福建省工业和信息化厅、福建省教育厅、福建省文化和旅游厅、福建省广播电视局、福建省体育局、福建省文物局",
            None,
            2,
            date(2026, 9, 24),
        ),
        (
            "detail_guangdong_1072",
            "https://gdii.gd.gov.cn/zwgk/tzgg1011/content/post_4960472.html",
            "各地级以上市人民政府",
            "广东省工业和信息化厅",
            "粤工信材料函〔2026〕53号",
            0,
            date(2026, 9, 24),
        ),
        (
            "detail_most_1493",
            "https://www.most.gov.cn/tztg/202609/t20260908_197283.html",
            "国际大科学计划—平方公里阵列射电望远镜",
            "科学技术部国际科技合作中心",
            None,
            1,
            date(2026, 9, 8),
        ),
    ],
)
def test_real_detail_pages(fixture, url, opening, issuer, number, attachments, published) -> None:
    result = parse_detail(read_fixture_html(fixture), url)
    assert result.failure_reason is None
    assert result.body_text.startswith(opening) if opening else result.body_text == ""
    assert result.issuer == issuer
    assert result.doc_number == number
    assert result.publish_date == published
    assert len(result.attachments) == attachments
    assert all(a.url.startswith("http") and a.extension for a in result.attachments)
    assert "网站首页" not in result.body_text
    assert "相关新闻" not in result.body_text


def test_attachment_names_and_urls_are_absolute() -> None:
    result = parse_detail(
        read_fixture_html("detail_fujian_915"),
        "https://gxt.fujian.gov.cn/zwgk/gsgg/202609/t20260924_7218179.htm",
    )
    assert [a.extension for a in result.attachments] == ["doc", "doc"]
    assert result.attachments[0].name.startswith("1.视听系统典型案例申报书")
    assert result.attachments[0].url.startswith("https://gxt.fujian.gov.cn/")


def test_attachments_are_capped() -> None:
    links = "".join(f"<p><a href='/files/{i}.xlsx'>附件{i}</a></p>" for i in range(80))
    sample = (
        "<html><body><div id='zoom'><p>关于公布有关名单的通知，全文见附件。</p>"
        f"{links}</div></body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/a.html")
    assert len(result.attachments) == MAX_ATTACHMENTS
    assert result.attachments[0].url == "https://example.gov.cn/files/0.xlsx"


def test_doc_number_variants_and_sensitive_data() -> None:
    for number in ("辽工信〔2026〕130号", "辽工信[2026]130号", "辽工信（2026）130号"):
        sample = (
            f"<html><meta name='PubDate' content='2026-09-24'><body><div id='zoom'>"
            f"<p>{number}</p><p>这是公开通知的正文，联系方式：13812345678，"
            "身份证号：110101199001011234，银行账号：6222021234567890123。</p>"
            "<p>辽宁省工业和信息化厅</p><p>2026年9月24日</p></div></body></html>"
        )
        result = parse_detail(sample, "https://example.gov.cn/a.html")
        assert result.doc_number == number
        assert "13812345678" not in result.body_text
        assert "110101199001011234" not in result.body_text
        assert "6222021234567890123" not in result.body_text
    assert redact_sensitive("联系人：张三，邮箱：zhang@example.cn") == (
        "联系人：[姓名已脱敏]，邮箱：[邮箱已脱敏]"
    )
    assert "130503670401001" not in redact_sensitive("旧身份证号：130503670401001")
    assert "6222 0212 3456 7890 123" not in redact_sensitive("银行账号：6222 0212 3456 7890 123")


# 10/20 附件读入时发现：旧规则把邮箱、座机前面连着的中文一起遮掉（截止日期、单位名都没了），
# "联系人："后面一律遮前 2–8 个汉字（遮掉单位名，姓名反倒留着）。下面每条都是原网页里见过的写法
# （名字、号码换成了假的）。
@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        # 邮箱：只遮地址本身
        (
            "加盖公章，PDF电子版于7月2日前发送至abc_x@163.com邮箱。",
            "加盖公章，PDF电子版于7月2日前发送至[邮箱已脱敏]邮箱。",
        ),
        (
            "请参训人员于9月4日（周五）17:00前将参会回执电子版发到指定邮箱abc@example.gov.cn，组织方",
            "请参训人员于9月4日（周五）17:00前将参会回执电子版发到指定邮箱[邮箱已脱敏]，组织方",
        ),
        (
            "政务邮箱user000（OA）或user.name+tag@mail.example.com.cn。",
            "政务邮箱user000（OA）或[邮箱已脱敏]。",
        ),
        # 座机前面不是自成一段的姓名：不遮（以前"日前寄至""联系电话""传真"都被当成姓名）
        ("材料请于11月20日前寄至0571-87654321", "材料请于11月20日前寄至[电话已脱敏]"),
        ("联系电话0571-87654321，传真0571-87654322", "联系电话[电话已脱敏]，传真[电话已脱敏]"),
        ("应届毕业生，38周岁及以下\n022-12345678", "应届毕业生，38周岁及以下\n[电话已脱敏]"),
        # 自成一段、以常见姓氏开头的是姓名，前面的单位名留着
        (
            "省工信厅中小企业处 张三 0351-1234567",
            "省工信厅中小企业处 [姓名已脱敏] [电话已脱敏]",
        ),
        (
            "运城市工信局中小企业科 李 四 0359-1234567",
            "运城市工信局中小企业科 [姓名已脱敏] [电话已脱敏]",
        ),
        ("张老师 0571-87654321", "[姓名已脱敏] [电话已脱敏]"),
        ("张三0571-87654321", "[姓名已脱敏][电话已脱敏]"),
        ("市科技局基础处\n王小五\n022-12345678", "市科技局基础处\n[姓名已脱敏]\n[电话已脱敏]"),
        # 同一行几个姓名合成一个
        (
            "咨询电话：省科技厅 郑十 吴九 025-12345678",
            "咨询电话：省科技厅 [姓名已脱敏] [电话已脱敏]",
        ),
        ("赵六六 孙 七 0951-1234567", "[姓名已脱敏] [电话已脱敏]"),
        ("省科技厅 张三、李四 025-12345678", "省科技厅 [姓名已脱敏] [电话已脱敏]"),
        ("联系人：张三、李四", "联系人：[姓名已脱敏]"),
        # 单位名后面紧跟着姓名
        ("省财政厅周八八 0531-12345678", "省财政厅[姓名已脱敏] [电话已脱敏]"),
        # 单位名、地名、标签不当姓名
        ("高新处 0571-87654321", "高新处 [电话已脱敏]"),
        ("济南 0531-12345678 青岛 0532-12345678", "济南 [电话已脱敏] 青岛 [电话已脱敏]"),
        ("西安市 029-12345678", "西安市 [电话已脱敏]"),
        ("石家庄 0311-87654321 武汉 027-87654321", "石家庄 [电话已脱敏] 武汉 [电话已脱敏]"),
        ("技术支持 0571-87654321", "技术支持 [电话已脱敏]"),
        ("申报系统 0571-87654321", "申报系统 [电话已脱敏]"),
        ("联 系 方 式 0571-87654321", "联 系 方 式 [电话已脱敏]"),
        # 和前面的数字、字母连着的不是自成一段
        ("A座1210高新 0571-87654321", "A座1210高新 [电话已脱敏]"),
        ("A座1210王五 0571-87654321", "A座1210王五 [电话已脱敏]"),
        # 已经遮过的座机前面也一样看（正文给出前会再过一遍）
        ("张三 [电话已脱敏]", "[姓名已脱敏] [电话已脱敏]"),
        ("省工信厅 张三 [电话已脱敏]", "省工信厅 [姓名已脱敏] [电话已脱敏]"),
        # 邮箱、座机先遮，姓名后认：姓名前后挨着号码、邮箱的，第一遍就遮（再过一遍不变）
        ("邮箱：abc@163.com张三 0571-87654321", "邮箱：[邮箱已脱敏][姓名已脱敏] [电话已脱敏]"),
        (
            "联系人：张三0571-87654321李四0571-87654322",
            "联系人：[姓名已脱敏][电话已脱敏][姓名已脱敏][电话已脱敏]",
        ),
        ("abc@163.com-def@qq.com", "[邮箱已脱敏][邮箱已脱敏]"),
        ("abc@163.com_def@qq.com", "[邮箱已脱敏][邮箱已脱敏]"),
        # 没写冒号、紧贴着姓名的"联系人张三"
        ("联系人张三 0571-87654321", "联系人[姓名已脱敏] [电话已脱敏]"),
        # 普通词、地址、自治州不当姓名
        ("值班电话：白天 0571-87654321", "值班电话：白天 [电话已脱敏]"),
        ("地址：杭州市西湖区文三路 0571-87654321", "地址：杭州市西湖区文三路 [电话已脱敏]"),
        ("甘孜 0836-1234567", "甘孜 [电话已脱敏]"),
        ("纪检 0571-87654321；商务 0571-87654322", "纪检 [电话已脱敏]；商务 [电话已脱敏]"),
        ("董事长 张三 0571-87654321", "董事长 [姓名已脱敏] [电话已脱敏]"),
        # "联系人："后面：跳过单位名，遮姓名
        ("联系人：张三，邮箱：zhang@example.cn", "联系人：[姓名已脱敏]，邮箱：[邮箱已脱敏]"),
        (
            "（联系人：市工信局汽车工业处 张三；联系电话：12345678）",
            "（联系人：市工信局汽车工业处 [姓名已脱敏]；联系电话：12345678）",
        ),
        ("联系人：省科技厅对外合作处 钱一一 孙二", "联系人：省科技厅对外合作处 [姓名已脱敏]"),
        ("联系人：王 五 李小四 赵六", "联系人：[姓名已脱敏]"),
        ("联 系 人：杜三", "联 系 人：[姓名已脱敏]"),
        # "联系人："后面两个字、以常见姓氏开头的，以单位字结尾也是姓名；别处不是
        ("联系人：王镇", "联系人：[姓名已脱敏]"),
        ("联系人：省科技厅高新处 张路", "联系人：省科技厅高新处 [姓名已脱敏]"),
        ("沙县 0598-1234567", "沙县 [电话已脱敏]"),
        # 不是这一行最后一个词的、以"州"结尾的地级市：照样不当姓名
        ("联系人：省科技厅 高校 张三", "联系人：省科技厅 高校 [姓名已脱敏]"),
        (
            "联系人：浙江省 杭州 张三 0571-87654321",
            "联系人：浙江省 杭州 [姓名已脱敏] [电话已脱敏]",
        ),
        (
            "各市联系人：杭州 张三 0571-87654321；苏州 李四 0512-87654321",
            "各市联系人：杭州 [姓名已脱敏] [电话已脱敏]；苏州 [姓名已脱敏] [电话已脱敏]",
        ),
        ("联系人：杭州", "联系人：杭州"),
        # 几个联系人里最后那个两字的也算；冒号后换行的不用这条例外（表格下一格可能是表头、单位名）
        ("联系人：张三 王镇", "联系人：[姓名已脱敏]"),
        ("联系人：\n王镇", "联系人：\n王镇"),
        ("联系人：\n张三 王镇", "联系人：\n[姓名已脱敏] 王镇"),
        ("联系人：张三、王镇", "联系人：[姓名已脱敏]"),
        # 冒号后换行的，几个姓名也一起遮
        ("联系人：\n李四、王五", "联系人：\n[姓名已脱敏]"),
        ("联系人及电话：王五 0571-87654321", "联系人及电话：[姓名已脱敏] [电话已脱敏]"),
        ("联系人及联系电话：王五 0571-87654321", "联系人及联系电话：[姓名已脱敏] [电话已脱敏]"),
        ("联系人及联系方式：王五，0571-87654321", "联系人及联系方式：[姓名已脱敏]，[电话已脱敏]"),
        ("联系人和联系方式：王五", "联系人和联系方式：[姓名已脱敏]"),
        ("联系人电话：王五", "联系人电话：[姓名已脱敏]"),
        # 复姓
        ("联系人：欧阳小明", "联系人：[姓名已脱敏]"),
        ("欧阳小明 0571-87654321", "[姓名已脱敏] [电话已脱敏]"),
        ("省科技厅司马小明 0571-87654321", "省科技厅[姓名已脱敏] [电话已脱敏]"),
        ("联系人：刘三三 12345678", "联系人：[姓名已脱敏] 12345678"),
        ("联系人：张三028—12345678", "联系人：[姓名已脱敏]028—12345678"),
        ("联系人：阿依·买买", "联系人：[姓名已脱敏]"),
        (
            "联系人：某某市科学技术信息研究所办公室张三",
            "联系人：某某市科学技术信息研究所办公室[姓名已脱敏]",
        ),
        # 姓名后面跟着的不是姓名：不遮（以前"请于"也会被遮）
        ("联系人：张三 请于11月20日前报送", "联系人：[姓名已脱敏] 请于11月20日前报送"),
        ("联系人：Ryan LEE 先生", "联系人：Ryan LEE 先生"),
        # 只有单位名、表头：不遮（以前遮掉前 8 个字）
        ("联系人：省科技厅高新处", "联系人：省科技厅高新处"),
        ("联系人：推荐单位", "联系人：推荐单位"),
        # 到数字为止：后面的截止日期不当姓名
        ("联系人：各单位于10月20日前确定", "联系人：各单位于10月20日前确定"),
        ("联系人及电话：\n推荐单位", "联系人及电话：\n推荐单位"),
        ("联系人及电话：\n是否已 上年 上年", "联系人及电话：\n是否已 上年 上年"),
        ("联系人：\n李四四，13812345678", "联系人：\n[姓名已脱敏]，[手机号已脱敏]"),
        # 银行账号：标签后面跟"为""号码"、隔着开户行的也遮；对公账号可能超过 19 位
        ("收款账号为1202021309001234568", "收款账号为[银行账号已脱敏]"),
        ("账户号码：1202021309001234568", "账户号码：[银行账号已脱敏]"),
        (
            "开户行及账号：中国工商银行股份有限公司杭州西湖支行 1202021309001234568",
            "开户行及账号：中国工商银行股份有限公司杭州西湖支行 [银行账号已脱敏]",
        ),
        (
            "开户行：中国工商银行杭州分行 1202021309001234568",
            "开户行：中国工商银行杭州分行 [银行账号已脱敏]",
        ),
        ("账号（基本户）：1202021309001234568", "账号（基本户）：[银行账号已脱敏]"),
        ("开户账号：33050161613500000123", "开户账号：[银行账号已脱敏]"),
        ("银行账号：6222 0212 3456 7890 123", "银行账号：[银行账号已脱敏]"),
        # 后面的年份、金额、日期不算进账号（以前"2025"被一起遮掉）
        ("账号：1234567890 2025年11月5日前", "账号：[银行账号已脱敏] 2025年11月5日前"),
        ("账号：1202021309001234568 1500元", "账号：[银行账号已脱敏] 1500元"),
        ("账号：1202021309001234568 1500", "账号：[银行账号已脱敏] 1500"),
        ("账号：1202021309 2025 10 10", "账号：[银行账号已脱敏] 2025 10 10"),
        ("账号：1202021309 20251010", "账号：[银行账号已脱敏] 20251010"),
        # 写了"账号"的第一组不看后面跟的字（"日期""次日""分行""号码""时间"开头的词不是单位）
        ("账号：1234567890   元", "账号：[银行账号已脱敏]   元"),
        (
            "账号：1202021309001234568 日期：2025年10月10日",
            "账号：[银行账号已脱敏] 日期：2025年10月10日",
        ),
        ("汇款账号：1202021309001234568 次日到账", "汇款账号：[银行账号已脱敏] 次日到账"),
        ("开户账号：1202021309001234568 分行：杭州", "开户账号：[银行账号已脱敏] 分行：杭州"),
        ("账号1202021309001234568时间2025年", "账号[银行账号已脱敏]时间2025年"),
        ("银行卡号：6222 0212 3456 7890 123 号码", "银行卡号：[银行账号已脱敏] 号码"),
        ("账号：488418-246237-7124元", "账号：[银行账号已脱敏]元"),
        ("账号：6222 0212 3456 7890 1234时间2025年", "账号：[银行账号已脱敏]时间2025年"),
        ("账号：1234567890 10月20日前", "账号：[银行账号已脱敏] 10月20日前"),
        ("账号：1234567890 20日前", "账号：[银行账号已脱敏] 20日前"),
        ("账号：1234567890 20日，", "账号：[银行账号已脱敏] 20日，"),
        ("账号：1234567890 5日起", "账号：[银行账号已脱敏] 5日起"),
        ("账号：1234567890 10日内", "账号：[银行账号已脱敏] 10日内"),
        # 年份隔着空格跟"年"、点号斜杠写的日期、超过 19 位才接上的金额：不并进账号
        (
            "账号：1202021309001234 2025 年 11 月 20 日前",
            "账号：[银行账号已脱敏] 2025 年 11 月 20 日前",
        ),
        ("账号：1202021309001234 1500元", "账号：[银行账号已脱敏] 1500元"),
        ("账号：1202021309001234 2025.10.10前", "账号：[银行账号已脱敏] 2025.10.10前"),
        ("账号：1202021309001234 2025/10/10前", "账号：[银行账号已脱敏] 2025/10/10前"),
        ("账号：1202021309001234 2025-10前", "账号：[银行账号已脱敏] 2025-10前"),
        ("账号：6222 0212 3456 7890 2025-1截止", "账号：[银行账号已脱敏] 2025-1截止"),
        ("账号：1202021309001234 2025-10", "账号：[银行账号已脱敏] 2025-10"),
        ("账号：1202021309001234 1999-12前", "账号：[银行账号已脱敏] 1999-12前"),
        ("账号：1202-0213-0900-1234 2025-10前", "账号：[银行账号已脱敏] 2025-10前"),
        # 整串都用横线分组的、"月份"不在 1–12 的照样接
        ("账号：1202-0213-0900-2025-10", "账号：[银行账号已脱敏]"),
        ("账号：1202 0213 0900 2025-13", "账号：[银行账号已脱敏]"),
        # 第一组 12 位以上的，和旧规则一样最多接到 19 位：后面的年份、金额、日期不并进来
        ("账号：1202021309001234 2025-13", "账号：[银行账号已脱敏] 2025-13"),
        ("账号：6222021234567890 2025 10月10日", "账号：[银行账号已脱敏] 2025 10月10日"),
        ("账号：622202123456789012 300.00元", "账号：[银行账号已脱敏] 300.00元"),
        ("账号：622202123456789012 20,000元", "账号：[银行账号已脱敏] 20,000元"),
        ("账号：622202123456789012 20日之前", "账号：[银行账号已脱敏] 20日之前"),
        ("账号：622202123456789012 25年3月", "账号：[银行账号已脱敏] 25年3月"),
        # 分组写的账号超过 19 位也照样断开：年份隔空格跟月份、带千分位小数的金额、两位数的年份
        ("账号：6222 0212 3456 7890 2025 10月10日", "账号：[银行账号已脱敏] 2025 10月10日"),
        ("账号：6222 0212 3456 7890 123 20,000元", "账号：[银行账号已脱敏] 20,000元"),
        ("账号：6222 0212 3456 7890 123 300.00 元", "账号：[银行账号已脱敏] 300.00 元"),
        ("账号：6222 0212 3456 7890 25年", "账号：[银行账号已脱敏] 25年"),
        ("账号：6222 0212 3456 7890 2025.10.10前", "账号：[银行账号已脱敏] 2025.10.10前"),
        ("账号：6222 0212 3456 7890 2025      年", "账号：[银行账号已脱敏] 2025      年"),
        # 超过 19 位的只在四位一组、号码后面没有接着数字写法时才接；别的写法和旧规则一样最多 19 位
        ("账号：6222 0212 3456 7890 2025 10.10前", "账号：[银行账号已脱敏] 2025 10.10前"),
        ("账号：6222 0212 3456 789012 25.10.10前", "账号：[银行账号已脱敏] 25.10.10前"),
        ("账号：6222 0212 3456 789012 10/31前", "账号：[银行账号已脱敏] 10/31前"),
        ("账号：6222 0212 3456 789012 20 000元", "账号：[银行账号已脱敏] 20 000元"),
        ("账号：6222 0212 3456 789012 20，000元", "账号：[银行账号已脱敏] 20，000元"),
        (
            "账号：6222 0212 3456 7890 2025\u300010月10日",
            "账号：[银行账号已脱敏] 2025\u300010月10日",
        ),
        (
            "账号：6222 0212 3456 7890 2025\uff0d10\uff0d10前",
            "账号：[银行账号已脱敏] 2025\uff0d10\uff0d10前",
        ),
        ("账号：3305 0161 6135 0000 0123。", "账号：[银行账号已脱敏]。"),
        ("账号：1202 0213 0900 1234 2026 年度", "账号：[银行账号已脱敏] 2026 年度"),
        ("账号：1234567890 10 月 20 日前", "账号：[银行账号已脱敏] 10 月 20 日前"),
        ("账号：1234567890 10月底前", "账号：[银行账号已脱敏] 10月底前"),
        ("账号：1234567890 10月下旬", "账号：[银行账号已脱敏] 10月下旬"),
        ("账号：1234567890 1月上旬", "账号：[银行账号已脱敏] 1月上旬"),
        ("账号：1234567890 1月内", "账号：[银行账号已脱敏] 1月内"),
        ("账号：1234567890 10月后", "账号：[银行账号已脱敏] 10月后"),
        ("账号：1234567890 10月之前", "账号：[银行账号已脱敏] 10月之前"),
        ("账号：1234567890 20日之前", "账号：[银行账号已脱敏] 20日之前"),
        ("账号：1202021309001234 2025      年", "账号：[银行账号已脱敏] 2025      年"),
        ("账号：1234567890 2025\n年11月", "账号：[银行账号已脱敏] 2025\n年11月"),
        ("账号：1234567890 1 月", "账号：[银行账号已脱敏] 1 月"),
        ("账号：1234567890 12月，", "账号：[银行账号已脱敏] 12月，"),
        ("账号：1234567890 1 月 申报", "账号：[银行账号已脱敏] 1 月 申报"),
        ("账号：1234567890 1 月\n截止申报", "账号：[银行账号已脱敏] 1 月\n截止申报"),
        ("账号：1234567890 1月起", "账号：[银行账号已脱敏] 1月起"),
        ("账号：1234567890 1月至3月", "账号：[银行账号已脱敏] 1月至3月"),
        # 一两位数后面的"月均""月份""日期""日均"是词、不是日期；不在 1–12、1–31 的不是月、日：并进账号
        ("账户：3851 2761 6565 09 月均余额", "账户：[银行账号已脱敏] 月均余额"),
        ("账户：3851 2761 6565 09 日均余额", "账户：[银行账号已脱敏] 日均余额"),
        # "年费""年均""年检""年报"也是词：前面的一两位数、四位数照样并进账号
        ("卡号：6222 0212 3456 7890 12 年费", "卡号：[银行账号已脱敏] 年费"),
        ("账户：3851 2761 6565 09 年均余额", "账户：[银行账号已脱敏] 年均余额"),
        ("开户账号：2181-0597-8465-7112-55 年检", "开户账号：[银行账号已脱敏] 年检"),
        ("卡号4904-4502-0113-2075 年报", "卡号[银行账号已脱敏] 年报"),
        ("账号：1234567890 25年底前", "账号：[银行账号已脱敏] 25年底前"),
        ("账号：6222 0212 3456 7890 2025年第一批", "账号：[银行账号已脱敏] 2025年第一批"),
        ("卡号：6222 0212 3456 7890 12月份", "卡号：[银行账号已脱敏]月份"),
        (
            "账号：6222 0212 3456 7890 98日期：2025年10月10日",
            "账号：[银行账号已脱敏]日期：2025年10月10日",
        ),
        ("账号：6222 0212 3456 7890 98日前", "账号：[银行账号已脱敏]日前"),
        ("卡号：6222 0212 3456 7890 13月底", "卡号：[银行账号已脱敏]月底"),
        ("账号：3305 0161 6135 0000 0123", "账号：[银行账号已脱敏]"),
        # 新规则一位都没遮、旧规则会遮的，照旧规则遮
        ("账号：3301 20251010 1234", "账号：[银行账号已脱敏]"),
        ("账号：1202-0213-0900-1234-568", "账号：[银行账号已脱敏]"),
        (
            "开户银行：中国工商银行杭州分行 1202021309001234568",
            "开户银行：中国工商银行杭州分行 [银行账号已脱敏]",
        ),
        ("零余额账户 2026 年度预算", "零余额账户 2026 年度预算"),
        ("账号：12345", "账号：12345"),
        # 手机号后面跟着一两位小数的是金额
        ("项目产值 13812345678.50 元", "项目产值 13812345678.50 元"),
        ("电话 13812345678。", "电话 [手机号已脱敏]。"),
        ("手机：13812345678.13900000000", "手机：[手机号已脱敏].[手机号已脱敏]"),
    ],
)
def test_redaction_masks_only_the_numbers_and_names(raw: str, masked: str) -> None:
    assert redact_sensitive(raw) == masked
    # 正文给出前会再过一遍：遮过的再过一遍不变
    assert redact_sensitive(masked) == masked


@pytest.mark.parametrize(
    "stored",
    [
        # 库里按旧规则遮过的正文：再过一遍新规则不变（不会再多吞字）
        "加盖公章，[邮箱已脱敏]邮箱。",
        "请参训人员于9月4日（周五）17:[邮箱已脱敏]，组织方",
        "材料请于11月20[姓名已脱敏][电话已脱敏]",
        "省工信厅中小企[姓名已脱敏][电话已脱敏]",
        "联系人：[姓名已脱敏]处 [电话已脱敏]；",
        "（联系人：[姓名已脱敏]业处 张三；",
        # 新认的标签后面是旧规则吞剩的半截单位名：不当姓名
        "联系人及电话：中小企[姓名已脱敏][电话已脱敏]",
        "联系人及联系方式：科技厅高[姓名已脱敏][电话已脱敏]",
        # 第一遍遮的时候后面还有姓名的：前面的"高校""党委"不当这一行最后一个词
        "联系人：省科技厅 高校 [姓名已脱敏]",
        "联系人：党委_[姓名已脱敏]",
    ],
)
def test_old_redacted_bodies_stay_the_same(stored: str) -> None:
    assert redact_sensitive(stored) == stored


def test_protection_page_is_rejected() -> None:
    result = parse_detail(
        "<html><title>安全验证</title><body><div id='zoom'>请输入验证码</div></body></html>",
        "https://example.gov.cn/a.html",
    )
    assert result.failure_reason == "防护页或验证码"
    assert not result.body_text


def test_density_fallback_ignores_navigation_and_redacts() -> None:
    sample = (
        "<html><body><div class='top-links'><a href='/a'>网站首页栏目导航公告通知</a>"
        "<a href='/b'>政策服务信息公开相关链接</a></div>"
        "<div class='story'><p>各单位应按规定报送申报材料，主管部门将逐项核对材料。</p>"
        "<p>申报截止2026年10月30日，联系人电话13812345678。</p></div>"
        "<div class='related'>相关新闻推荐报道</div></body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/a.html")
    assert result.failure_reason is None
    assert result.body_text.startswith("各单位应按规定")
    assert "网站首页" not in result.body_text
    assert "13812345678" not in result.body_text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("国家税务总局 辽宁省税务局", "国家税务总局辽宁省税务局"),
        ("国家金融监督管理总局 广东监管局", "国家金融监督管理总局广东监管局"),
        ("中国人民银行 江苏省分行", "中国人民银行江苏省分行"),
        ("国家外汇管理局 上海市分局", "国家外汇管理局上海市分局"),
        ("海关总署 沈阳海关", "海关总署沈阳海关"),
        ("辽宁省财政厅 国家税务总局 辽宁省税务局", "辽宁省财政厅、国家税务总局辽宁省税务局"),
        ("安 徽 省 财 政 厅", "安徽省财政厅"),
        (
            "福建省工程技术经济专业职称改革领导小组 福建省职称改革工作办公室",
            "福建省工程技术经济专业职称改革领导小组、福建省职称改革工作办公室",
        ),
    ],
)
def test_issuer_branch_names_are_not_split(line: str, expected: str) -> None:
    assert _split_issuer_line(line) == expected
    sample = (
        "<html><body><div id='zoom'><p>这是公开通知正文，需要保存这些段落内容。"
        "申报单位应当按照规定提交材料。</p>"
        f"<p>{line}</p><p>2026年9月24日</p></div></body></html>"
    )
    assert parse_detail(sample, "https://example.gov.cn/a.html").issuer == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("安徽省工业和信息化厅、安 徽 省 财 政 厅", "安徽省工业和信息化厅、安徽省财政厅"),
        (
            "福建省工程技术经济专业职称改革领导小组 福建省职称改革工作办公室",
            "福建省工程技术经济专业职称改革领导小组、福建省职称改革工作办公室",
        ),
        ("国家税务总局 安徽省税务局", "国家税务总局安徽省税务局"),
        # 名称中间的排版空格不当成两个机关
        ("上海市 科学技术委员会", "上海市 科学技术委员会"),
    ],
)
def test_normalize_issuer_display(value: str, expected: str) -> None:
    from youju.issuer import normalize_issuer

    assert normalize_issuer(value) == expected


def test_attachment_links_written_in_page_scripts() -> None:
    """TRS 模板把附件链接写在脚本里（var hasFJ='<a href=...>'），页面结构里没有（10/15 查名单读不出企业名的原因）。"""
    result = parse_detail(
        read_fixture_html("detail_chongqing_kjj_script_attachments"),
        "https://kjj.cq.gov.cn/zwxx_176/tzgg/202609/t20260904_16033963.html",
    )
    assert [(a.name, a.url, a.extension) for a in result.attachments] == [
        (
            "2026年第八批入库科技型企业名单.pdf",
            "https://kjj.cq.gov.cn/zwxx_176/tzgg/202609/P020260904620028456189.pdf",
            "pdf",
        )
    ]
    result = parse_detail(
        read_fixture_html("detail_ningxia_gxt_script_attachments"),
        "https://gxt.nx.gov.cn/zwgk/fdzdgknr/glfw/gsgg/202609/t20260923_5354445.html",
    )
    assert [a.name for a in result.attachments] == [
        "附件1.2026年度认定的自治区专精特新中小企业名单.xlsx",
        "附件2.复核通过的自治区专精特新中小企业名单（第二批）.xlsx",
        "附件3.取消认定的自治区专精特新中小企业名单（第二批）.xlsx",
    ]
    assert all(
        a.extension == "xlsx" and a.url.startswith("https://gxt.nx.gov.cn/")
        for a in result.attachments
    )


def test_script_links_follow_the_same_rules() -> None:
    """脚本里的链接按同样的规则收：要是文件、外站的要标明附件、同一地址只收一次；外部脚本不看。"""
    sample = (
        "<html><head><script src='https://example.gov.cn/a.js'></script></head><body>"
        "<div id='zoom'><p>关于公布有关名单的通知，全文见附件。</p>"
        "<p><a href='/files/list.xlsx'>附件1.名单.xlsx</a></p></div>"
        '<script>var hasFJ=\'<a href="/files/list.xlsx">附件1.名单.xlsx</a><BR/>'
        '<a href="./more.pdf">附件2.补充名单.pdf</a><BR/>'
        '<a href="https://other.example.com/ref.pdf">引用的文件.pdf</a>\';'
        "var stat='<a href=\"https://tongji.example.com/track\">统计</a>';"
        "var other='<a href=\"https://other.example.com/notice-att.docx\">附件3.通知.docx</a>';</script>"
        "</body></html>"
    )
    result = parse_detail(sample, "https://example.gov.cn/zw/a.html")
    assert [(a.name, a.url) for a in result.attachments] == [
        ("附件1.名单.xlsx", "https://example.gov.cn/files/list.xlsx"),
        ("附件2.补充名单.pdf", "https://example.gov.cn/zw/more.pdf"),
        ("附件3.通知.docx", "https://other.example.com/notice-att.docx"),
    ]


def test_script_attachment_escaped_slashes_and_markup():
    sample = r"""<div id="zoom">这是一份公开的名单通知，申报单位请按规定准备材料。
    <script>var a = '<a href=".\/files\/list.xlsx"><b>附件名单.xlsx</b><\/a>';</script></div>"""
    result = parse_detail(sample, "https://example.gov.cn/notice/index.html")
    assert len(result.attachments) == 1
    assert result.attachments[0].url == "https://example.gov.cn/notice/files/list.xlsx"
    assert result.attachments[0].name == "附件名单.xlsx"


def test_attachments_on_provincial_file_server() -> None:
    """附件放在本省政府网站群统一的文件服务器上（河南科技厅 kjt.henan.gov.cn → xcoss.henan.gov.cn），
    链接文字里没写"附件"（10/16：河南 2026 年第一批 11903 家入库科技型中小企业名单原来没认出来）。
    只认明确列出的文件服务器：同省别的子域、别的政府网站、非政府网站，没写"附件"的照旧不收。"""
    page = (
        "<html><body><div id='zoom'><p>现将通过公示的河南省2026年第一批入库企业名单（详见附件）予以公告。</p>"
        "<p>附件：<a href='https://xcoss.henan.gov.cn/typtfile/20260902/6f6b.pdf'>"
        "河南省2026年第一批入库科技型中小企业名单</a></p>"
        "<p>参考：<a href='https://www.henan.gov.cn/zwgk/policy.pdf'>省政府有关文件</a></p>"
        "<p>参考：<a href='https://www.most.gov.cn/xxgk/policy.pdf'>科技部有关文件</a></p>"
        "<p>另见<a href='https://other.example.com/notice.pdf'>其他网站的文件</a></p>"
        "</div></body></html>"
    )
    result = parse_detail(page, "http://kjt.henan.gov.cn/2026/09-02/3410350.html")
    assert [(a.name, a.url) for a in result.attachments] == [
        (
            "河南省2026年第一批入库科技型中小企业名单",
            "https://xcoss.henan.gov.cn/typtfile/20260902/6f6b.pdf",
        )
    ]
    # 文件服务器只对本省的页面放开：别的省的页面链过去，没写"附件"照旧不收
    other = page.replace("附件：<a", "<a")
    assert parse_detail(other, "https://kjt.hebei.gov.cn/a/1.html").attachments == []


def test_attachment_rule_counterexamples() -> None:
    """外站文件只看链接自己的文字里有没有"附件"，不看链接前面写的"附件："；
    文件服务器按主机名精确匹配，页面主机名要以"."加省份后缀结尾。"""
    page_url = "https://gxt.fujian.gov.cn/zwgk/tzgg/1.html"
    # 福建工信厅那种写法：正文"附件：1."后面链到国务院网站一份旧文件，真正的附件在页面底部
    cited = (
        "<html><body><div id='zoom'><p>根据相关文件精神，现将有关事项通知如下。</p>"
        "<p>附件：1.<a href='https://www.gov.cn/zhengce/2023/policy.pdf'>国务院有关文件</a></p>"
        "<p><a href='/attach/list.xlsx'>名单.xlsx</a></p></div></body></html>"
    )
    assert [a.url for a in parse_detail(cited, page_url).attachments] == [
        "https://gxt.fujian.gov.cn/attach/list.xlsx"
    ]
    # 链接自己的文字写了"附件"的外站文件照旧收（原有规则，没改）
    labelled = cited.replace(">国务院有关文件<", ">附件1：国务院有关文件<")
    assert "https://www.gov.cn/zhengce/2023/policy.pdf" in [
        a.url for a in parse_detail(labelled, page_url).attachments
    ]
    henan = "http://kjt.henan.gov.cn/2026/09-02/1.html"
    for href in (
        "https://xcoss.henan.gov.cn.example.com/typtfile/a.pdf",  # 仿冒主机名
        "https://xcoss.henan.gov.cn@example.com/typtfile/a.pdf",  # 用户名里写服务器名
        "https://oss.henan.gov.cn/typtfile/a.pdf",  # 同省别的子域，没列出
        "http://xcoss.henan.gov.cn:8080/typtfile/a.pdf",  # 非默认端口
    ):
        lookalike = (
            f"<html><body><div id='zoom'><p><a href='{href}'>名单</a></p></div></body></html>"
        )
        assert parse_detail(lookalike, henan).attachments == [], href
    upper = (
        "<html><body><div id='zoom'><p>"
        "<a href='https://XCOSS.HENAN.GOV.CN/typtfile/a.pdf'>名单</a></p></div></body></html>"
    )
    assert [a.url for a in parse_detail(upper, henan).attachments] == [
        "https://xcoss.henan.gov.cn/typtfile/a.pdf"
    ]
    port = upper.replace("XCOSS.HENAN.GOV.CN", "xcoss.henan.gov.cn:443")
    assert [a.url for a in parse_detail(port, henan).attachments] == [
        "https://xcoss.henan.gov.cn:443/typtfile/a.pdf"
    ]
    # 页面主机名只是以"henan.gov.cn"结尾、前面不是"."的，不算河南的网站
    assert parse_detail(upper, "https://www.zhenan.gov.cn/a/1.html").attachments == []


@pytest.mark.parametrize("label", ["账号", "联系人", "联系人：", "开户行：", "卡号\n"])
def test_long_whitespace_after_labels_does_not_hang(label: str) -> None:
    """PDF 里标签后面可能跟着一长串空格：正则不能回退到卡住（普通的 \\s*[:：]?\\s* 几万个空格就要几十秒）。"""
    import time

    started = time.perf_counter()
    for tail in ("x", "2025", "123"):
        # 不足 10 位的数字会走到"照旧规则遮"那一步，也不能卡住
        text = label + " " * 100_000 + tail
        assert redact_sensitive(text) == text
    assert time.perf_counter() - started < 2


def test_accounts_old_rule_masked_stay_masked() -> None:
    """旧规则（_BANK）会遮的账号，新规则留下的数字不能比旧规则多（红线上的号码不能比以前漏得多）。
    随机拼标签、分组、后缀。"""
    import random

    from youju.crawler.detail import _BANK
    from youju.refetch_redacted import old_redact

    rng = random.Random(20261021)
    labels = ("账号：", "银行账号 ", "卡号", "账户:", "帐号：", "银行卡号：", "开户账号：", "账号")
    tails = (
        "",
        "。",
        " 日期：2025年10月10日",
        "元",
        " 次日到账",
        " 号码",
        "时间2025年",
        " 1500",
        "，请核对",
        " 月均余额",
        " 日均余额",
        "月份",
        "日期：",
        " 年费",
        "年均余额",
        " 年检",
        "年报",
    )
    for _ in range(3000):
        size = rng.randint(10, 22)
        digits = "".join(rng.choice("0123456789") for _ in range(size))
        sep = rng.choice(("", " ", "-"))
        cut = rng.choice((4, 6, size))
        run = sep.join(digits[i : i + cut] for i in range(0, size, cut))
        text = rng.choice(labels) + run + rng.choice(tails)
        if _BANK.search(text):
            visible = sum(c.isdigit() for c in redact_sensitive(text))
            assert visible <= sum(c.isdigit() for c in old_redact(text)), text


def test_redaction_is_idempotent_on_random_mixes() -> None:
    """随机拼姓名、单位名、标签、号码、邮箱：遮过一遍的再过一遍不变。"""
    import random

    rng = random.Random(20261020)
    pieces = (
        "联系人：", "联系人及电话：", "联 系 人：", "张三", "李 四", "欧阳小明", "省工信厅中小企业处",
        "高新处", "济南", "白天", " ", "，", "、", "\n", "0571-87654321", "abc@163.com", "-", "_",
        "13812345678", "账号：1202021309001234568", "2025年10月10日前", "发送至", "[姓名已脱敏]",
        "[电话已脱敏]", "高校", "杭州", "王镇", "张路", "党委",
    )  # fmt: skip
    for _ in range(5000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(3, 15)))
        once = redact_sensitive(text)
        assert redact_sensitive(once) == once, text
