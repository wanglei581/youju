"""扫描页识别文字（D-125）：开源的 RapidOCR 在我们自己的机器上识别，开关 YOUJU_OCR_ENABLED 默认关。

识别引擎大多用冒充的（不装识别组件也能测）：拼行、去页码、和取出的字分开分段、遮号码、存进三张表、
详情页和阅读页的说法、识别任务（开关、组件、锁、下载核对、页数上限、出错重来）。装了识别组件
（uv sync --extra ocr）时另有一项真识别（CI 的"扫描页识别"一项装了组件跑它）。"""

import importlib.util
import io
import resource
import sys
import time
from dataclasses import replace
from datetime import date
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sql

from youju import attachment_text as at
from youju import attachments as copies
from youju import cli, pdf_sandbox
from youju.config import get_settings
from youju.crawler.fetcher import FetchResult
from youju.models import AttachmentTextSegment, AttachmentTextUrl, AttachmentTextVersion

from .test_attachment_text import BASE, pdf_pages
from .test_attachments import APPLY, NOW, RECENT, FakeFetcher, add_doc
from .test_readers import client, path, seed  # noqa: F401 - client 是 pytest 夹具

FULL = {"images": [(0, 0, 300, 400)]}  # 整页是图片：扫描页
OCR_READY = importlib.util.find_spec("rapidocr_onnxruntime") is not None


def ocr_limits(pages: int = 10) -> at.Limits:
    return replace(at._limits(), ocr_pages=pages)


def texts(result) -> list[str]:
    return [s.text for s in result.segments]


def _boxes(i: int, line) -> list[list]:
    """第 i 行的框：写成字符串的是一个框；写成列表的是从左到右的几格（格和格隔开 44），其中写成
    元组的一格是挨得很近的几个框（隔开 4，比如一个号码被拆开了）。行高 12。"""
    top, bottom = 40.0 + 20 * i, 52.0 + 20 * i
    out, x = [], 30.0
    for cell in line if isinstance(line, list) else [line]:
        for part in cell if isinstance(cell, tuple) else (cell,):
            right = x + 9 * len(part)
            out.append([top, bottom, x, part, 0.95, right])
            x = right + 4
        x += 40
    return out


def fake_ocr(monkeypatch, recognized: dict[int, list], *, calls=None, error=None):
    """冒充识别子进程：recognized 是 {页码: [一行一行]}，一行怎么写见 _boxes；没写的页什么都没认出。
    别的 task（取文字）照样走真的子进程。"""
    real = pdf_sandbox.read

    def read(task, data, **options):
        if task != "ocr":
            return real(task, data, **options)
        if calls is not None:
            calls.append(options)
        if error is not None:
            raise error
        pages = {}
        for number in options["pages"]:
            if number in recognized:
                lines = [
                    box for i, line in enumerate(recognized[number]) for box in _boxes(i, line)
                ]
                pages[str(number)] = {"height": 400.0, "lines": lines}
        return {"pages": pages}

    monkeypatch.setattr(pdf_sandbox, "read", read)


# ---- 一页识别出的框拼成行 ----


def test_boxes_become_lines_low_scores_and_margin_page_numbers_dropped() -> None:
    page = {
        "height": 842.0,
        "lines": [
            # 同一行的三个框（中心高度差不到半行）：从左到右用空格接
            [100.0, 112.0, 300.0, "金额", 0.98],
            [101.0, 113.0, 60.0, "项目名称", 0.97],
            [99.0, 111.0, 180.0, "承担单位", 0.96],
            [130.0, 142.0, 60.0, "第二行的文字", 0.9],
            [160.0, 172.0, 60.0, "公章认出的乱码", 0.31],  # 置信度低的不要
            [820.0, 832.0, 290.0, "- 3 -", 0.99],  # 页脚的页码
            [400.0, 412.0, 290.0, "3", 0.99],  # 正文中间单独一个数字不是页码，留着
        ],
    }
    assert at._ocr_lines(page) == [
        (99.0, ["项目名称", "承担单位", "金额"]),
        (130.0, ["第二行的文字"]),
        (400.0, ["3"]),
    ]
    # 带右边的：挨得很近的框（间隔不到行高的 0.8）接成一格，隔得开的各是一格
    page = {
        "height": 842.0,
        "lines": [
            [100.0, 112.0, 60.0, "编号", 0.95, 84.0],
            [100.0, 112.0, 120.0, "1101011990", 0.95, 180.0],
            [100.0, 112.0, 184.0, "03071234", 0.95, 232.0],
            [100.0, 112.0, 300.0, "12000000", 0.95, 348.0],
        ],
    }
    assert at._ocr_lines(page) == [(100.0, ["编号", "1101011990 03071234", "12000000"])]


# ---- PDF 里的扫描页 ----


