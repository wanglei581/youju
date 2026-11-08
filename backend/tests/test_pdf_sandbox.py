"""读 PDF 的子进程（pdf_sandbox）：限内存、限时、结果只传 JSON、环境变量只带 PATH 和 LANG；四处读 PDF 的
地方（附件副本的检查、附件文字、名单解析、PDF 正文）在子进程没读成时各记成什么。

解压以外的内存炸弹（字体 ToUnicode 里的 bfrange、层层嵌套的方括号）只在 Linux 上量得到上限：macOS
设不了 RLIMIT_AS（setrlimit 报错，分配照样成功），这几个测试在 macOS 上跳过，CI（Linux）上照常跑。
样本都是内存里现做的几百字节到几十 KB 的 PDF，不联网、不用数据库。"""

import json
import resource
import signal
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from youju import attachment_text, pdf_limits, pdf_sandbox
from youju import attachments as copies
from youju.crawler import pdf as body_pdf
from youju.crawler.pdf import extract_pdf_text
from youju.lists import parser
from youju.lists.parser import parse_attachment

MB = 1024 * 1024
TEXT = b"BT /F1 12 Tf 20 100 Td (Notice 2026) Tj ET"
linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason="macOS 设不了 RLIMIT_AS（setrlimit 报错，分配照样成功），内存上限只能在 Linux（CI、线上）上量",
)


def make_pdf(
    content: bytes,
    *,
    font: bytes = b"",
    extra: tuple[bytes, ...] = (),
    compress: bool = True,
    size: bytes = b"300 200",
) -> bytes:
    """一页的 PDF：页面内容是 content（默认按 FlateDecode 压），字体是 Helvetica（font 是字体字典里
    再加的项），extra 是接在后面的对象（从 6 号起）。"""
    raw = zlib.compress(content, 9) if compress else content
    flate = b" /Filter /FlateDecode" if compress else b""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s] /Contents 4 0 R" % size
        + b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d%s >>\nstream\n" % (len(raw), flate) + raw + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica" + font + b" >>",
        *extra,
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def table_pdf(rows: list[list[str]]) -> bytes:
    """一页一张带框线的表格（每格 100×20），格子里是 ASCII 文字。"""
    ops = [b"0.5 w"]
    for i, row in enumerate(rows):
        for j, text in enumerate(row):
            x, y = 20 + 100 * j, 360 - 20 * (i + 1)
            ops.append(b"%d %d 100 20 re S" % (x, y))
            ops.append(b"BT /F1 9 Tf %d %d Td (%s) Tj ET" % (x + 4, y + 6, text.encode()))
    return make_pdf(TEXT + b"\n" + b"\n".join(ops), size=b"500 400")


def bfrange_bomb() -> bytes:
    """11/5 独立审阅的样子：字体的 ToUnicode 里一行 beginbfrange <00000000> <FFFFFFFF>，pdfminer
    照着建一个 2^32 项的字典。不到 1KB，不压缩，解压上限管不到。"""
    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /B def"
        b" 1 begincodespacerange <00> <FF> endcodespacerange 1 beginbfrange"
        b" <00000000> <FFFFFFFF> <0000> endbfrange endcmap end end"
    )
    stream = b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream"
    return make_pdf(TEXT, font=b" /ToUnicode 6 0 R", extra=(stream,), compress=False)


def bracket_bomb(mb: int = 30) -> bytes:
    """页面内容里 30MB 层层嵌套的方括号（压完约 30KB，没超解压上限）：pdfminer 每一层留一个列表。"""
    return make_pdf(TEXT + b"\n" + b"[" * (mb * MB))


