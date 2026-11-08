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


def fake_ocr(monkeypatch, recognized: dict[int, list[str]], *, calls=None, error=None):
    """冒充识别子进程：recognized 是 {页码: [一行一行的字]}，没写的页什么都没认出。别的 task（取
    文字）照样走真的子进程。"""
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
                    [40.0 + 20 * i, 52.0 + 20 * i, 30.0, text, 0.95]
                    for i, text in enumerate(recognized[number])
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
        (99.0, "项目名称 承担单位 金额"),
        (130.0, "第二行的文字"),
        (400.0, "3"),
    ]


# ---- PDF 里的扫描页 ----


def test_recognized_pages_get_one_marker_per_run_and_stay_apart_from_extracted_text(
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
        "（第 2–3 页是扫描件，文字是识别出来的）",
        "第二页的第一行文字",
        "联系电话：[手机号已脱敏]。",
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
    assert texts(result)[0] == "（这份文件是扫描件，文字是识别出来的）"
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
    assert texts(result)[0] == "（第 1–10 页是扫描件，文字是识别出来的）"
    assert "（第 21–25 页是扫描件，文字是识别出来的）" in texts(result)
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
        "pages": {"1": {"height": 400.0, "lines": [[72.0, 90.0, 36.0, "认出的字", 0.9]]}}
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
    result = at.ocr_version(session, scan, SCAN_PDF, NOW, pages=3)
    assert result.ok and result.ocr_pages == 3
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
    assert stored[0].text == at.OCR_MARKER_ALL and stored[0].loc == {"page": 1, "ocr": 1}
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
        "能读，其中 1 页扫描件的文字是识别出来的；另有 1 页没认出文字",
    ]


def test_failed_recognition_is_remembered_and_rule_changes_retry(session, monkeypatch) -> None:
    add_doc(session, 1, APPLY, RECENT, [("扫描件", BASE + "scan.pdf", "pdf")])
    at.record(session, BASE + "scan.pdf", "pdf", NOW, at.TextReport(), content=SCAN_PDF)
    version = _version(session, BASE + "scan.pdf")
    fake_ocr(monkeypatch, {})  # 一页都没认出
    result = at.ocr_version(session, version, SCAN_PDF, NOW, pages=3)
    assert result.reason == "scan"
    session.expire_all()
    version = session.get(AttachmentTextVersion, version.id)
    assert (version.reason, version.ocr_rules, version.ocr_pages) == ("scan", at.OCR_RULES, 0)
    assert at.ocr_due(session) == []  # 不反复识别
    monkeypatch.setattr(at, "OCR_RULES", at.OCR_RULES + 1)  # 识别规则改了：按新规则再来
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
    segment.text, segment.loc = at.OCR_MARKER_ALL, {"page": 1, "ocr": 1}
    session.commit()
    # 都认出了：顶上不另写（文字开头那一行标明是识别出来的），段落出处只给页码
    data = client.get(path(doc, att)).json()
    assert (data["status"], data["scannedPages"]) == ("ok", 0)
    assert data["segments"][0] == {**data["segments"][0], "text": at.OCR_MARKER_ALL}
    assert data["segments"][0]["loc"] == {"page": 1}
    # 还有没认出的页：顶上写缺几页（小程序只在 partial 时显示）
    version.ocr_pages, version.scanned_pages = 2, 1
    session.commit()
    data = client.get(path(doc, att)).json()
    assert (data["status"], data["scannedPages"]) == ("partial", 1)
    assert data["notice"] == "其中 1 页扫描件没认出文字，下面的文字不含这些页。"
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
    add_doc(
        session,
        1,
        APPLY,
        RECENT,
        [("旧的", BASE + "old.pdf", "pdf"), ("新的", BASE + "new.pdf", "pdf")],
    )
    report = at.TextReport()
    old = pdf_pages([FULL, FULL])
    new = pdf_pages([FULL])
    at.record(session, BASE + "old.pdf", "pdf", NOW, report, content=old)
    at.record(session, BASE + "new.pdf", "pdf", NOW, report, content=new)
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
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW, pages=1)
    assert fetcher.calls == [BASE + "new.pdf"]
    assert (report.documents, report.recognized, report.pages, report.ocr_pages) == (1, 1, 1, 1)
    assert report.deferred == 1
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert fetcher.calls == [BASE + "new.pdf", BASE + "old.pdf"]
    assert (report.documents, report.ocr_pages, report.deferred) == (1, 2, 0)
    assert copies.ocr_scans(session, fetcher=fetcher, now=NOW).documents == 0


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
    assert len(at.ocr_due(session)) == 2  # 都没记成识别过
    # 起不了识别的子进程（和当时的环境有关）：这一份记出错，不记识别过，下次再来
    fake_ocr(monkeypatch, {}, error=pdf_sandbox.Failed("spawn", "broken", "起不了", "起不了"))
    fetcher = FakeFetcher({BASE + "new.pdf": new, BASE + "old.pdf": old})
    report = copies.ocr_scans(session, fetcher=fetcher, now=NOW)
    assert (report.errors, report.documents) == (2, 0)
    assert len(at.ocr_due(session)) == 2


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
        "errors=0 deferred=0"
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
    assert texts(result)[0] == at.OCR_MARKER_ALL
    assert "applicationform" in joined and "2026-12-15" in joined