def test_recognized_pages_get_a_marker_each_and_stay_apart_from_extracted_text(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    fake_ocr(
        monkeypatch,
        {
            2: ["第二页的第一行文字", "联系电话：13812345678。"],
            3: ["第三页的文字也认出来了"],
            5: ["太少"],  # 不到 10 个字：按没认出算，还是扫描页
            7: ["第七页单独一页认出的文字"],
        },
        calls=calls,
    )
    data = pdf_pages(
        [
            {"text": [(40, 370, "第一页的正文。")]},
            FULL,
            FULL,
            {"text": [(40, 370, "第四页的正文。")]},
            FULL,
            {"text": [(40, 370, "第六页的正文。")]},
            FULL,
        ]
    )
    result = at.extract(data, limits=ocr_limits())
    assert calls[0]["pages"] == [2, 3, 5, 7]
    assert calls[0]["memory"] == pdf_sandbox.OCR_MEMORY_MAX and calls[0]["threads"] == 1
    assert result.ok and (result.pages, result.scanned_pages, result.ocr_pages) == (7, 1, 3)
    assert texts(result) == [
        "第一页的正文。",
        "（这一页是扫描件，文字是识别出来的）",
        "第二页的第一行文字",
        "联系电话：[手机号已脱敏]。",
        "（这一页是扫描件，文字是识别出来的）",
        "第三页的文字也认出来了",
        "第四页的正文。",
        "第六页的正文。",
        "（这一页是扫描件，文字是识别出来的）",
        "第七页单独一页认出的文字",
    ]
    assert [s.loc for s in result.segments] == [
        {"page": 1},
        {"page": 2, "ocr": 1},
        {"page": 2, "ocr": 1},
        {"page": 2, "ocr": 1},
        {"page": 3, "ocr": 1},
        {"page": 3, "ocr": 1},
        {"page": 4},
        {"page": 6},
        {"page": 7, "ocr": 1},
        {"page": 7, "ocr": 1},
    ]
    assert result.masked == 1
    # 平常取文字不识别（不起识别的子进程），扫描页照旧
    calls.clear()
    plain = at.extract(data)
    assert calls == [] and (plain.scanned_pages, plain.ocr_pages) == (4, 0)
    # 一份最多识别几页：按页码先后
    at.extract(data, limits=ocr_limits(2))
    assert calls[-1]["pages"] == [2, 3]


def test_whole_scanned_pdf_reads_after_recognition(monkeypatch) -> None:
    fake_ocr(monkeypatch, {1: ["第一页认出来的一行字"], 2: ["第二页认出来的一行字"]})
    data = pdf_pages([FULL, FULL])
    assert at.extract(data).reason == "scan"
    result = at.extract(data, limits=ocr_limits())
    assert result.ok and (result.scanned_pages, result.ocr_pages) == (0, 2)
    assert texts(result)[0] == at.OCR_MARKER and texts(result).count(at.OCR_MARKER) == 2
    # 识别没成（内存、时间超了等）：和没识别一样还是扫描件；起不了子进程这类照样抛出，下次再来
    fake_ocr(monkeypatch, {}, error=pdf_sandbox.Failed("memory", "too_large", "内存超了"))
    assert at.extract(data, limits=ocr_limits()).reason == "scan"
    fake_ocr(monkeypatch, {}, error=pdf_sandbox.Failed("spawn", "broken", "起不了", "起不了"))
    with pytest.raises(pdf_sandbox.Failed):
        at.extract(data, limits=ocr_limits())


def test_pages_are_recognized_in_batches_and_a_failed_batch_only_loses_itself(
    monkeypatch,
) -> None:
    calls: list[dict] = []
    recognized = {n: [f"第{n}页认出来的一行字"] for n in range(1, 26)}
    real = pdf_sandbox.read

    def read(task, data, **options):
        if task != "ocr":
            return real(task, data, **options)
        calls.append(options)
        if options["pages"][0] == 11:  # 第二批内存超了
            raise pdf_sandbox.Failed("memory", "too_large", "内存超了")
        lines = {n: recognized[n] for n in options["pages"]}
        return {
            "pages": {
                str(n): {"height": 400.0, "lines": [[40.0, 52.0, 30.0, t[0], 0.95]]}
                for n, t in lines.items()
            }
        }

    monkeypatch.setattr(pdf_sandbox, "read", read)
    result = at.extract(pdf_pages([FULL] * 25), limits=ocr_limits(100))
    assert [c["pages"] for c in calls] == [
        list(range(1, 11)),
        list(range(11, 21)),
        list(range(21, 26)),
    ]
    assert [c["seconds"] for c in calls] == [
        at.ocr_seconds(10),
        at.ocr_seconds(10),
        at.ocr_seconds(5),
    ]
    assert result.ok and (result.ocr_pages, result.scanned_pages) == (15, 10)
    assert texts(result)[0] == at.OCR_MARKER and texts(result).count(at.OCR_MARKER) == 15
    assert at.ocr_seconds(25) == 3 * 60 + 25 * 40


def test_scanned_pdf_inside_a_zip_is_recognized(monkeypatch) -> None:
    import zipfile

    fake_ocr(monkeypatch, {1: ["压缩包里扫描件认出来的字"]})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("申报书.pdf", pdf_pages([FULL]))
        z.writestr("说明.pdf", pdf_pages([{"text": [(40, 370, "Plain text page.")]}]))
    result = at.extract(out.getvalue(), limits=ocr_limits())
    assert result.ok and result.ocr_pages == 1
    assert "压缩包里扫描件认出来的字" in texts(result)
    assert "Plain text page." in texts(result)


def test_recognized_tables_are_masked_by_column_and_long_numbers_always(monkeypatch) -> None:
    """识别页没有表格结构：表头那一行有一格写着"账号"，往后各行的账号（不过 Luhn、没写标签，四位
    一组的也算）一格一格遮，旁边那格的金额不动；没有列名的识别页上，15 位以上的数字一律遮（拆成
    两个框的也算）。金额、日期、序号不动。"""
    fake_ocr(
        monkeypatch,
        {
            1: [
                ["序号", "单位名称", "开户银行", "账号", "金额"],
                ["1", "某某公司", "工行某支行", "1202021309001234568", "50"],
                ["2", "某某研究院", "建行某支行", "3301 2345 6789 0123", "4500"],
            ],
            2: ["第二页接着的一行", ["3", "某某中心", "农行某支行", "6000123456789012", "800"]],
        },
    )
    result = at.extract(pdf_pages([FULL, FULL]), limits=ocr_limits())
    got = texts(result)
    assert "1 某某公司 工行某支行 [银行账号已脱敏] 50" in got
    assert "2 某某研究院 建行某支行 [银行账号已脱敏] 4500" in got
    assert "3 某某中心 农行某支行 [银行账号已脱敏] 800" in got  # 列名带到后面的识别页
    assert result.masked == 3
    fake_ocr(
        monkeypatch,
        {
            1: [
                "备注：2026-12-15 合计 1350 820 4500 万元，另附 120202130900123456 一份",
                ["编号", ("1101011990", "03071234"), "联系人"],  # 一个号码被拆成挨着的两个框
                ["收款账户", ("1202021309", "001234568"), "说明"],  # 账号拆成两段
                ["预算（元）", "12000000", "8000000"],  # 相邻两格的金额不连成一个号码
                "合计 12500 13600 14700 万元",  # 一个框里的一串金额、年份不遮
                "年度 2023 2024 2025 2026",
                "分别为 125000 368000 520000 元",
                "请转至 622 2021 2345 6789 0123 或 6222 0212 3456 7890 123",  # 分组写的账号
                # 认成一个框的几个金额碰巧是卡号的分法（中间每组 4 位）：宁可多遮（取舍，见 _card_like）
                "单价 500 1500 3000 2000 元",
            ],
            2: [
                "序号 单位名称 开户银行 账号 金额",  # 没有边框的表头认成了一个框
                ["1", "某某公司", "中行某支行", "123456789012", "50"],  # 12 位的账号
                "采购编号 912345678901234567",  # 18 位，不是身份证号的格式：按账号说
            ],
        },
    )
    got = texts(at.extract(pdf_pages([FULL, FULL]), limits=ocr_limits()))
    assert "编号 [身份证号已脱敏] 联系人" in got  # 一行几格的自成一段
    assert "1 某某公司 中行某支行 [银行账号已脱敏] 50" in got
    assert "收款账户 [银行账号已脱敏] 说明" in got
    assert "预算（元） 12000000 8000000" in got
    joined = "\n".join(got)  # 一个框的几行可能接成一段
    # 18 位、不是身份证号的格式（地区码、出生日期对不上）：照样遮，按账号说
    assert "备注：2026-12-15 合计 1350 820 4500 万元，另附 [银行账号已脱敏] 一份" in joined
    assert "合计 12500 13600 14700 万元" in joined
    assert "年度 2023 2024 2025 2026" in joined
    assert "分别为 125000 368000 520000 元" in joined
    assert "请转至 [银行账号已脱敏] 或 [银行账号已脱敏]" in joined
    assert "单价 [银行账号已脱敏] 元" in joined
    assert "采购编号 [银行账号已脱敏]" in joined


def test_engine_that_cannot_start_in_the_child_is_an_environment_problem(
    pdf_in_process, monkeypatch
) -> None:
    """识别组件在子进程里起不来（装坏了、模型缺了）：Failed 的 setup，retry 为真（不算文件的，
    不记成识别过）。"""

    class Broken:
        def __init__(self, **options):
            raise RuntimeError("模型文件缺了")

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", SimpleNamespace(RapidOCR=Broken))
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(setNumThreads=lambda n: None))
    with pytest.raises(pdf_sandbox.Failed) as caught:
        pdf_sandbox.read(
            "ocr", pdf_pages([FULL]), seconds=60, pages=[1], dpi=200, pixels=10**8, threads=1
        )
    assert (caught.value.kind, caught.value.retry) == ("setup", True)
    assert caught.value.detail == "识别组件起不来（RuntimeError）"


