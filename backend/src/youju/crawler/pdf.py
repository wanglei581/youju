"""仅把公开政府 PDF 作为通知正文读取；不持久化二进制文件。"""

from youju import pdf_sandbox
from youju.crawler.detail import redact_sensitive

MAX_PAGES = 200
# 读一份 PDF 正文最多用这么多秒 CPU 时间（补详情在线程池里读，SIGALRM 管不了，由 pdf_sandbox
# 停掉子进程）。11/8 实测 341 份真实文件最慢 53 秒（Linux 虚拟机）
PDF_SECONDS = 240


def extract_pdf_text(content: bytes) -> tuple[str, str | None]:
    """PDF 里的文字，没有脱敏：只在内存里用，入库前要过 redact_sensitive。
    取不出时文字为空、带原因（在限内存、限时的子进程里读，解数据流有上限，见 pdf_sandbox）；
    遇到和当时的环境有关的问题抛 pdf_sandbox.Failed（retry 为真），由补详情过一段时间再抓。"""
    if not content.startswith(b"%PDF-"):
        return "", "响应不是 PDF"
    try:
        found = pdf_sandbox.read("body", content, seconds=PDF_SECONDS, pages=MAX_PAGES)
        if "text" not in found:
            return "", f"PDF 超过 {MAX_PAGES} 页"
        body = found["text"].strip()
    except pdf_sandbox.Failed as exc:
        if exc.retry:
            raise  # 和当时的环境有关（容器内存不够、忙……）：补详情记成 pdf_retry，过一段时间再抓
        # 损坏、读的时候出错照旧记"无法解析"；解压超了、超时、超内存写明原因（都不自动重抓）
        return "", "PDF 无法解析" if exc.kind in ("open", "error") else f"PDF {exc.detail}"
    except Exception:  # noqa: BLE001 - 起不了子进程等，和原来解析出错一样记
        return "", "PDF 无法解析"
    if not body:
        return "", "扫描件，需 OCR"
    return body, None


def extract_pdf_body(content: bytes) -> tuple[str, str | None]:
    body, error = extract_pdf_text(content)
    return (redact_sensitive(body), None) if body else ("", error)