def busy_pdf(mb: int = 8) -> bytes:
    """页面内容是几 MB 的"q Q"（保存、恢复画图状态）：内存不涨，要算十几秒以上。"""
    return make_pdf(TEXT + b"\n" + b"q Q " * (mb * MB // 4))


def results(pdf: bytes) -> dict:
    """四处读 PDF 的结果：附件副本的检查、附件文字、名单解析（失败时的原因）、PDF 正文。"""
    check = copies.check_content(pdf, "pdf")
    text = attachment_text.extract(pdf)
    try:
        listed = parse_attachment(pdf, "pdf")
    except ValueError as exc:
        listed = str(exc)
    except pdf_sandbox.Failed as exc:
        listed = f"解析失败：{exc.error}"
    return {
        "copy": check if isinstance(check, str) else check.code,
        "text": (text.reason, text.detail),
        "list": listed,
        "body": extract_pdf_text(pdf),
    }


def failed(detail: str, text_reason: str = "too_large") -> dict:
    """子进程没读成（不是拒读、坏文件）时四处的结果：副本按取不出文字、名单按暂不支持（不再重试）。"""
    return {
        "copy": "unchecked",
        "text": (text_reason, f"PDF {detail}"),
        "list": f"暂不支持这份 PDF（{detail}）",
        "body": ("", f"PDF {detail}"),
    }


def in_process(monkeypatch) -> None:
    monkeypatch.setattr(
        pdf_sandbox,
        "_spawn",
        lambda cfg, data, seconds: json.loads(
            json.dumps(pdf_sandbox._work(cfg["task"], data, cfg["options"]))
        ),
    )


# ---- 读成的：和在本进程里读一样 ----


def test_four_readers_read_through_the_child_like_in_process(monkeypatch) -> None:
    samples = [
        make_pdf(TEXT),
        table_pdf([["Name", "Code"], ["Alpha Co", "001"], ["Beta Co", "002"]]),
        b"%PDF-1.4\nnot really a pdf",
    ]
    child = [results(pdf) for pdf in samples]
    in_process(monkeypatch)
    assert child == [results(pdf) for pdf in samples]
    assert child[0] == {
        "copy": "pdf",
        "text": (None, None),
        "list": [],
        "body": ("Notice 2026", None),
    }
    # 打不开的：和以前一样记损坏、无法解析；名单记子进程里那个异常的类名（6 小时后再试）
    assert child[2]["copy"] == "unchecked"
    assert child[2]["text"] == ("broken", "PDF 打不开（损坏或加密）")
    assert child[2]["list"].startswith("解析失败：")
    assert child[2]["body"] == ("", "PDF 无法解析")


def test_decompression_limits_are_passed_to_the_child(monkeypatch) -> None:
    """pdf_limits 的解压上限在子进程里照样用，用的是父进程当时的值（这里调小）。"""
    monkeypatch.setattr(pdf_limits, "STREAM_MAX", 1 * MB)
    monkeypatch.setattr(pdf_limits, "DOCUMENT_MAX", 3 * MB)
    bomb = make_pdf(TEXT + b"\n" + b" " * (8 * MB))
    assert len(bomb) < 16 * 1024
    assert results(bomb) == {
        "copy": "unchecked",
        "text": ("too_large", "PDF 数据流解压后超过 1MB"),
        "list": "暂不支持这份 PDF（数据流解压后超过 1MB）",
        "body": ("", "PDF 数据流解压后超过 1MB"),
    }


def test_table_budget_options_reach_the_child(monkeypatch) -> None:
    """名单的表格预算（parser._MAX_CELLS 等）按调用时的值传进子进程，停在超了的那一行，报错和以前一样。"""
    pdf = table_pdf([["Name", "Code"], ["Alpha Co", "001"], ["Beta Co", "002"]])
    monkeypatch.setattr(parser, "_MAX_CELLS", 3)
    with pytest.raises(ValueError, match="安全解析预算的表格（行列或单元格过多）"):
        parse_attachment(pdf, "pdf")


# ---- 解压以外的炸弹：内存（Linux） ----


@linux_only
@pytest.mark.parametrize("bomb", [bfrange_bomb, bracket_bomb], ids=["bfrange", "brackets"])
def test_memory_bombs_stop_at_the_memory_limit(monkeypatch, bomb) -> None:
    """上限调到 320MB（真实上限下也一样，只是慢一点）：几秒内停下，四处都按"太大"记，父进程的内存
    不跟着涨。同样的上限下普通文件照常读（证明不是一启动就超了）。"""
    monkeypatch.setattr(pdf_sandbox, "MEMORY_MAX", 320 * MB)
    pdf = bomb()
    assert len(pdf) < 64 * 1024
    assert results(make_pdf(TEXT))["body"] == ("Notice 2026", None)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.monotonic()
    assert results(pdf) == failed("解析用的内存超过 320MB")
    assert time.monotonic() - started < 120
    grown_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    assert grown_kb < 100 * 1024


def test_memory_error_swallowed_by_a_parser_still_stops_the_child(monkeypatch) -> None:
    """pdfminer 里的 except Exception 会吞掉 MemoryError、接着读出缺字的结果：子进程里一抛出
    MemoryError 就退出（这一条不靠 RLIMIT_AS，哪里都能测）。"""
    boot = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from youju import pdf_sandbox\n"
        "pdf_sandbox._exit_on_memory_error()\n"
        "try:\n    raise MemoryError\nexcept Exception:\n    pass\n"
        "print('swallowed')\n"
    )
    monkeypatch.setattr(pdf_sandbox, "_BOOT", boot)
    with pytest.raises(pdf_sandbox.Failed) as raised:
        pdf_sandbox.read("body", make_pdf(TEXT), seconds=60, pages=200)
    assert raised.value.kind == "memory"
    assert raised.value.detail == f"解析用的内存超过 {pdf_sandbox.MEMORY_MAX // MB}MB"


# ---- 时间 ----


def test_time_limit_stops_a_busy_pdf_in_all_four_readers(monkeypatch) -> None:
    monkeypatch.setattr(copies, "PDF_SECONDS", 2)
    monkeypatch.setattr(parser, "PDF_SECONDS", 2)
    monkeypatch.setattr(body_pdf, "PDF_SECONDS", 2)
    monkeypatch.setattr(attachment_text, "EXTRACT_SECONDS", 2)
    pdf = busy_pdf()
    assert len(pdf) < 64 * 1024
    started = time.monotonic()
    got = results(pdf)
    assert got == {**failed("解析超过 2 秒"), "text": ("too_large", "取文字超过 2 秒")}
    assert time.monotonic() - started < 40


def test_time_limit_works_outside_the_main_thread(monkeypatch) -> None:
    """补详情在线程池里读 PDF 正文：SIGALRM 只能在主线程，子进程照样到时停掉。"""
    monkeypatch.setattr(body_pdf, "PDF_SECONDS", 2)
    started = time.monotonic()
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(extract_pdf_text, busy_pdf()).result() == ("", "PDF 解析超过 2 秒")
    assert time.monotonic() - started < 30


def test_caller_alarm_kills_the_child(monkeypatch) -> None:
    """附件文字自己的限时（SIGALRM）先到：打断等待的同时子进程也停掉、收回，不留在后台接着算。"""
    spawned = []
    popen = subprocess.Popen

    def record(*args, **kwargs):
        spawned.append(popen(*args, **kwargs))
        return spawned[-1]

    monkeypatch.setattr(subprocess, "Popen", record)
    with (
        pytest.raises(attachment_text.Unreadable, match="取文字超过 1 秒"),
        attachment_text._time_limit(1),
    ):
        pdf_sandbox.read("body", busy_pdf(), seconds=60, pages=200)
    assert len(spawned) == 1
    assert spawned[0].poll() is not None


# ---- 子进程崩了、写回的不对 ----


CRASHES = {
    # 被信号停掉（先关掉内存转储）
    "segfault": (
        "import os, resource, signal\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        "os.kill(os.getpid(), signal.SIGSEGV)\n",
        ("crash", "解析时子进程异常退出：SIGSEGV"),
    ),
    # 不是父进程杀的 SIGKILL：多半是容器内存不够，系统停掉的
    "killed": (
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)\n",
        ("memory", "解析时内存不够，被系统停掉"),
    ),
    "uncaught": ("raise KeyError('某某')\n", ("crash", "解析时子进程异常退出：退出码 1，KeyError")),
    "garbage": (
        "import sys; sys.stdout.buffer.write(b'xx')\n",
        ("crash", "解析时子进程写回的结果不对"),
    ),
    "unknown answer": (
        "import json, sys\nraw = json.dumps({'other': 1}).encode()\n"
        "sys.stdout.buffer.write(len(raw).to_bytes(8, 'big') + raw)\n",
        ("crash", "解析时子进程写回的结果不对"),
    ),
    "memory exit": ("import os; os._exit(77)\n", ("memory", "解析用的内存超过 1024MB")),
    "output exit": ("import os; os._exit(78)\n", ("output", "读出的内容超过 64MB")),
}