def test_setup_answer_from_a_real_child_is_an_environment_problem(monkeypatch) -> None:
    """真的子进程写回 {"setup": ...}（识别组件起不来）：照样认得，不当成"写回的结果不对"。"""
    boot = (
        "import json, os\n"
        'raw = json.dumps({"setup": "ModuleNotFoundError"}).encode()\n'
        'os.write(1, len(raw).to_bytes(8, "big") + raw)\n'
        "os._exit(0)\n"
    )
    monkeypatch.setattr(pdf_sandbox, "_BOOT", boot)
    with pytest.raises(pdf_sandbox.Failed) as caught:
        pdf_sandbox.read(
            "ocr", pdf_pages([FULL]), seconds=60, pages=[1], dpi=200, pixels=10**8, threads=1
        )
    assert (caught.value.kind, caught.value.retry) == ("setup", True)
    assert caught.value.detail == "识别组件起不来（ModuleNotFoundError）"


def test_ocr_child_gets_thread_limits_in_its_environment() -> None:
    plain = pdf_sandbox._env({"task": "text", "options": {}})
    assert set(plain) == {"PATH", "LANG"}
    ocr = pdf_sandbox._env({"task": "ocr", "options": {"threads": 2}})
    assert (ocr["OMP_NUM_THREADS"], ocr["OPENBLAS_NUM_THREADS"], ocr["MALLOC_ARENA_MAX"]) == (
        "2",
        "1",
        "2",
    )
    assert not any("DATABASE" in key or "TOKEN" in key for key in ocr)


