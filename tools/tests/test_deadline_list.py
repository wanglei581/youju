import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).parents[1] / "team"
sys.path.insert(0, str(TEAM))
SPEC = importlib.util.spec_from_file_location("deadline_list", TEAM / "deadline_list.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
TODAY = date(2026, 10, 19)  # 周一
COVERAGE = {
    "status": 200,
    "cities": [{"province": "广东", "city": "广州", "sources": 1}],
    "districts": [{"province": "山东", "city": "青岛", "district": "黄岛区", "sources": 1}],
    "notCovered": [{"name": "浙江", "reason": "暂未接入"}],
}


def entry(i, day, label="企业申报截止", level="省级", city=None, **extra):
    return {"id": str(i), "title": f"关于申报第{i}项的通知", "issuer": "山东省科学技术厅", "province": "山东",
            "city": city, "district": None, "level": level, "label": label, "date": day, "time": None,
            "remaining_days": None, **extra}


class TextTests(unittest.TestCase):
    def test_day_text_has_weekday_and_time(self):
        self.assertEqual(MODULE.day_text("2026-10-30", "17:00"), "10月30日（周五）17:00")
        self.assertEqual(MODULE.day_text("2026-10-19", None), "10月19日（周一）")
        # 不是今年的写年份
        self.assertEqual(MODULE.day_text("2027-01-05", None, date(2026, 12, 28)), "2027年1月5日（周二）")
        self.assertEqual(MODULE.day_text("2026-12-30", None, date(2026, 12, 28)), "12月30日（周三）")

    def test_shareable_empty_says_do_not_send(self):
        text = MODULE.shareable_text("山东", [], "范围", TODAY)
        self.assertIn("这一份是空的", text)
        self.assertIn("这次不要发", text)
        self.assertNotIn(MODULE.FOOTER, text)

    def test_shareable_has_footer_last_no_urls_and_review_note(self):
        items = [entry(1, "2026-10-23"), entry(2, "2026-10-30", label="推荐或审核截止")]
        text = MODULE.shareable_text("山东", items, "范围", TODAY)
        self.assertEqual(text.strip().splitlines()[-1], MODULE.FOOTER)
        self.assertNotIn("http", text)
        self.assertIn("1. 10月23日（周五）｜企业申报截止", text)
        self.assertIn("不是企业自己提交的截止", text)
        self.assertIn("不是全部通知", text)
        self.assertIn("人工核对", text.splitlines()[1])
        self.assertEqual(text.splitlines()[2], "范围")  # 范围紧跟标题

    def test_private_has_three_sections_links_and_do_not_forward(self):
        verified = [entry(1, "2026-10-23")]
        excerpts = [entry(2, "2026-10-30", source="excerpt", quote="请于10月30日前提交")]
        soon = [{"id": "3", "title": "关于第3项的通知", "issuer": "山东省工业和信息化厅", "kind": "企业申报截止"}]
        urls = {"1": "https://kjt.example.gov.cn/1.html", "2": "https://kjt.example.gov.cn/2.html"}
        text = MODULE.private_text("山东", verified, excerpts, soon, 1, False, urls, "范围：测试", TODAY)
        self.assertTrue(text.startswith("【只给你自己看，请不要转发】"))
        for part in ("一、已人工核对（1 条", "二、机器摘录·未经人工核对（1 条", "三、7 天内可能截止、还没人工核对（1 条"):
            self.assertIn(part, text)
        self.assertIn("原文这句：“请于10月30日前提交”", text)
        self.assertIn("原文：https://kjt.example.gov.cn/2.html", text)
        self.assertIn("网址没取到", text)  # 第 3 条没有网址
        self.assertIn("范围：测试", text)

    def test_private_counts_when_list_was_cut(self):
        text = MODULE.private_text("山东", [], [], [{"id": "3"}], 60, False, {}, "", TODAY)
        self.assertIn("60 条，这里列了前 1 条", text)
        text = MODULE.private_text("山东", [], [], [{"id": "3"}], 1, True, {}, "", TODAY)
        self.assertIn("可能还有没列出的", text)

    def test_scope_text(self):
        self.assertIn("加上已收录的青岛市黄岛区", MODULE.scope_text("山东", "", False, COVERAGE))
        self.assertIn("不含国家部委", MODULE.scope_text("山东", "", False, COVERAGE))
        self.assertIn("济南市的通知目前没有收录", MODULE.scope_text("山东", "济南", True, COVERAGE))
        self.assertIn("市、区目前都没有收录", MODULE.scope_text("河北", "", False, COVERAGE))
        self.assertIn("含国家部委", MODULE.scope_text("山东", "济南", True, COVERAGE))
        self.assertIn("加上已收录的广州市", MODULE.scope_text("广东", "", False, COVERAGE))

    def test_in_scope_city(self):
        self.assertTrue(MODULE.in_scope(entry(1, "2026-10-23"), "青岛"))
        self.assertTrue(MODULE.in_scope(entry(1, "2026-10-23", level="区县级", city="青岛"), "青岛"))
        self.assertFalse(MODULE.in_scope(entry(1, "2026-10-23", level="市级", city="济南"), "青岛"))
        self.assertTrue(MODULE.in_scope(entry(1, "2026-10-23", level="市级", city="济南"), ""))


class MainTests(unittest.TestCase):
    """main() 的筛选：用替身代替开发者工具，不连线上。"""

    def run_main(self, a_items, b_items, soon, soon_total=None, argv=("山东",), pages=None, no_url=()):
        results = {
            "cov": COVERAGE,
            "a0": {"status": 200, "items": a_items},
            "b0": {"status": 200, "items": b_items, "soon": soon,
                   "soonTotal": len(soon) if soon_total is None else soon_total},
        }
        for key, extra in (pages or {}).items():  # 后面几页：{"a0p2": [...]}，第一页写明 total
            results[key[:2]]["total"] = len(results[key[:2]]["items"]) + len(extra)

        def fake_run(jobs, label):
            if label == "取原文链接":
                return {j["id"]: {"status": 200, "url": "" if j["id"][1:] in no_url else f"https://example.gov.cn/{j['id']}"}
                        for j in jobs}
            if label == "取后面几页":
                return {j["id"]: {"status": 200, "items": pages[j["id"]]} for j in jobs}
            return results

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(MODULE.concierge, "evaluate"), \
                mock.patch.object(MODULE.concierge, "run_jobs", side_effect=fake_run), \
                mock.patch.object(MODULE.concierge, "today_cn", return_value=TODAY), \
                mock.patch.object(sys, "argv", ["deadline_list.py", *argv, "--out", tmp]):
            code = MODULE.main()
            files = sorted(Path(tmp).glob("*.txt"))
            return code, {f.name: f.read_text() for f in files}

    def test_filters_and_writes_two_files(self):
        a_items = [entry(1, "2026-10-23"), entry(9, "2026-10-18")]  # 第 9 条已过期
        b_items = [
            entry(1, "2026-10-23", source="verified"),
            entry(2, "2026-10-30", source="excerpt", quote="10月30日前"),
            entry(3, "2026-10-25", source="excerpt", quote="10月25日前"),  # 只剩 6 天：不写
            entry(4, "2026-10-31", source="excerpt", quote=""),  # 没原句：不写
            entry(5, "2026-10-31", source="excerpt", quote="10月31日前", level="市级", city="济南"),
        ]
        soon = [{"id": "1", "title": "已核对的那份"}, {"id": "6", "title": "关于第6项的通知", "level": "省级"}]
        code, files = self.run_main(a_items, b_items, soon)
        self.assertEqual(code, 0)
        shareable = next(t for n, t in files.items() if "可转发" in n)
        private = next(t for n, t in files.items() if "只给专员" in n)
        self.assertIn("关于申报第1项的通知", shareable)
        self.assertNotIn("第9项", shareable)
        self.assertIn("二、机器摘录·未经人工核对（2 条", private)  # 第 2、5 条
        for absent in ("第3项", "第4项"):
            self.assertNotIn(absent, private)
        # 第 1 条的另一类截止已核对、也在可转发里，7 天内还没核对的那类照样列出
        self.assertIn("三、7 天内可能截止、还没人工核对（2 条", private)
        self.assertIn("https://example.gov.cn/d2", private)

    def test_more_pages_are_fetched_before_filtering(self):
        with mock.patch.object(MODULE, "PAGE_SIZE", 1):
            code, files = self.run_main([entry(1, "2026-10-23")], [], [], pages={"a0p2": [entry(7, "2026-10-24")]})
        self.assertEqual(code, 0)
        shareable = next(t for n, t in files.items() if "可转发" in n)
        self.assertIn("第7项", shareable)

    def test_missing_link_returns_error(self):
        code, _ = self.run_main([entry(1, "2026-10-23")], [], [], no_url=("1",))
        self.assertEqual(code, 1)

    def test_city_filter_and_empty_shareable(self):
        b_items = [entry(5, "2026-10-31", source="excerpt", quote="10月31日前", level="市级", city="济南")]
        code, files = self.run_main([], b_items, [], argv=("山东", "--city", "青岛"))
        self.assertEqual(code, 0)
        shareable = next(t for n, t in files.items() if "可转发" in n)
        private = next(t for n, t in files.items() if "只给专员" in n)
        self.assertIn("这次不要发", shareable)
        self.assertIn("二、机器摘录·未经人工核对（0 条", private)
        self.assertTrue(all("山东_青岛_" in name for name in files))

    def test_uncovered_province_and_bad_name(self):
        with mock.patch.object(sys, "argv", ["deadline_list.py", "山东省"]):
            self.assertEqual(MODULE.main(), 1)
        results = {"cov": COVERAGE, "a0": {"status": 200}, "b0": {"status": 200}}
        with mock.patch.object(MODULE.concierge, "evaluate"), \
                mock.patch.object(MODULE.concierge, "run_jobs", return_value=results), \
                mock.patch.object(sys, "argv", ["deadline_list.py", "浙江"]):
            self.assertEqual(MODULE.main(), 1)


if __name__ == "__main__":
    unittest.main()
