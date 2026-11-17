"""PDF -> text per page (OCR only where a page is a scan) -> cleaned -> chunks tagged with page numbers."""
import re
from concurrent.futures import ThreadPoolExecutor

import pymupdf
from PIL import Image

import config


class PdfError(Exception):
    """A problem with the PDF that we can explain to the user in plain words."""


# ---------- cleaning ----------
def clean_text(raw: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)   # re-join words hyphenated across lines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- chunking ----------
def _split_long(text: str, max_len: int) -> list[str]:
    out, rest = [], text
    while len(rest) > max_len:
        cut = rest.rfind("\n", 0, max_len)
        if cut < max_len * 0.5:
            cut = rest.rfind(" ", 0, max_len)
        if cut < max_len * 0.5:
            cut = max_len
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def chunk_pages(pages: list[dict], max_chars: int) -> list[dict]:
    """Group pages into chunks of about max_chars. Every page starts with [[Page N]] so the AI can cite it."""
    chunks: list[dict] = []
    buf, start, end, ocr = "", 0, 0, False

    def flush():
        nonlocal buf, ocr
        if buf:
            chunks.append({"index": len(chunks), "page_start": start, "page_end": end, "content": buf.strip(), "used_ocr": ocr})
        buf, ocr = "", False

    for p in pages:
        if not p["text"]:
            continue
        block = f"[[Page {p['page']}]]\n{p['text']}\n\n"
        if len(block) > max_chars:  # a single huge page
            flush()
            for piece in _split_long(p["text"], max_chars - 20):
                chunks.append({"index": len(chunks), "page_start": p["page"], "page_end": p["page"],
                               "content": f"[[Page {p['page']}]]\n{piece}", "used_ocr": p["ocr"]})
            continue
        if buf and len(buf) + len(block) > max_chars:
            flush()
        if not buf:
            start = p["page"]
        buf += block
        end = p["page"]
        ocr = ocr or p["ocr"]
    flush()
    return chunks


# ---------- OCR ----------
def _ocr_image(img: Image.Image) -> str:
    import pytesseract
    return pytesseract.image_to_string(img, lang="eng")


def _check_tesseract() -> None:
    import os
    import pytesseract
    if os.getenv("TESSERACT_CMD"):
        pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        raise PdfError(
            "Some pages are scanned images and need the free Tesseract OCR program, which is not installed "
            "(or not on PATH). See README.md -> 'Install Tesseract', then try again."
        )


# ---------- the whole pipeline ----------
def read_pdf(path, on_progress) -> dict:
    """on_progress(stage, percent, detail, page_count=None). Returns {page_count, ocr_pages, chunks}."""
    with open(path, "rb") as f:
        if b"%PDF-" not in f.read(1024):
            raise PdfError("This does not look like a PDF file.")
    try:
        doc = pymupdf.open(path)
    except Exception:
        raise PdfError("This PDF could not be opened. It may be damaged. Try saving or exporting it again.")

    with doc:
        if doc.needs_pass:
            raise PdfError("This PDF is password-protected. Remove the password and upload it again.")
        total = doc.page_count
        if total > config.MAX_PAGES:
            raise PdfError(f"This PDF has {total} pages. The limit is {config.MAX_PAGES}.")
        on_progress("extracting", 3, f"Found {total} pages", total)

        # 1. Read the text layer of every page.
        pages: list[dict] = []
        scanned: list[int] = []
        for n in range(1, total + 1):
            text = clean_text(doc[n - 1].get_text())
            if len(text) < config.OCR_MIN_CHARS:
                scanned.append(n)
            pages.append({"page": n, "text": text, "ocr": False})
            if n % 5 == 0 or n == total:
                on_progress("extracting", 3 + round(n / total * 37), f"Reading page {n} of {total}")

        # 2. OCR only the pages with (almost) no text.
        if scanned:
            _check_tesseract()
            on_progress("ocr", 40, f"Starting OCR for {len(scanned)} scanned page{'s' if len(scanned) > 1 else ''}")
            done = 0
            batch = config.OCR_WORKERS * 2
            matrix = pymupdf.Matrix(config.OCR_SCALE, config.OCR_SCALE)
            with ThreadPoolExecutor(config.OCR_WORKERS) as pool:
                for i in range(0, len(scanned), batch):
                    numbers = scanned[i:i + batch]
                    images = []  # PyMuPDF documents are not thread-safe, so pages are rendered here, one by one
                    for n in numbers:
                        pix = doc[n - 1].get_pixmap(matrix=matrix, alpha=False)
                        images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
                    for n, text in zip(numbers, pool.map(_ocr_image, images)):  # the OCR itself runs in parallel
                        pages[n - 1] = {"page": n, "text": clean_text(text), "ocr": True}
                        done += 1
                        on_progress("ocr", 40 + round(done / len(scanned) * 50), f"OCR: page {done} of {len(scanned)} scanned pages")

    # 3. Chunk.
    on_progress("chunking", 93, "Preparing text for AI")
    chunks = chunk_pages(pages, config.CHUNK_CHARS)
    if not chunks:
        raise PdfError("No readable text was found in this PDF.")
    return {"page_count": total, "ocr_pages": len(scanned), "chunks": chunks}
