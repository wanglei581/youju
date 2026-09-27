"""仅把公开政府 PDF 作为通知正文读取；不持久化二进制文件。"""

from io import BytesIO

import pdfplumber

from youju.crawler.detail import redact_sensitive


def extract_pdf_body(content: bytes) -> tuple[str, str | None]:
    if not content.startswith(b"%PDF-"):
        return "", "响应不是 PDF"
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            if len(pdf.pages) > 200:
                return "", "PDF 超过 200 页"
            body = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
    except Exception:  # noqa: BLE001 - 第三方 PDF 解码器会抛多种异常
        return "", "PDF 无法解析"
    if not body:
        return "", "扫描件，需 OCR"
    return redact_sensitive(body), None