def test_ocr_task_renders_pages_and_returns_boxes_in_pdf_points(
    pdf_in_process, monkeypatch
) -> None:
    """子进程里识别的那一段（冒充的 RapidOCR、OpenCV）：只识别要的页、按分辨率换算回 PDF 的点、
    某一页出错只跳过这一页。"""
    seen = []

    class FakeEngine:
        def __init__(self, **options):
            seen.append(("options", options))

        def __call__(self, image):
            seen.append(("size", image.size))
            if len(seen) == 3:  # 第二次识别（第 3 页）出错
                raise ValueError("坏图片")
            # 像素坐标：200dpi 下 (100, 200)–(300, 250) 是 PDF 的 (36, 72)–(108, 90)
            return [[[[100, 200], [300, 200], [300, 250], [100, 250]], "认出的字", 0.9]], [0.1]

    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", SimpleNamespace(RapidOCR=FakeEngine))
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(setNumThreads=lambda n: None))
    data = pdf_pages([FULL, FULL, FULL])
    found = pdf_sandbox.read(
        "ocr", data, seconds=60, pages=[1, 3, 9], dpi=200, pixels=10**8, threads=1
    )
    assert seen[0] == ("options", {"intra_op_num_threads": 1, "inter_op_num_threads": 1})
    assert seen[1] == ("size", (834, 1112))  # 300×400 点，200dpi（向上取整）
    assert found == {
        "pages": {"1": {"height": 400.0, "lines": [[72.0, 90.0, 36.0, "认出的字", 0.9, 108.0]]}}
    }
    # 大幅面按像素上限降分辨率
    seen.clear()
    pdf_sandbox.read("ocr", data, seconds=60, pages=[1], dpi=200, pixels=120_000, threads=1)
    assert seen[1] == ("size", (300, 400))


# ---- 存进三张表、详情页和阅读页 ----


SCAN_PDF = pdf_pages([FULL, FULL, FULL])
SOME_PDF = pdf_pages(
    [
        {"text": [(40, 370, "Applicants must submit the application form and licence copy.")]},
        FULL,
        FULL,
    ]
)


def _version(session, url) -> AttachmentTextVersion:
    row = session.scalar(select(AttachmentTextUrl).where(AttachmentTextUrl.url == url))
    return session.get(AttachmentTextVersion, row.version_id)


def test_ocr_version_replaces_text_and_marks_links(session, monkeypatch) -> None:
    doc = add_doc(
        session,
        1,
        APPLY,
        RECENT,
        [("扫描件", BASE + "scan.pdf", "pdf"), ("有几页扫描", BASE + "some.pdf", "pdf")],
    )
    report = at.TextReport()
    at.record(session, BASE + "scan.pdf", "pdf", NOW, report, content=SCAN_PDF)
    at.record(session, BASE + "some.pdf", "pdf", NOW, report, content=SOME_PDF)
    scan, some = _version(session, BASE + "scan.pdf"), _version(session, BASE + "some.pdf")
    assert (scan.reason, some.status, some.scanned_pages) == ("scan", "ok", 2)
    assert [v.id for v in at.ocr_due(session)] == [some.id, scan.id]  # 新的在前
    fake_ocr(
        monkeypatch,
        {1: ["第一页认出来的一行字"], 2: ["第二页认出来的一行字"], 3: ["第三页认出来的一行字"]},
    )
    assert at.ocr_version(session, scan, SCAN_PDF, NOW, pages=3) == 3
    session.expire_all()
    scan = session.get(AttachmentTextVersion, scan.id)
    assert (scan.status, scan.reason, scan.ocr_rules, scan.ocr_pages, scan.scanned_pages) == (
        "ok",
        None,
        at.OCR_RULES,
        3,
        0,
    )
    assert scan.detail == "3 页扫描页的字是识别出来的"
    link = session.scalar(
        select(AttachmentTextUrl).where(AttachmentTextUrl.url == BASE + "scan.pdf")
    )
    assert (link.status, link.reason, link.changed_at) == ("ok", None, NOW)
    stored = session.scalars(
        select(AttachmentTextSegment).where(AttachmentTextSegment.version_id == scan.id)
    ).all()
    assert stored[0].text == at.OCR_MARKER and stored[0].loc == {"page": 1, "ocr": 1}
    # 有几页是扫描页的：第 3 页没认出
    fake_ocr(monkeypatch, {2: ["第二页认出来的一行字"]})
    at.ocr_version(session, some, SOME_PDF, NOW, pages=2)
    assert at.ocr_due(session) == []
    found = at.readings(session, doc.id, NOW)
    assert [(x.state, x.readable, x.ocr_pages, x.scanned_pages) for x in found] == [
        ("ok", True, 3, 0),
        ("ok", True, 1, 1),
    ]
    assert [x.note for x in found] == [
        "能读（扫描件，文字是识别出来的）",
        "能读，其中 1 页扫描件的文字是识别出来的；另有 1 页暂不能识别文字",
    ]