@pytest.mark.parametrize("name", CRASHES)
def test_crashed_or_garbled_child_is_a_failure(monkeypatch, name) -> None:
    boot, (kind, detail) = CRASHES[name]
    monkeypatch.setattr(pdf_sandbox, "_BOOT", boot)
    monkeypatch.setattr(pdf_sandbox, "MEMORY_MAX", 1024 * MB)
    monkeypatch.setattr(pdf_sandbox, "OUTPUT_MAX", 64 * MB)
    with pytest.raises(pdf_sandbox.Failed) as raised:
        pdf_sandbox.read("body", make_pdf(TEXT), seconds=60, pages=200)
    assert (raised.value.kind, raised.value.detail) == (kind, detail)


def test_crash_in_all_four_readers(monkeypatch) -> None:
    monkeypatch.setattr(pdf_sandbox, "_BOOT", CRASHES["segfault"][0])
    detail = "解析时子进程异常退出：SIGSEGV"
    assert results(make_pdf(TEXT)) == failed(detail, text_reason="broken")


def test_output_limit(monkeypatch) -> None:
    """写回的结果超了 OUTPUT_MAX：子进程写不进去（RLIMIT_FSIZE），四处按"太大"记。"""
    words = b" ".join(b"(Line %d of a long notice) Tj T*" % i for i in range(150))
    pdf = make_pdf(b"BT /F1 6 Tf 8 TL 20 1290 Td " + words + b" ET", size=b"300 1300")
    assert 2048 < len(extract_pdf_text(pdf)[0]) < 8192
    monkeypatch.setattr(pdf_sandbox, "OUTPUT_MAX", 2048)
    assert results(pdf) == failed("读出的内容超过 2KB")


