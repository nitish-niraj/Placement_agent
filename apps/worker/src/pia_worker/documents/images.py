"""Image OCR via RapidOCR (fallback path — vision LLM is primary per P5 decision)."""

import io

import structlog

from pia_worker.documents.types import ParsedDocument

logger = structlog.get_logger()
_engine = None  # lazy singleton — model download happens on first use


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    return _engine


def rapid_ocr(data: bytes) -> ParsedDocument:
    """OCR an image. Returns raw text + confidence; row structuring is left to
    the review path (this branch only runs when the vision LLM is unavailable)."""
    import numpy as np
    from PIL import Image

    try:
        image = np.array(Image.open(io.BytesIO(data)).convert("RGB"))
        result, _elapsed = _get_engine()(image)
    except Exception as exc:  # noqa: BLE001 — OCR failure is a review state, not a crash
        logger.warning("rapidocr_failed", error=str(exc)[:150])
        return ParsedDocument(parser="rapidocr-v1", needs_review=True,
                              notes=[f"ocr failed: {str(exc)[:120]}"])

    if not result:
        return ParsedDocument(parser="rapidocr-v1", needs_review=True,
                              notes=["ocr produced no text"])
    lines = [(item[1], float(item[2])) for item in result]
    text = "\n".join(t for t, _ in lines)
    avg_conf = sum(c for _, c in lines) / max(len(lines), 1)
    return ParsedDocument(
        text=text[:100_000], parser="rapidocr-v1", confidence=round(avg_conf, 3),
        needs_review=True,  # unstructured text — review until LLM structuring runs
        notes=["rapidocr fallback (vision LLM unavailable) — raw OCR text retained"],
    )