def test_zip_shares_one_page_budget_and_is_stored(session, monkeypatch) -> None:
    """压缩包里几个扫描 PDF 合起来最多识别 pages 页（一份最多 OCR_DOC_PAGES 页、一轮的页数才算得
    准），识别完照样存进去。"""
    import zipfile

    calls: list[dict] = []
    fake_ocr(monkeypatch, {1: ["第一页认出来的一行字"], 2: ["第二页认出来的一行字"]}, calls=calls)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("甲.pdf", pdf_pages([FULL, FULL]))
        z.writestr("乙.pdf", pdf_pages([FULL, FULL]))
    content = out.getvalue()
    add_doc(session, 1, APPLY, RECENT, [("压缩包", BASE + "pack.zip", "zip")])
    at.record(session, BASE + "pack.zip", "zip", NOW, at.TextReport(), content=content)
    version = _version(session, BASE + "pack.zip")
    assert (version.file_type, version.reason) == ("zip", "scan")
    assert at.ocr_version(session, version, content, NOW, pages=3) == 3
    assert [c["pages"] for c in calls] == [[1, 2], [1]]
    session.expire_all()
    assert (version.status, version.ocr_pages, version.scanned_pages) == ("ok", 3, 1)


def test_mixed_zip_is_stored_although_titles_and_summary_get_shorter(session, monkeypatch) -> None:
    """压缩包里有能读的 Word、也有扫描的 PDF：扫描件认出来以后，文件标题后面的"扫描件，暂不能识别
    文字"、开头说明里的"没有读出文字"都去掉了，这不算别的文字变少，照样存。"""
    import zipfile

    from .test_attachments import make_docx

    fake_ocr(monkeypatch, {1: ["扫描件第一页认出来的一行字"], 2: ["扫描件第二页认出来的一行字"]})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("通知.docx", make_docx("申报指南：请各单位按要求报送材料。" * 3))
        for name in ("甲.pdf", "乙.pdf", "丙.pdf"):
            z.writestr(name, pdf_pages([FULL, FULL]))
    content = out.getvalue()
    add_doc(session, 1, APPLY, RECENT, [("压缩包", BASE + "mixed.zip", "zip")])
    at.record(session, BASE + "mixed.zip", "zip", NOW, at.TextReport(), content=content)
    version = _version(session, BASE + "mixed.zip")
    assert (version.status, version.ocr_pages) == ("ok", 0)
    assert at.ocr_version(session, version, content, NOW, pages=100) == 6
    session.expire_all()
    assert (version.ocr_pages, version.scanned_pages) == (6, None)


def test_recognition_that_shrinks_the_other_text_is_not_stored(session, monkeypatch) -> None:
    """识别以后别的文字反而少了（字数上限、压缩包整份的时间被识别占掉）：原来的段落照留，只记下
    识别过。"""
    add_doc(session, 1, APPLY, RECENT, [("有几页扫描", BASE + "some.pdf", "pdf")])
    at.record(session, BASE + "some.pdf", "pdf", NOW, at.TextReport(), content=SOME_PDF)
    version = _version(session, BASE + "some.pdf")
    before = session.scalars(
        select(AttachmentTextSegment.text).where(AttachmentTextSegment.version_id == version.id)
    ).all()
    shrunk = at.Extraction(
        "pdf",
        [
            at.Segment(1, "p", at.OCR_MARKER, {"page": 2, "ocr": 1}, None),
            at.Segment(2, "p", "第二页认出来的一行字", {"page": 2, "ocr": 1}, None),
        ],
        pages=3,
        scanned_pages=1,
        ocr_pages=1,
    )
    monkeypatch.setattr(at, "extract", lambda data, limits=None: shrunk)
    assert at.ocr_version(session, version, SOME_PDF, NOW, pages=2) == 0
    session.expire_all()
    assert (version.ocr_rules, version.ocr_pages, version.ocr_tries) == (at.OCR_RULES, 0, 0)
    assert (
        session.scalars(
            select(AttachmentTextSegment.text).where(AttachmentTextSegment.version_id == version.id)
        ).all()
        == before
    )


