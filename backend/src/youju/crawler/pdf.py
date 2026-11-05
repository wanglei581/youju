"""仅把公开政府 PDF 作为通知正文读取；不持久化二进制文件。"""

from io import BytesIO

import pdfplumber

from youju import pdf_limits
from youju.crawler.detail import redact_sensitive


def extract_pdf_text(content: bytes) -> tuple[str, str | None]:
    """PDF 里的文字，没有脱敏：只在内存里用，入库前要过 redact_sensitive。
    取不出时文字为空、带原因（解数据流有上限，见 pdf_limits）。"""
    if not content.startswith(b"%PDF-"):
        return "", "响应不是 PDF"
    try:
        with pdf_limits.reading(), pdfplumber.open(BytesIO(content)) as pdf:
            if len(pdf.pages) > 200:
                return "", "PDF 超过 200 页"
            body = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
    except Exception as exc:  # noqa: BLE001 - 第三方 PDF 解码器会抛多种异常
        hit = pdf_limits.refused(exc)
        return "", f"PDF {hit.detail}" if hit else "PDF 无法解析"
    if not body:
        return "", "扫描件，需 OCR"
    return body, None


def extract_pdf_body(content: bytes) -> tuple[str, str | None]:
    body, error = extract_pdf_text(content)
    return (redact_sensitive(body), None) if body else ("", error)
