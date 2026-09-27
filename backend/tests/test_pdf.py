from youju.crawler.pdf import extract_pdf_body


def test_scanned_pdf_is_marked_for_ocr(monkeypatch):
    class Page:
        def extract_text(self):
            return None

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("youju.crawler.pdf.pdfplumber.open", lambda _: Pdf())
    assert extract_pdf_body(b"%PDF-scan") == ("", "扫描件，需 OCR")
