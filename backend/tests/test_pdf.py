from youju import pdf_sandbox
from youju.crawler.pdf import extract_pdf_body


def test_scanned_pdf_is_marked_for_ocr(monkeypatch, pdf_in_process):
    class Page:
        def extract_text(self):
            return None

        def close(self):
            pass

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(pdf_sandbox, "_open", lambda _: Pdf())
    assert extract_pdf_body(b"%PDF-scan") == ("", "扫描件，需 OCR")
