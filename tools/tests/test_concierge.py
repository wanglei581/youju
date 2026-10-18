import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

TEAM = Path(__file__).parents[1] / "team"
sys.path.insert(0, str(TEAM))
SPEC = importlib.util.spec_from_file_location("concierge", TEAM / "concierge.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
try:  # CI 的工具测试环境没有 openpyxl：出 Excel 的测试只在本机跑
    import openpyxl  # noqa: F401

    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False
TODAY = date(2026, 10, 19)
QUOTE = "请于2026年10月30日17:00前完成网上申报"


def item(**due):
    return {"due": due or None, "deadline_state": "unverified"}


class DeadlineTextTests(unittest.TestCase):
    """截止一格和小程序 dueView 同一个口径（D-061）。"""

    def test_verified(self):
        text = MODULE.deadline_text(
            item(kind="企业申报截止", source="verified", date="2026-10-21", time="17:00"), TODAY
        )
        self.assertEqual(text, "2026-10-21 17:00（企业申报截止，已人工核对）")

    def test_verified_past_says_closed(self):
        text = MODULE.deadline_text(item(source="verified", date="2026-10-18"), TODAY)
        self.assertEqual(text, "2026-10-18已截止（已人工核对）")

    def test_excerpt_in_window_has_label_and_quote(self):
        text = MODULE.deadline_text(
            item(kind="企业申报截止", source="excerpt", date="2026-10-30", quote=QUOTE), TODAY
        )
        self.assertIn("机器摘录·未经人工核对", text)
        self.assertIn(QUOTE, text)
        self.assertTrue(text.startswith("2026-10-30"))

    def test_excerpt_without_quote_or_outside_8_to_30_days_has_no_date(self):
        for due in (
            {"source": "excerpt", "date": "2026-10-30", "quote": ""},
            {"source": "excerpt", "date": "2026-10-26", "quote": QUOTE},  # 7 天
            {"source": "excerpt", "date": "2026-11-19", "quote": QUOTE},  # 31 天
        ):
            with self.subTest(due=due):
                self.assertEqual(MODULE.deadline_text(item(**due), TODAY), "截止未核对，请以原文为准")

    def test_old_api_and_no_candidate(self):
        self.assertEqual(
            MODULE.deadline_text({"deadline": {"date": "2026-10-25"}}, TODAY), "2026-10-25（已人工核对）"
        )
        self.assertEqual(
            MODULE.deadline_text({"due": None, "deadline_state": "none"}, TODAY), "截止时间见原文"
        )

    def test_notice_search_asks_for_excerpts(self):
        path = MODULE.notice_path({"provinces": ["重庆"], "industries": ["先进制造"]})
        self.assertIn("excerpts=1", path)
        self.assertIn("regions=%E5%9B%BD%E5%AE%B6%E9%83%A8%E5%A7%94%2C%E9%87%8D%E5%BA%86", path)


LIST_A = {"title": "绿色工厂名单的公示", "issuer": "某市经济和信息化委员会", "date": "2026-10-16",
          "status": "公示", "url": "https://example.gov.cn/a.html"}
LIST_B = {"title": "企业技术中心名单", "issuer": "某省工业和信息化厅", "date": "2025-12-30",
          "status": "公布", "url": "https://example.gov.cn/b.html"}


def found(*lists, via="name", written=None):
    return {"status": 200, "items": [
        {**x, "matched": {"name": written, "via": via, "batch": None, "sequence": "3"}} for x in lists
    ], "coverage": {"lists": 1200, "since": "2025-01-02"}}


CLIENTS = [{"name": n, "provinces": [], "industries": [], "ignored": [], "region_text": ""}
           for n in ("甲公司", "乙公司", "丙公司", "丁", "戊公司")]
LOOKUPS = {
    "l0": found(LIST_A, LIST_B, written="甲公司"),
    "l1": found(LIST_A, via="creditCode", written="乙公司（更名前）"),
    "l2": {"status": 200, "items": [], "similar": [{"name": "丙公司（一厂）", "lists": 2}],
           "candidates": [], "coverage": {"lists": 1200, "since": "2025-01-02"}},
    "l3": {"status": 422, "error": "只查企业和机构"},
    "l4": {"status": 0, "error": "timeout"},
}


class LookupRowsTests(unittest.TestCase):
    def test_rows_failures_and_by_list(self):
        rows, failed, by_list = MODULE.lookup_rows(CLIENTS, LOOKUPS)
        self.assertEqual(failed, 1)
        self.assertEqual([r[1] for r in rows if r[0] == "甲公司"], ["上榜 2 份", "上榜 2 份"])
        self.assertIn("按统一社会信用代码相同找到", next(r[9] for r in rows if r[0] == "乙公司"))
        self.assertIn("丙公司（一厂）（2 份）", next(r[9] for r in rows if r[0] == "丙公司"))
        self.assertIn("名称太短或像个人姓名", next(r[1] for r in rows if r[0] == "丁"))
        self.assertIn("请重跑", next(r[1] for r in rows if r[0] == "戊公司"))
        self.assertEqual(
            by_list[LIST_A["url"]]["clients"], [("甲公司", "甲公司"), ("乙公司", "乙公司（更名前）")]
        )
        self.assertEqual(len(by_list), 2)

    def test_coverage_text_mentions_count_and_start_date(self):
        text = MODULE.coverage_text(LOOKUPS)
        self.assertIn("1200 份名单", text)
        self.assertIn("2025-01-02", text)


@unittest.skipUnless(HAS_OPENPYXL, "没有 openpyxl（CI 的工具测试环境），只在本机跑")
class ParkBookTests(unittest.TestCase):
    def test_three_sheets_counts_and_order(self):
        book, hit, failed = MODULE.park_book(CLIENTS, LOOKUPS, "2026-10-19 09:00")
        self.assertEqual(book.sheetnames, ["说明", "按名单看", "按企业看"])
        self.assertEqual((hit, failed), (2, 1))
        intro = "\n".join(str(c.value) for c in book["说明"]["A"] if c.value)
        self.assertIn("园区给的企业 5 家，其中 2 家", intro)
        self.assertIn(MODULE.STATEMENT, intro)
        self.assertIn("交付当天删除本机文件", intro)
        lists = list(book["按名单看"].iter_rows(min_row=2, values_only=True))
        self.assertEqual([r[0] for r in lists], [LIST_A["title"], LIST_B["title"]])  # 新的在前
        self.assertEqual(lists[0][4], 2)
        self.assertEqual(lists[0][5], "甲公司、乙公司")
        self.assertEqual(lists[0][6], "乙公司（名单里写作：乙公司（更名前））")
        with tempfile.TemporaryDirectory() as tmp:
            book.save(Path(tmp) / "x.xlsx")

    def test_advisor_book_keeps_four_sheets(self):
        clients = [{"name": "甲公司", "provinces": ["重庆"], "industries": [], "ignored": ["汽车"],
                    "region_text": "重庆"}]
        notices = {MODULE.notice_key(clients[0]): {"status": 200, "total": 1, "items": [
            {"title": "关于申报的通知", "issuer": "某厅", "date": "2026-10-10", "url": "https://example.gov.cn/n",
             "category": "", "due": {"kind": "企业申报截止", "source": "excerpt", "date": "2026-11-05",
                                      "quote": "2026年11月5日前"}}]}}
        book, hit, count, failed = MODULE.advisor_book(clients, {"l0": LOOKUPS["l0"]}, notices, "2026-10-19 09:00")
        self.assertEqual(book.sheetnames, ["说明", "上榜情况", "相关通知", "按通知看"])
        self.assertEqual((hit, count, failed), (1, 1, 0))
        row = next(book["相关通知"].iter_rows(min_row=2, values_only=True))
        self.assertIn("“汽车”不在可筛选的 8 项里", row[2])


if __name__ == "__main__":
    unittest.main()