def test_failed_recognition_is_remembered_and_rule_changes_retry(session, monkeypatch) -> None:
    add_doc(session, 1, APPLY, RECENT, [("扫描件", BASE + "scan.pdf", "pdf")])
    at.record(session, BASE + "scan.pdf", "pdf", NOW, at.TextReport(), content=SCAN_PDF)
    version = _version(session, BASE + "scan.pdf")
    fake_ocr(monkeypatch, {})  # 一页都没认出
    assert at.ocr_version(session, version, SCAN_PDF, NOW, pages=3) == 0
    session.expire_all()
    version = session.get(AttachmentTextVersion, version.id)
    assert (version.reason, version.ocr_rules, version.ocr_pages) == ("scan", at.OCR_RULES, 0)
    assert at.ocr_due(session) == []  # 不反复识别
    monkeypatch.setattr(at, "OCR_RULES", at.OCR_RULES + 1)  # 识别规则改了：按新规则再来
    assert [v.id for v in at.ocr_due(session)] == [version.id]
    # 以前每页都认出了的（没有还剩的扫描页）规则改了也按新规则重识别
    monkeypatch.setattr(at, "OCR_RULES", 1)
    fake_ocr(
        monkeypatch,
        {1: ["第一页认出来的一行字"], 2: ["第二页认出来的一行字"], 3: ["第三页认出来的一行字"]},
    )
    version.ocr_rules = 0
    session.commit()
    at.ocr_version(session, version, SCAN_PDF, NOW, pages=3)
    session.expire_all()
    assert (version.scanned_pages, version.ocr_pages, version.reason) == (0, 3, None)
    assert at.ocr_due(session) == []
    monkeypatch.setattr(at, "OCR_RULES", 2)
    assert [v.id for v in at.ocr_due(session)] == [version.id]
    # 平常取文字重取了（规则改了、内容变了）：原来识别出的字没了，识别任务再来
    monkeypatch.setattr(at, "OCR_RULES", 1)
    fake_ocr(monkeypatch, {1: ["第一页认出来的一行字"]})
    version.ocr_rules = 0
    session.commit()
    at.ocr_version(session, version, SCAN_PDF, NOW, pages=3)
    at._store(session, version, len(SCAN_PDF), at.extract(SCAN_PDF), NOW)
    session.commit()
    assert (version.ocr_rules, version.ocr_pages, version.reason) == (0, 0, "scan")
    assert [v.id for v in at.ocr_due(session)] == [version.id]


def test_reader_page_marks_recognized_text_and_lists_missed_pages(session, client) -> None:  # noqa: F811
    doc, att, _url, version = seed(session)
    version.file_type, version.pages, version.ocr_pages, version.scanned_pages = "pdf", 3, 3, 0
    segment = session.scalar(
        select(AttachmentTextSegment).where(AttachmentTextSegment.version_id == version.id)
    )
    segment.text, segment.loc = at.OCR_MARKER, {"page": 1, "ocr": 1}
    session.commit()
    # 都认出了：顶上不另写（文字开头那一行标明是识别出来的），段落出处只给页码
    data = client.get(path(doc, att)).json()
    assert (data["status"], data["scannedPages"]) == ("ok", 0)
    assert data["segments"][0] == {**data["segments"][0], "text": at.OCR_MARKER}
    assert data["segments"][0]["loc"] == {"page": 1}
    # 还有没认出的页：顶上写缺几页（小程序只在 partial 时显示）
    version.ocr_pages, version.scanned_pages = 2, 1
    session.commit()
    data = client.get(path(doc, att)).json()
    assert (data["status"], data["scannedPages"]) == ("partial", 1)
    assert data["notice"] == "其中 1 页扫描件暂不能识别文字，下面的文字不含这些页。"
    # 没识别过的照旧
    version.ocr_pages = 0
    session.commit()
    data = client.get(path(doc, att)).json()
    assert data["notice"] == "其中 1 页是扫描件，暂不能识别文字，下面的文字不含这些页。"


# ---- 识别任务（youju ocr-scans） ----


@pytest.fixture
def ocr_on(monkeypatch):
    monkeypatch.setenv("YOUJU_OCR_ENABLED", "1")
    monkeypatch.setattr(copies, "ocr_engine_ready", lambda: True)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _two_scans(session):
    """两份通知各一个扫描件：old.pdf 在 9/1 发布的那份里，new.pdf 在 10/1 发布的那份里。new.pdf
    先记（内容版本编号小）：按编号从新到旧会先识别 old.pdf，按发布日期才先识别 new.pdf。"""
    add_doc(session, 1, APPLY, date(2026, 9, 1), [("旧的", BASE + "old.pdf", "pdf")])
    add_doc(session, 2, APPLY, RECENT, [("新的", BASE + "new.pdf", "pdf")])
    report = at.TextReport()
    old = pdf_pages([FULL, FULL])
    new = pdf_pages([FULL])
    at.record(session, BASE + "new.pdf", "pdf", NOW, report, content=new)
    at.record(session, BASE + "old.pdf", "pdf", NOW, report, content=old)
    return old, new


def test_ocr_job_is_off_by_default_and_needs_the_engine(session, monkeypatch) -> None:
    _two_scans(session)
    fetcher = FakeFetcher({})
    get_settings.cache_clear()
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert report.skipped == "开关关着（YOUJU_OCR_ENABLED）" and fetcher.calls == []
    monkeypatch.setenv("YOUJU_OCR_ENABLED", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(copies, "ocr_engine_ready", lambda: False)
    try:
        report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    finally:
        get_settings.cache_clear()
    assert report.skipped == "没装识别组件（uv sync --extra ocr）" and fetcher.calls == []


def test_ocr_job_newest_first_within_the_page_budget(session, monkeypatch, ocr_on) -> None:
    old, new = _two_scans(session)
    fake_ocr(monkeypatch, {1: ["第一页认出来的一行字"], 2: ["第二页认出来的一行字"]})
    fetcher = FakeFetcher({BASE + "old.pdf": old, BASE + "new.pdf": new})
    assert _version(session, BASE + "new.pdf").id < _version(session, BASE + "old.pdf").id
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW, pages=1)
    assert fetcher.calls == [BASE + "new.pdf"]
    assert (report.documents, report.recognized, report.pages, report.ocr_pages) == (1, 1, 1, 1)
    assert report.deferred == 1
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert fetcher.calls == [BASE + "new.pdf", BASE + "old.pdf"]
    assert (report.documents, report.ocr_pages, report.deferred) == (1, 2, 0)
    assert copies.ocr_scans(session, fetcher=fetcher, now=NOW).documents == 0


