"""量扫描页识别（D-125）的用时和内存：

    uv run python scripts/ocr_measure.py [PDF 文件 ...] [--pages 3] [--threads 1] [--sandbox]

不给文件时现做一份 A4 的"扫描件"（每页 30 行英文和数字，200dpi 的整页图片）。每份识别前 --pages 页。

- 默认在本进程里按识别任务的读法跑（pdf_sandbox._work("ocr", ...)，不起子进程、不设内存上限），
  打印每页的墙上时间、CPU 时间和本进程的内存峰值：Linux 上 VmPeak 是地址空间（识别子进程的内存
  上限 RLIMIT_AS 管的就是它），VmHWM 是实际占用的峰值。
- --sandbox 照识别任务的样子走一遍（attachment_text.extract，识别在限内存 OCR_MEMORY_MAX、限 CPU
  时间的子进程里），看识别成没成、子进程实际占用的内存峰值。

只读给的文件，不连数据库、不联网。要先装识别组件：uv sync --extra ocr。"""

import argparse
import io
import os
import resource
import sys
import time
from pathlib import Path


def synthetic(pages: int) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=34)
    images = []
    for page in range(pages):
        image = Image.new("L", (1654, 2339), 255)
        draw = ImageDraw.Draw(image)
        for i in range(30):
            day = f"2026-12-{i % 28 + 1:02d}"
            line = f"Item {page + 1}.{i + 1:02d}: submit the application form before {day}."
            draw.text((110, 140 + i * 68), line, fill=0, font=font)
        images.append(image)
    out = io.BytesIO()
    images[0].save(out, "PDF", resolution=200, save_all=True, append_images=images[1:])
    return out.getvalue()


def _mb(maxrss: int) -> float:
    return maxrss / 1024 / (1024 if sys.platform == "darwin" else 1)  # macOS 是字节，Linux 是 KB


def memory() -> str:
    try:
        status = Path("/proc/self/status").read_text()
    except OSError:  # macOS：没有 /proc
        peak = _mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return f"本进程内存峰值（实际占用）{peak:.0f}MB"
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    return f"本进程 VmPeak {fields['VmPeak'].strip()}，VmHWM {fields['VmHWM'].strip()}"


def in_process(name: str, data: bytes, pages: int, threads: int) -> None:
    from youju import attachment_text as at
    from youju import pdf_sandbox

    options = {
        "pages": list(range(1, pages + 1)),
        "dpi": at._OCR_DPI,
        "pixels": at._OCR_PIXELS,
        "threads": threads,
    }
    wall, cpu = time.perf_counter(), time.process_time()
    answer = pdf_sandbox._work("ocr", data, options)
    wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
    if "ok" not in answer:
        print(f"{name}：没识别成 {answer}")
        return
    found = answer["ok"]["pages"]
    lines = sum(len(page["lines"]) for page in found.values())
    each = max(len(found), 1)
    print(
        f"{name}：识别 {len(found)} 页、{lines} 行；墙上 {wall:.1f} 秒、CPU {cpu:.1f} 秒"
        f"（每页 {wall / each:.1f} / {cpu / each:.1f} 秒，含载入模型）"
    )


def sandboxed(name: str, data: bytes, pages: int, threads: int) -> bool:
    from dataclasses import replace

    from youju import attachment_text as at
    from youju import pdf_sandbox

    limits = replace(at._limits(), ocr_pages=pages, ocr_threads=threads)
    wall = time.perf_counter()
    result = at.extract(data, limits=limits)
    wall = time.perf_counter() - wall
    peak = _mb(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    print(
        f"{name}（子进程，内存上限 {pdf_sandbox.OCR_MEMORY_MAX // 1024 // 1024}MB）："
        f"{'能读' if result.ok else '没读成 ' + str(result.reason)}，认出 {result.ocr_pages} 页，"
        f"墙上 {wall:.1f} 秒；子进程内存峰值（实际占用）{peak:.0f}MB"
    )
    return result.ok and result.ocr_pages > 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="要量的 PDF（不给就现做一份）")
    parser.add_argument("--pages", type=int, default=3, help="每份识别前几页（默认 3）")
    parser.add_argument("--threads", type=int, default=1, help="识别用几个线程（默认 1）")
    parser.add_argument("--sandbox", action="store_true", help="在限内存的子进程里走一遍")
    args = parser.parse_args()
    # 和识别子进程一样限住 numpy、onnxruntime 的线程数（见 pdf_sandbox._env）；要在导入之前设
    os.environ.update(
        OMP_NUM_THREADS=str(args.threads), OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1"
    )
    docs = [(name, Path(name).read_bytes()) for name in args.files]
    docs = docs or [("现做的扫描件", synthetic(args.pages))]
    ok = True
    for name, data in docs:
        if args.sandbox:
            ok = sandboxed(name, data, args.pages, args.threads) and ok
        else:
            in_process(name, data, args.pages, args.threads)
    if not args.sandbox:
        print(memory())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