# ---- 环境、并发 ----


def test_child_gets_only_path_and_lang_and_runs_isolated(monkeypatch) -> None:
    monkeypatch.setenv("YOUJU_DATABASE_URL", "mysql+pymysql://secret@db/youju")
    monkeypatch.setenv("PYTHONPATH", "/tmp/planted")
    boot = (
        "import json, os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import youju\n"
        "answer = {'ok': {'env': sorted(os.environ), 'isolated': sys.flags.isolated,\n"
        "  'youju': youju.__file__, 'planted': '/tmp/planted' in sys.path}}\n"
        "raw = json.dumps(answer).encode()\n"
        "sys.stdout.buffer.write(len(raw).to_bytes(8, 'big') + raw)\n"
    )
    monkeypatch.setattr(pdf_sandbox, "_BOOT", boot)
    got = pdf_sandbox.read("body", make_pdf(TEXT), seconds=60, pages=200)
    # macOS 的系统库可能自己加上 __CF_USER_TEXT_ENCODING
    assert set(got["env"]) - {"__CF_USER_TEXT_ENCODING"} == {"PATH", "LANG"}
    assert got["isolated"] == 1
    assert not got["planted"]
    assert got["youju"].startswith(str(pdf_sandbox._ROOT))


def test_one_child_at_a_time_per_process(monkeypatch) -> None:
    running, most = [0], [0]
    lock = threading.Lock()

    def slow(cfg, data, seconds):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.2)
        with lock:
            running[0] -= 1
        return {"ok": {"pages": 1, "text": "x"}}

    monkeypatch.setattr(pdf_sandbox, "_spawn", slow)
    with ThreadPoolExecutor(4) as pool:
        texts = list(pool.map(extract_pdf_text, [make_pdf(TEXT)] * 4))
    assert texts == [("x", None)] * 4
    assert most[0] == pdf_sandbox.PARALLEL == 1


def test_children_limit_themselves() -> None:
    """子进程自己设的上限（Linux 上全都管用；macOS 设不了 RLIMIT_AS）。"""
    cfg = {"memory": 512 * MB, "output": 4 * MB, "cpu": 70}
    code = (
        "import json, resource, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from youju import pdf_sandbox\n"
        "pdf_sandbox._limit(json.loads(sys.argv[2]))\n"
        "names = ['RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_CORE']\n"
        "got = {n: resource.getrlimit(getattr(resource, n)) for n in names}\n"
        "if sys.platform == 'linux':\n"
        "    got['oom'] = open('/proc/self/oom_score_adj').read().strip()\n"
        "print(json.dumps(got))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(pdf_sandbox._ROOT), json.dumps(cfg)],
        capture_output=True,
        check=True,
        text=True,
    )
    limits = json.loads(out.stdout)
    assert limits["RLIMIT_CPU"] == [70, 75]
    assert limits["RLIMIT_FSIZE"] == [4 * MB + 8] * 2
    assert limits["RLIMIT_CORE"] == [0, 0]
    if sys.platform == "linux":
        assert limits["RLIMIT_AS"] == [512 * MB] * 2
        assert limits["oom"] == "1000"  # 容器内存不够时先停读 PDF 的子进程


def test_signal_names_for_unknown_codes() -> None:
    assert pdf_sandbox._died(-signal.SIGTERM, None) == "SIGTERM"
