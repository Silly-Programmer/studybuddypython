"""Background work. Each job runs in its own thread, so the page stays responsive while it works."""
import threading
import time

import config
import db
import pdf_reader


def start(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def process_document(doc_id: str) -> None:
    """Read the PDF (OCR on scanned pages), save the chunks, mark the document ready."""
    try:
        doc = db.one("select * from documents where id=?", (doc_id,))
        if not doc:
            return
        db.update("documents", doc_id, status="extracting", progress=1, status_detail="Opening your PDF", error=None)
        state = {"stage": "", "at": 0.0}

        def progress(stage, percent, detail, page_count=None):
            now = time.time()
            if stage == state["stage"] and page_count is None and now - state["at"] < 0.4:
                return  # do not write to the database on every single page
            state.update(stage=stage, at=now)
            extra = {"page_count": page_count} if page_count else {}
            db.update("documents", doc_id, status=stage, progress=percent, status_detail=detail, **extra)

        result = pdf_reader.read_pdf(config.UPLOAD_DIR / doc["filename"], progress)
        db.replace_chunks(doc_id, result["chunks"])
        db.update("documents", doc_id, status="ready", progress=100, status_detail=None,
                  page_count=result["page_count"], ocr_pages=result["ocr_pages"], error=None)
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, pdf_reader.PdfError):
            print("[pdf] failed:", repr(e))
        msg = str(e) if isinstance(e, pdf_reader.PdfError) else "Something went wrong while reading this PDF. Try again, or upload a different copy."
        db.update("documents", doc_id, status="failed", status_detail=None, error=msg)
