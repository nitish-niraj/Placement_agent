"""PDF handling (F-012): text extraction, scanned detection, page rendering."""

import pymupdf

from pia_worker.documents.types import ParsedDocument


def extract_pages(data: bytes) -> list[dict]:
    """[{page: 1-based, text: str}] for a PDF."""
    pages = []
    with pymupdf.Document(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=1):
            pages.append({"page": i, "text": page.get_text("text", sort=True)})
    return pages


def looks_scanned(pages: list[dict], min_chars_per_page: int = 40) -> bool:
    """A page with almost no extractable text is a scanned/image page."""
    if not pages:
        return True
    return all(len(p["text"].strip()) < min_chars_per_page for p in pages)


def render_page_png(data: bytes, page_number: int, dpi: int = 150) -> bytes:
    with pymupdf.Document(stream=data, filetype="pdf") as doc:
        page = doc[page_number - 1]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def pdf_summary(pages: list[dict]) -> tuple[int, str]:
    return len(pages), "\n".join(p["text"] for p in pages)


def parse_pdf_text_fallback(pages: list[dict]) -> ParsedDocument:
    """Deterministic fallback when the LLM is unavailable: keep the text with page
    markers so the extraction is at least searchable and reviewable."""
    total, text = pdf_summary(pages)
    return ParsedDocument(
        text=text[:100_000], pages_or_sheets=total, parser="pdf-text-v1",
        confidence=0.3, needs_review=True,
        notes=["LLM structuring unavailable — raw text retained with page markers"],
    )