def test_ocr_job_only_takes_attachments_in_the_text_scope(session, monkeypatch, ocr_on) -> None:
    """取文字范围以外的（申报通知过了 180 天等，取文字下一轮会清掉）不识别。"""
    add_doc(session, 1, APPLY, date(2026, 1, 5), [("早就过了范围", BASE + "old.pdf", "pdf")])
    at.record(session, BASE + "old.pdf", "pdf", NOW, at.TextReport(), content=SCAN_PDF)
    fake_ocr(monkeypatch, {1: ["试认的小图认出来的字"]})
    assert len(at.ocr_due(session)) == 1
    fetcher = FakeFetcher({BASE + "old.pdf": SCAN_PDF})
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert (report.documents, report.deferred, fetcher.calls) == (0, 0, [])


def test_ocr_job_checks_the_download_and_retries_environment_errors(
    session, monkeypatch, ocr_on
) -> None:
    old, new = _two_scans(session)
    fake_ocr(monkeypatch, {1: ["第一页认出来的一行字"]})
    gone = FetchResult(BASE + "old.pdf", "error", http_status=404, error="HTTP 404")
    # 新的那份原网站换了文件（等平常的取文字重取）；旧的那份下载不了
    fetcher = FakeFetcher({BASE + "new.pdf": pdf_pages([FULL, FULL, FULL]), BASE + "old.pdf": gone})
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert (report.changed, report.unavailable, report.documents) == (1, 1, 0)
    # 换了的文件按平常取文字的办法记下了：链接改指新内容（也是扫描件，下一轮识别），旧内容没有
    # 链接指向，不再排队；下载不了的那份没记成识别过，下次再来
    changed = session.scalar(
        select(AttachmentTextUrl).where(AttachmentTextUrl.url == BASE + "new.pdf")
    )
    fresh = session.get(AttachmentTextVersion, changed.version_id)
    assert fresh.sha256 == sha256(pdf_pages([FULL, FULL, FULL])).hexdigest()
    assert sorted(v.id for v in at.ocr_due(session)) == sorted(
        [fresh.id, _version(session, BASE + "old.pdf").id]
    )
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert (report.changed, report.documents) == (0, 1)  # 新内容这次识别了，不再反复下载


