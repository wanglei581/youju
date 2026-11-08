"""在限内存、限时的子进程里读 PDF（附件副本的检查、附件文字、名单解析、PDF 正文四处共用）。

为什么（PR #452 的后续）：pdf_limits 只管"解压"这一层，解出来的内容在上限以内、却让 pdfminer
生成海量对象或算很久的，它管不到。11/5 独立审阅实测：856 字节的 PDF，字体 ToUnicode 里一行
beginbfrange <00000000> <FFFFFFFF> 就让 pdfminer 建一个 2^32 项的字典（每项约 130 字节）；几十 MB
层层嵌套的方括号也一样。附件副本的检查、名单解析、PDF 正文三处读 PDF 也没有总限时（附件文字的
_time_limit 用 SIGALRM，只在主线程里管用；补详情读 PDF 正文是在线程池里）。所以读 PDF 整个放进
子进程，四处只拿回读出的文字、表格这些（JSON），自己再判断：

- 内存：子进程的虚拟内存（RLIMIT_AS）不超过 MEMORY_MAX。超了 Python 抛 MemoryError，pdfminer、
  pdfplumber 里不少 except Exception 会把它吞掉、接着读，读出缺字的结果；所以子进程里一出现
  MemoryError 就立刻退出（sys.monitoring 看着每个抛出的异常），整份按内存超了算。只在 Linux 上
  管用（CI、线上）：本机 macOS 设不了 RLIMIT_AS（setrlimit 报错，分配照样成功）。另外把子进程的
  oom_score_adj 调到最大：整个容器内存不够时，系统先停读 PDF 的子进程，不先停定时任务、核对台；
- 时间：按子进程自己用的 CPU 时间算（RLIMIT_CPU，seconds 秒）：容器忙的时候排队等 CPU 的时间
  不算，同一份文件在哪种负载下结果都一样，哪个线程里调用都管用（SIGALRM 只能在主线程）。父进程
  另外最多等 _wall(seconds) 秒（墙上时间，兜底：子进程卡住不用 CPU、或者容器忙得一直轮不上）；
- 结果：子进程写进一个临时文件（RLIMIT_FSIZE 限住大小，写不进去就退出），父进程看过大小再读。
  只传 JSON（字符串、数字、列表、字典），不用 pickle：子进程读的是不可信的文件，父进程不执行它
  给回来的任何东西；
- 环境：子进程只带 PATH、LANG（数据库地址、密钥一概不带），用 -I 启动（不读 PYTHON* 环境变量、
  不把当前目录加进 sys.path），代码从父进程同一份 youju 读。这里管的是资源和环境变量，不是安全
  沙箱：子进程照样能读本用户能读的文件、能联网（pdfminer 是纯 Python，读 PDF 不执行文件里的代码）；
- 一个进程里同时最多 PARALLEL 个子进程（补详情 4 个线程一起跑；容器只有 1 核，多开也不会更快，
  内存却要按个数翻倍）。限时从拿到名额以后算：排队等别的文件不算这一份的时间，免得好好的文件
  因为前面有慢的而被判超时；
- pdf_limits 的解压上限在子进程里照样用：父进程把当时的上限传过去。

没读成分两种（Failed.retry）：文件本身的（拒读、打不开、出错、内存或 CPU 时间超了、读出的太多），
同一份文件再读还是一样，四处照原来的办法记、不反复重读；和当时的环境有关的（起不了子进程、
设不了资源上限、容器内存不够被系统停掉、墙上时间兜底到了、子进程崩了），过一段时间再读。

读 PDF 的那一段（打开、翻页、取哪些字段、什么时候停）都在这里，按 TASKS 分开，写法照搬原来
四处的代码；拿回去以后怎么判断（扫描件、太大、表格里哪列是名称）还在原来的模块里。原来抛异常
结束的（图片太大、页数超了、名单是扫描件、表格超了预算），子进程里也是抛 _Stop 结束，和
pdf_limits.reading() 的配合不变（正常读完时，被 pdfplumber 吞掉的拒读照样让整份按拒读算）。

上限怎么定（2026-11-08，按 11/4 公开数据快照里的附件、名单 PDF 链接重新下载 341 份真实文件，在 Linux
虚拟机里把四处的读法各跑一遍，量子进程的 VmPeak，也就是 RLIMIT_AS 管的地址空间）：最大 276MB、
99% 在 107MB 以内，刚导入完约 50MB；MEMORY_MAX 取 1GB（pdf_limits 允许一份解出 256MB，再加上解析
用的对象；容器 2GB）。写回的结果最大 2.6MB，OUTPUT_MAX 取 32MB。各处的限时见调用的地方。
开销：每份多起一个 Python 进程、导入 pdfplumber，Linux 上约 0.2 秒。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from itertools import islice
from pathlib import Path

log = logging.getLogger("youju.pdf_sandbox")
_MB = 1024 * 1024
# 子进程的虚拟内存上限（RLIMIT_AS，含 Python 自己和各个库占的地址空间）。真实文件最大 276MB
MEMORY_MAX = 1024 * _MB
# 识别扫描页（ocr，D-125）的子进程要装下 RapidOCR、onnxruntime、OpenCV 和渲染出的页面图片，
# 上限另给。实测（11/8，单线程，A4 200dpi、每页 30 行）：Linux（CI 的 x86 机器）识别 3 页地址
# 空间峰值（VmPeak，RLIMIT_AS 管的）约 1.0GB、实际占用约 0.77GB；本机 macOS 实际占用头两页涨到
# 约 1.2GB，到第 8 页不再涨（不是每页都涨）。每批最多 10 页（attachment_text._OCR_BATCH），
# 一批一个子进程，识别完退出、内存还回去。一次只跑一个识别子进程；本地电脑 16G（WSL 默认分到
# 一半），2G 的上限加上抓取、数据库都够
OCR_MEMORY_MAX = 2048 * _MB
# 子进程写回的结果最多这么大（RLIMIT_FSIZE）。真实文件最大 2.6MB
OUTPUT_MAX = 32 * _MB
# 一个进程里同时读 PDF 的子进程个数
PARALLEL = 1
# 子进程出了 MemoryError、结果写不下、设不了资源上限时的退出码
_EXIT_MEMORY = 77
_EXIT_OUTPUT = 78
_EXIT_LIMITS = 79
_EXIT_WRITE = 80
# 和当时的环境有关、过一段时间再读的几种（见模块说明）
_RETRY = {"spawn", "limits", "killed", "busy", "crash", "io"}
_SLOTS = threading.BoundedSemaphore(PARALLEL)
# youju 包所在的目录：子进程用 -I 启动不读 PYTHONPATH，从这里导入和父进程同一份代码
_ROOT = Path(__file__).resolve().parents[1]
_BOOT = (
    "import sys; sys.path.insert(0, sys.argv[1]); from youju.pdf_sandbox import _child; _child()"
)


class Failed(Exception):
    """没读成。kind 说明是哪一种：

    - refused：pdf_limits 拒读（reason 是它给的 too_large、format、broken）；
    - open：打不开（损坏、加密），error 是 pdfplumber 抛的异常类名；
    - error：读的时候出错，error 同上；
    - memory、time、output：内存、CPU 时间、读出的结果超了（reason 都是 too_large）；
    - spawn、limits、killed、busy、crash、io：起不了子进程、设不了资源上限（或者平台给的上限比
      设定的低，超了不能算文件的）、被系统停掉（多半是容器内存不够）、墙上时间兜底到了、子进程崩了
      或写回的结果不对、读写结果的临时文件出错。和当时的环境有关，retry 为真，error 是一句短的
      说明（名单记"解析失败：……"，过 6 小时再试）。

    detail 写具体原因，不带"PDF"两个字（调用方自己接）。"""

    def __init__(self, kind: str, reason: str, detail: str, error: str | None = None) -> None:
        super().__init__(detail)
        self.kind = kind
        self.reason = reason
        self.detail = detail
        self.error = error
        self.retry = kind in _RETRY


def read(task: str, data: bytes, *, seconds: float, memory: int | None = None, **options) -> dict:
    """在子进程里按 task（TASKS 里的一种）读这份 PDF，返回读出的东西（见各个 task 的说明）；
    没读成抛 Failed。options 原样传给 task。memory：虚拟内存上限，不给是 MEMORY_MAX。"""
    from youju import pdf_limits

    cfg = {
        "task": task,
        "options": options,
        "stream_max": pdf_limits.STREAM_MAX,
        "document_max": pdf_limits.DOCUMENT_MAX,
        "memory": memory or MEMORY_MAX,
        "output": OUTPUT_MAX,
        "cpu": max(1, math.ceil(seconds)),
    }
    try:
        with _SLOTS:  # 限时从拿到名额以后算（见模块说明）
            return _answer(_spawn(cfg, data, seconds))
    except Failed as exc:
        if exc.retry:  # 和环境有关的要有人看到（连着出现多半是容器或部署出了问题）
            log.warning("读 PDF 的子进程没读成（%s）：%s", exc.kind, exc.detail)
        elif exc.kind not in ("open", "error"):  # 普通的坏文件不记，超了、拒读的记一条
            log.info("PDF 没读成（%s）：%s", exc.kind, exc.detail)
        raise


def _answer(answer: dict) -> dict:
    """子进程的回答 → 读出的东西；没读成抛 Failed。"""
    if "ok" in answer:
        return answer["ok"]
    if "refused" in answer:
        reason, detail = answer["refused"]
        raise Failed("refused", reason, detail)
    if "open" in answer:
        raise Failed("open", "broken", "打不开（损坏或加密）", answer["open"])
    raise Failed("error", "broken", f"解析出错（{answer['error']}）", answer["error"])


def _wall(seconds: float) -> float:
    """父进程最多等多少秒（墙上时间，兜底）：CPU 时间的三倍再多半分钟。"""
    return seconds * 3 + 30


def _spawn(cfg: dict, data: bytes, seconds: float) -> dict:
    """起子进程读，等它读完，返回它写回的 JSON；超时、超内存、崩了抛 Failed。
    输入、输出、错误输出都是临时文件（不用管道：不会卡住，输出大小也好限）；临时目录写满、
    文件句柄用完这类，和起不了子进程一样过一段时间再读。"""
    with contextlib.ExitStack() as files:
        try:
            given, out, err = (files.enter_context(tempfile.TemporaryFile()) for _ in range(3))
            given.write(data)
            given.flush()
            given.seek(0)
        except OSError as exc:
            detail = "起不了解析的子进程（临时文件）"
            raise Failed("spawn", "broken", detail, "起不了子进程") from exc
        child = None
        try:
            # 起子进程的时候不让调用方的 SIGALRM 打断（打断了可能留下没人管的子进程）；
            # 挡住的信号在这个 try 里面放出来，下面的 finally 照样停掉子进程
            with _alarm_held():
                child = subprocess.Popen(
                    [sys.executable, "-I", "-c", _BOOT, str(_ROOT), json.dumps(cfg)],
                    stdin=given,
                    stdout=out,
                    stderr=err,
                    env=_env(cfg),
                )
            code = child.wait(timeout=_wall(seconds))
            return _output(code, cfg, seconds, out, err, _reduced(cfg))
        except OSError as exc:  # 起子进程、读结果的临时文件出错
            if child is not None:
                raise Failed("io", "broken", "读解析结果时出错", "读结果出错") from exc
            raise Failed("spawn", "broken", "起不了解析的子进程", "起不了子进程") from exc
        except subprocess.TimeoutExpired:
            detail = f"解析等了 {_wall(seconds):g} 秒还没完（系统繁忙）"
            raise Failed("busy", "too_large", detail, "系统繁忙") from None
        finally:
            # 超时、调用方自己的超时（SIGALRM）打断等待时，子进程一定要停掉、收回
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()


@contextlib.contextmanager
def _alarm_held():
    if threading.current_thread() is not threading.main_thread():
        yield  # 别的线程收不到 SIGALRM 的处理（Python 只在主线程里调信号处理函数）
        return
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def _env(cfg: dict | None = None) -> dict[str, str]:
    """子进程的环境变量：只有 PATH、LANG，数据库地址、密钥这些一概不带。识别扫描页（ocr）另带
    几个线程数：numpy（OpenBLAS）、onnxruntime 默认按 CPU 核数起线程，每个线程都占一块地址空间，
    核多的机器上会顶到内存上限（RLIMIT_AS）；malloc 的分配区也限成 2 个。"""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8"}
    if cfg is not None and cfg.get("task") == "ocr":
        threads = str(cfg["options"].get("threads", 1))
        env.update(
            OMP_NUM_THREADS=threads,
            OPENBLAS_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            MALLOC_ARENA_MAX="2",
        )
    return env


def _reduced(cfg: dict) -> dict[str, int]:
    """平台的硬上限让子进程实际拿到的比设定的少的几项：子进程从父进程继承硬上限，_limit 照它设
    （照样有上限），这时超了不能算文件的（换个地方就读得了）。算法和 _limit 一样，返回
    {"memory": 实际的内存上限, "cpu": 实际的 CPU 时间软上限}（没少的不写）。"""
    import resource

    reduced = {}
    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
    if hard != resource.RLIM_INFINITY and hard < cfg["memory"]:
        reduced["memory"] = hard
    hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
    if hard != resource.RLIM_INFINITY and hard < cfg["cpu"] + 5:
        soft = min(cfg["cpu"], max(hard - 1, 1))  # 和 _limit 一样：软上限比压低的硬上限少 1 秒
        if soft < cfg["cpu"]:
            reduced["cpu"] = soft
    return reduced


def _output(code: int, cfg: dict, seconds: float, out, err, reduced: dict[str, int]) -> dict:
    # 到了内存、CPU 时间的上限（SIGKILL 不算：CPU 软上限总比硬上限低，先收到的是 SIGXCPU）
    if code in (_EXIT_MEMORY, -signal.SIGXCPU) and reduced:
        kind = "memory" if code == _EXIT_MEMORY else "cpu"
        if kind in reduced:
            got = _size(reduced[kind]) if kind == "memory" else f"{reduced[kind]} 秒"
            detail = (
                f"平台给的{'内存' if kind == 'memory' else ' CPU 时间'}上限只有 {got}，比设定的低"
            )
            raise Failed("limits", "broken", detail, "平台资源上限太低")
    if code == _EXIT_MEMORY:
        raise Failed("memory", "too_large", f"解析用的内存超过 {_size(cfg['memory'])}")
    if code == -signal.SIGXCPU:
        raise Failed("time", "too_large", f"解析超过 {seconds:g} 秒")
    if code == _EXIT_LIMITS:
        raise Failed("limits", "broken", "解析的子进程设不了资源上限", "设不了资源上限")
    if code == -signal.SIGKILL:  # 不是父进程杀的（超时在前面）：多半是容器内存不够，系统停掉的
        detail = "解析的子进程被系统停掉（可能是容器内存不够）"
        raise Failed("killed", "too_large", detail, "被系统停掉")
    if code in (_EXIT_WRITE, -signal.SIGXFSZ):  # 临时盘写满、读写出错、平台的文件大小上限更低
        raise Failed("io", "broken", "解析的子进程写不出结果", "子进程写不出结果")
    size = os.fstat(out.fileno()).st_size
    if code == _EXIT_OUTPUT or size > cfg["output"] + 8:
        raise Failed("output", "too_large", f"读出的内容超过 {_size(cfg['output'])}")
    if code != 0:
        detail = f"解析时子进程异常退出：{_died(code, err)}"
        raise Failed("crash", "broken", detail, "子进程异常退出")
    out.seek(0)
    head = out.read(8)
    answer = None
    if len(head) == 8 and int.from_bytes(head, "big") == size - 8:
        try:
            answer = json.loads(out.read().decode("utf-8", "surrogatepass"))
        except (ValueError, RecursionError):
            answer = None
    if not isinstance(answer, dict) or len(answer) != 1 or not answer.keys() & _ANSWERS:
        raise Failed("crash", "broken", "解析时子进程写回的结果不对", "子进程写回的结果不对")
    return answer


_ANSWERS = {"ok", "refused", "open", "error"}


def _size(n: int) -> str:
    return f"{n // _MB}MB" if n >= _MB else f"{n // 1024}KB"


def _died(code: int, err) -> str:
    """子进程怎么退出的：信号名，或者退出码和最后一行错误输出里的异常类名（不带异常说明，
    说明里可能有文件里的字）。"""
    if code < 0:
        try:
            return signal.Signals(-code).name
        except ValueError:
            return f"信号 {-code}"
    err.seek(max(0, os.fstat(err.fileno()).st_size - 4000))
    lines = err.read().decode("utf-8", "replace").splitlines()
    # 从后往前找异常那一行（"KeyError: ..."）；前后可能还有底层库打印的别的话
    names = (m.group(1) for line in reversed(lines) if (m := _EXCEPTION_LINE.match(line)))
    name = next(names, "")
    return f"退出码 {code}" + (f"，{name}" if name else "")


_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b")


# ---- 子进程 ----


def _child() -> None:
    """子进程：先设上限，再读标准输入里的 PDF，结果按"8 字节长度 + JSON"写进标准输出（一个临时
    文件），写完直接退出（不做退出前的清理：大文件的对象多，清理要好几秒）。"""
    cfg = json.loads(sys.argv[2])
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # 库里随手 print 的进错误输出，不混进结果
    _limit(cfg)
    _exit_on_memory_error()
    logging.disable(logging.CRITICAL)
    import warnings

    warnings.simplefilter("ignore")
    try:
        from youju import pdf_limits

        pdf_limits.STREAM_MAX = cfg["stream_max"]
        pdf_limits.DOCUMENT_MAX = cfg["document_max"]
        answer = _work(cfg["task"], sys.stdin.buffer.read(), cfg["options"])
        raw = json.dumps(answer, ensure_ascii=False).encode("utf-8", "surrogatepass")
    except MemoryError:
        os._exit(_EXIT_MEMORY)
    if len(raw) > cfg["output"]:  # 读出的太多（文件本身的）
        os._exit(_EXIT_OUTPUT)
    try:
        out.write(len(raw).to_bytes(8, "big"))
        out.write(raw)
        out.flush()
    except OSError:  # 长度先比过了，这里是临时盘写满、读写出错（环境的）
        os._exit(_EXIT_WRITE)
    os._exit(0)


def _limit(cfg: dict) -> None:
    """设资源上限。平台给的硬上限比要的还低时就按平台的（照样有上限）；设不上的就退出（没有上限
    不读不可信的文件），只有 macOS 上的 RLIMIT_AS 例外（设不了，本机开发、测试照样读）。"""
    import resource

    for name, soft, hard in (
        ("RLIMIT_AS", cfg["memory"], cfg["memory"]),
        # 到了软上限收到 SIGXCPU（默认就退出），硬上限多给 5 秒再 SIGKILL
        ("RLIMIT_CPU", cfg["cpu"], cfg["cpu"] + 5),
        ("RLIMIT_FSIZE", cfg["output"] + 8, cfg["output"] + 8),
        # 崩了、超了 CPU 时间不写内存转储（几百 MB 写进容器的磁盘）
        ("RLIMIT_CORE", 0, 0),
    ):
        try:
            which = getattr(resource, name)
            limit = resource.getrlimit(which)[1]
            if limit != resource.RLIM_INFINITY and hard > limit:
                hard = limit
                # CPU 时间：软上限等于硬上限时到了直接 SIGKILL（分不清原因），软上限留低 1 秒先收到
                # SIGXCPU
                soft = min(soft, max(limit - 1, 1) if name == "RLIMIT_CPU" else limit)
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            if not (name == "RLIMIT_AS" and sys.platform != "linux"):
                os._exit(_EXIT_LIMITS)
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:  # 只有 Linux 有；调大不用特权
            fh.write("1000")
    except OSError:
        pass


def _exit_on_memory_error() -> None:
    """子进程里任何地方抛出 MemoryError（哪怕接着就被 except Exception 吞掉）都立刻退出。"""
    monitoring = sys.monitoring

    def raised(_code, _offset, exc) -> None:
        if isinstance(exc, MemoryError):
            os._exit(_EXIT_MEMORY)

    for tool in (3, 4, monitoring.PROFILER_ID):
        try:
            monitoring.use_tool_id(tool, "youju.pdf_sandbox")
        except ValueError:  # 这个编号有人用了
            continue
        monitoring.register_callback(tool, monitoring.events.RAISE, raised)
        monitoring.set_events(tool, monitoring.events.RAISE)
        return


class _Stop(Exception):  # 不是错误：原来的代码抛异常结束的结果
    """原来的代码抛异常结束的（图片太大、页数超了……）：result 是要写回的结果。在
    pdf_limits.reading() 里按异常结束，被吞掉的拒读不改它的结果（和原来一样）。"""

    def __init__(self, result: dict) -> None:
        super().__init__()
        self.result = result


class _OpenFailed(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _open(data: bytes):
    import pdfplumber

    return pdfplumber.open(io.BytesIO(data))


def _work(task: str, data: bytes, options: dict) -> dict:
    """读一份 PDF（在子进程里；测试也可以在本进程里调）：{"ok": 读出的}、{"refused": [原因, 说明]}、
    {"open": 异常类名}、{"error": 异常类名} 四种之一。"""
    from youju import pdf_limits

    try:
        with pdf_limits.reading():
            try:
                pdf = _open(data)
            except Exception as exc:  # noqa: BLE001 - 损坏、加密的文件会抛多种异常
                raise _OpenFailed(exc.__class__.__name__) from exc
            with pdf:
                return {"ok": TASKS[task](pdf, options)}
    except _Stop as stop:
        return {"ok": stop.result}
    except Exception as exc:  # noqa: BLE001 - 第三方解析器对坏文件会抛多种异常
        if isinstance(exc, MemoryError):
            raise
        hit = pdf_limits.refused(exc)
        if hit is not None:
            return {"refused": [hit.reason, hit.detail]}
        if isinstance(exc, _OpenFailed):
            return {"open": exc.error}
        return {"error": exc.__class__.__name__}


# ---- 四处读 PDF 的那一段（写法照搬原来的代码） ----


def _image_areas(page, images: list[dict]) -> list[float]:
    """这一页上每张图片占的面积（超出页面的部分不算）。"""
    x0, top, x1, bottom = page.bbox
    areas = []
    for image in images:
        width = min(image["x1"], x1) - max(image["x0"], x0)
        height = min(image["bottom"], bottom) - max(image["top"], top)
        areas.append(width * height if width > 0 and height > 0 else 0.0)
    return areas


def _copy(pdf, opts: dict) -> dict:
    """附件副本的检查（attachments._pdf_text）：{"text": 各页文字}；超过 pages 页、某一页几乎
    没字却有图片（多半是扫描页）的 {"text": None}；某一页的图片超过 image_share、合计超过
    page_share 的停在这一页：{"images": [页码, "one" 或 "sum"]}。"""
    if len(pdf.pages) > opts["pages"]:
        return {"text": None}
    texts = []
    for number, page in enumerate(pdf.pages, 1):
        text = page.extract_text() or ""
        images = page.images
        scanned = len(text.strip()) < 10 and bool(images)
        areas = _image_areas(page, images) if images else []
        page_area = page.width * page.height
        page.close()  # 释放这一页解析出的对象，大文件不至于占满内存
        if scanned:
            return {"text": None}
        if any(area > page_area * opts["image_share"] for area in areas):
            raise _Stop({"images": [number, "one"]})
        if sum(areas) > page_area * opts["page_share"]:
            raise _Stop({"images": [number, "sum"]})
        texts.append(text)
    return {"text": "\n".join(texts)}


def _text(pdf, opts: dict) -> dict:
    """附件文字（attachment_text._pdf）：{"pages": 总页数, "read": [每页]}，每页见 _text_page
    （认扫描页、找表格用的几个数 scan_chars 等由 attachment_text 传过来，和它认 OFD 扫描页
    用同一套）。超过 pages 页的：first_pages 为真时只读前 pages 页，不然不读
    （只有总页数）。"""
    pages = len(pdf.pages)
    if pages > opts["pages"] and not opts["first_pages"]:
        raise _Stop({"pages": pages})
    read = []
    for page in pdf.pages[: opts["pages"]]:
        try:
            read.append(_text_page(page, opts))
        finally:
            page.close()  # 释放这一页解析出的对象
    return {"pages": pages, "read": read}


_OCR_FONT = re.compile(r"OCR|GlyphLess", re.I)
_INVISIBLE = re.compile(rb"(?<![\d.])3\s+Tr\b")


def _ocr_layer(page) -> bool:
    """这页的字是不是扫描件上看不见的识别层：字体名带 OCR、GlyphLess（Acrobat、Tesseract
    的识别层），或者页面内容把文字设成不可见（3 Tr，ABBYY 等识别软件这样写）。"""
    if any(_OCR_FONT.search(c.get("fontname") or "") for c in page.chars):
        return True
    from pdfminer.pdftypes import resolve1

    try:
        return any(
            _INVISIBLE.search(resolve1(stream).get_data())
            for stream in page.page_obj.contents or []
        )
    except Exception:  # noqa: BLE001 - 内容流解不开就按看得见的字算
        return False


def _text_page(page, opts: dict) -> dict:
    """一页：是不是扫描页（{"scan": true}）；不是的，表格外的文字行（[上边, 下边, 文字]，页码行
    由 attachment_text 按页面高度去掉）、表格（[上边, 各行各格的文字（合并单元格占的位置是
    null）, 竖线位置]）。

    扫描页：几乎没有文字、图片占了三成以上的页面；或者整页（九成以上）被图片盖住、上面的字是
    看不见的识别层（扫描件自动识别出的字，错字多，不取）。只有几个字加一枚公章、一个徽标的页
    （"特此通知"），整页底图上是正常文字的页（演示文稿导出的 PDF），字照样取。"""
    images = page.images
    if images:
        x0, top, x1, bottom = page.bbox
        cover = sum(
            max(0.0, min(im["x1"], x1) - max(im["x0"], x0))
            * max(0.0, min(im["bottom"], bottom) - max(im["top"], top))
            for im in images
        )
        area = page.width * page.height
        if len(page.chars) < opts["scan_chars"] and cover >= area * opts["scan_some_cover"]:
            return {"scan": True}
        if cover >= area * opts["scan_cover"] and _ocr_layer(page):
            return {"scan": True}
    if not page.chars and len(page.curves) >= opts["outlined_curves"]:
        return {"scan": True}  # 字被转成了线条（导出时"文字转曲"），取不出
    page = page.dedupe_chars()  # 为了加粗把同一个字重画一遍的，只留一个
    try:
        # 线条特别多的页（图纸、图表）找表格可能很慢：不找，只取文字行（9/28 数据里最多 1200 条）
        found = page.find_tables() if len(page.edges) <= opts["max_edges"] else []
    except Exception:  # noqa: BLE001 - 找表格失败就当没有表格，照样取文字
        found = []
    boxes = [t.bbox for t in found]

    def outside(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        x = (obj["x0"] + obj["x1"]) / 2
        y = (obj["top"] + obj["bottom"]) / 2
        return not any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in boxes)

    rest = page.filter(outside) if boxes else page
    lines = [
        [line["top"], line["bottom"], line.get("text") or ""]
        for line in rest.extract_text_lines(layout=False, strip=True, return_chars=False)
    ]
    tables = []
    for table in found:
        values = table.extract()
        # 表格竖线的位置（取整到 1pt）：下一页开头的表格竖线一样，才是续表
        xs = sorted({round(c[0]) for c in table.cells} | {round(c[2]) for c in table.cells})
        tables.append([table.bbox[1], values, xs])
    return {"scan": False, "height": page.height, "lines": lines, "tables": tables}


def _list(pdf, opts: dict) -> dict:
    """名单解析（lists.parser）：{"pages": 总页数, "has_text": 前 pages 页有没有字, "tables": 各页
    表格（每格是文字或 null）}。表格超了预算（rows、cells、columns、chars，和 parser._TableBudget
    同样数法）就停在超了的那一行（结果里带到这一行为止，parser 照原样报错）；没有字的也停。"""
    page_count = len(pdf.pages)
    has_text = False
    tables_out: list[list] = []
    rows = cells = chars = 0
    for page in islice(pdf.pages, opts["pages"]):
        try:
            has_text = has_text or bool((page.extract_text() or "").strip())
            tables = page.extract_tables()
            if not tables:
                tables = page.extract_tables(
                    table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"}
                )
            for table in tables:
                for index, row in enumerate(table):
                    rows += 1
                    cells += len(row)
                    chars += sum(len(str(value)) for value in row if value is not None)
                    if (
                        len(row) > opts["columns"]
                        or rows > opts["rows"]
                        or cells > opts["cells"]
                        or chars > opts["chars"]
                    ):
                        tables_out.append(table[: index + 1])
                        raise _Stop(
                            {"pages": page_count, "has_text": has_text, "tables": tables_out}
                        )
                tables_out.append(table)
        finally:
            # pdfplumber 缓存布局和文本对象；异常或超预算也必须释放当前页。
            page.close()
    result = {"pages": page_count, "has_text": has_text, "tables": tables_out}
    if not has_text:
        raise _Stop(result)
    return result


def _body(pdf, opts: dict) -> dict:
    """PDF 正文（crawler.pdf）：{"pages": 总页数, "text": 各页文字}；超过 pages 页的不读
    （只有 pages）。"""
    pages = len(pdf.pages)
    if pages > opts["pages"]:
        return {"pages": pages}
    texts = []
    for page in pdf.pages:
        texts.append(page.extract_text() or "")
        page.close()
    return {"pages": pages, "text": "\n".join(texts)}


def _ocr(pdf, opts: dict) -> dict:
    """扫描页识别文字（attachment_text 的识别，D-125）：opts["pages"] 里的页（从 1 起）一页一页
    渲染成图片（opts["dpi"]，一页的像素超过 opts["pixels"] 就降低分辨率），交给开源的 RapidOCR
    （onnxruntime，模型在安装包里，不联网）。返回 {"pages": {页码: {"height": 页高, "lines": [[上边,
    下边, 左边, 文字, 置信度], ...]}}}，坐标是 PDF 的点（和 _text_page 一样从页面上边往下量）。
    没装识别组件时导入就失败（error: ModuleNotFoundError），调用方先查装没装。某一页渲染、识别
    出错（坏的图片等）只跳过这一页；内存超了照样整个子进程退出（_exit_on_memory_error）。"""
    import cv2
    from rapidocr_onnxruntime import RapidOCR

    cv2.setNumThreads(opts["threads"])  # OpenCV 自己的线程池也按设定的线程数
    engine = RapidOCR(intra_op_num_threads=opts["threads"], inter_op_num_threads=1)
    out: dict[str, dict] = {}
    for number in opts["pages"]:
        if not 1 <= number <= len(pdf.pages):
            continue
        try:
            page = pdf.pages[number - 1]
        except Exception:  # noqa: BLE001 - 这一页的对象坏了：跳过
            continue
        try:
            area = max(float(page.width) * float(page.height), 1.0)
            # 分辨率（每英寸点数）：按要的给，页面很大时按像素上限降下来（72 点一英寸）
            dpi = min(float(opts["dpi"]), 72 * math.sqrt(opts["pixels"] / area))
            image = page.to_image(resolution=dpi).original
            result, _elapsed = engine(image)
            scale = 72 / dpi
            lines = []
            for box, text, score in result or []:
                xs = [float(point[0]) for point in box]
                ys = [float(point[1]) for point in box]
                lines.append(
                    [
                        round(min(ys) * scale, 2),
                        round(max(ys) * scale, 2),
                        round(min(xs) * scale, 2),
                        str(text),
                        round(float(score), 3),
                    ]
                )
            out[str(number)] = {"height": float(page.height), "lines": lines}
        except Exception:  # noqa: BLE001 - 这一页渲染、识别出错（坏的图片等）：跳过这一页
            continue
        finally:
            page.close()
    return {"pages": out}


TASKS = {"copy": _copy, "text": _text, "list": _list, "body": _body, "ocr": _ocr}