def test_ocr_job_stops_when_the_engine_is_broken(session, monkeypatch, ocr_on, capsys) -> None:
    """识别组件装了却用不了（子进程里导入、模型、推理出错）：每轮先试认一张小图，没成就整轮不识别、
    命令行返回 1（发提醒），排着的一份也不记成识别过。"""
    old, new = _two_scans(session)
    fake_ocr(
        monkeypatch, {}, error=pdf_sandbox.Failed("setup", "broken", "识别组件起不来（X）", "x")
    )
    fetcher = FakeFetcher({BASE + "new.pdf": new, BASE + "old.pdf": old})
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert report.broken == "识别组件用不了（识别组件起不来（X）），这轮没识别"
    assert (fetcher.calls, report.documents) == ([], 0)
    assert len(at.ocr_due(session)) == 2
    fake_ocr(monkeypatch, {})  # 能跑，但一个字也认不出来（推理坏了）
    assert copies.ocr_scans(session, fetcher=fetcher, now=NOW).broken.startswith("识别组件用不了")
    real = copies.ocr_scans
    monkeypatch.setattr(
        copies, "ocr_scans", lambda s, **kw: real(s, **{**kw, "fetcher": fetcher, "now": NOW})
    )
    assert cli.main(["ocr-scans"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "ocr_scans broken=1"


def test_ocr_job_retries_environment_errors_twice_then_gives_up(
    session, monkeypatch, ocr_on
) -> None:
    """冒烟过了、某一份识别时子进程出事（崩了、被停、卡住，也可能是文件让渲染出事）：这一份记出错、
    这轮的页数照算，下次再来；连着两次都这样的，第三次记成识别过、不再试。"""
    _old, new = _two_scans(session)
    real_read = pdf_sandbox.read

    def read(task, data, **options):
        if task != "ocr":
            return real_read(task, data, **options)
        if data == new:  # 这一份每次都让子进程崩了；试认的小图照常认得出
            raise pdf_sandbox.Failed("crash", "broken", "子进程崩了", "崩了")
        return {"pages": {"1": {"height": 400.0, "lines": [[40.0, 52.0, 30.0, "试认的小图", 0.9]]}}}

    monkeypatch.setattr(pdf_sandbox, "read", read)
    fetcher = FakeFetcher({BASE + "new.pdf": new, BASE + "old.pdf": pdf_pages([FULL, FULL])})
    version = _version(session, BASE + "new.pdf")
    for tries in (1, 2):
        report = copies.ocr_scans(session, fetcher=fetcher, now=NOW, pages=1)
        assert (report.errors, report.documents, report.pages) == (1, 0, 1)
        session.expire_all()
        assert (version.ocr_tries, version.ocr_rules) == (tries, 0)
    fetcher.calls.clear()
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW, pages=1)
    assert report.gave_up == 1
    assert BASE + "new.pdf" not in fetcher.calls  # 不再试的不用再下载
    session.expire_all()
    assert (version.ocr_tries, version.ocr_rules, version.reason) == (0, at.OCR_RULES, "scan")


def test_environment_failures_do_not_count_as_tries(session, monkeypatch) -> None:
    """起不了子进程、识别组件起不来这些只和当时的环境有关：抛出、下次再来，但不算次数。"""
    add_doc(session, 1, APPLY, RECENT, [("扫描件", BASE + "scan.pdf", "pdf")])
    at.record(session, BASE + "scan.pdf", "pdf", NOW, at.TextReport(), content=SCAN_PDF)
    version = _version(session, BASE + "scan.pdf")
    for kind in ("spawn", "setup", "spawn"):
        fake_ocr(monkeypatch, {}, error=pdf_sandbox.Failed(kind, "broken", "环境出事", "环境"))
        with pytest.raises(pdf_sandbox.Failed):
            at.ocr_version(session, version, SCAN_PDF, NOW, pages=3)
        session.expire_all()
        assert (version.ocr_tries, version.ocr_rules) == (0, 0)
    fake_ocr(monkeypatch, {}, error=pdf_sandbox.Failed("busy", "too_large", "卡住了", "卡住"))
    with pytest.raises(pdf_sandbox.Failed):
        at.ocr_version(session, version, SCAN_PDF, NOW, pages=3)
    session.expire_all()
    assert version.ocr_tries == 1


def test_ocr_job_skips_while_another_attachment_job_runs(session, monkeypatch, ocr_on) -> None:
    _two_scans(session)
    fetcher = FakeFetcher({})
    with session.get_bind().connect() as other:
        assert other.execute(sql("SELECT GET_LOCK('youju_attachment_copies', 0)")).scalar() == 1
        try:
            report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
        finally:
            other.execute(sql("SELECT RELEASE_LOCK('youju_attachment_copies')"))
    assert report.skipped.startswith("另一个附件任务正在运行") and fetcher.calls == []


def test_cli_ocr_scans(session, monkeypatch, capsys) -> None:
    get_settings.cache_clear()
    assert cli.main(["ocr-scans"]) == 0
    out = capsys.readouterr().out
    assert "没有识别：开关关着（YOUJU_OCR_ENABLED）" in out
    assert out.splitlines()[-1] == "ocr_scans skipped=1"
    _old, new = _two_scans(session)
    fake_ocr(monkeypatch, {1: ["第一页认出来的一行字"]})
    fetcher = FakeFetcher(
        {
            BASE + "new.pdf": new,
            BASE + "old.pdf": FetchResult(
                BASE + "old.pdf", "error", http_status=404, error="HTTP 404"
            ),
        }
    )
    real = copies.ocr_scans
    monkeypatch.setattr(
        copies, "ocr_scans", lambda s, **kw: real(s, **{**kw, "fetcher": fetcher, "now": NOW})
    )
    monkeypatch.setenv("YOUJU_OCR_ENABLED", "1")
    monkeypatch.setattr(copies, "ocr_engine_ready", lambda: True)
    get_settings.cache_clear()
    try:
        assert cli.main(["ocr-scans", "--pages", "50"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "识别了 1 份（认出字的 1 份）" in out and "原网站下载不了 1 份" in out
    assert out.splitlines()[-1] == (
        "ocr_scans documents=1 recognized=1 pages=1 ocr_pages=1 unavailable=1 changed=0 "
        "errors=0 gave_up=0 deferred=0"
    )
    assert session.scalar(select(func.count()).select_from(AttachmentTextVersion)) == 2


# ---- 真识别（装了识别组件才跑） ----


def _rendered_scan(lines: int = 30) -> bytes:
    """现做一份"扫描件"：A4、200dpi 的整页图片，上面是 lines 行英文和数字（默认字体只有拉丁字母）。"""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=34)
    image = Image.new("L", (1654, 2339), 255)
    draw = ImageDraw.Draw(image)
    for i in range(lines):
        draw.text(
            (110, 140 + i * 68),
            f"Item {i + 1:02d}: submit the application form before 2026-12-{i % 28 + 1:02d}.",
            fill=0,
            font=font,
        )
    out = io.BytesIO()
    image.save(out, "PDF", resolution=200)
    return out.getvalue()


@pytest.mark.skipif(not OCR_READY, reason="没装识别组件（uv sync --extra ocr）")
def test_real_ocr_reads_a_rendered_scan() -> None:
    """真的 RapidOCR，在限内存（OCR_MEMORY_MAX，Linux 上管用）、限 CPU 时间的子进程里识别。打印
    用时和子进程的内存峰值（CI 的"扫描页识别"一项看这一行）。"""
    data = _rendered_scan()
    assert at.extract(data).reason == "scan"
    started = time.perf_counter()
    result = at.extract(data, limits=ocr_limits())
    took = time.perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak_mb = peak / 1024 / (1024 if sys.platform == "darwin" else 1)
    print(f"\nOCR_MEASURE pages=1 seconds={took:.1f} child_maxrss_mb={peak_mb:.0f}")
    assert result.ok and result.ocr_pages == 1, (result.reason, result.detail)
    joined = "".join(texts(result)).replace(" ", "").lower()
    assert texts(result)[0] == at.OCR_MARKER
    assert "applicationform" in joined and "2026-12-15" in joined
    # 每轮开头试认的那张小图，真的识别组件也要认得出（不然线上每轮都报"用不了"）
    assert at.ocr_smoke() is None
