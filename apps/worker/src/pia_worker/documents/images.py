"""Image OCR via RapidOCR (fallback path — vision LLM is primary per P5 decision)."""

import io
import re

import structlog

from pia_shared.textnorm import normalize_name
from pia_worker.documents.types import ParsedDocument

logger = structlog.get_logger()
_engine = None  # lazy singleton — model download happens on first use

# Table-line parsing for OCR text (screenshot lists like "12507818 Aman Kumar
# MCA 2027"): deterministic, no LLM. A line is a candidate row when it carries
# a digits-only identifier >= 4 chars. Name may be empty (reg-only fallback) —
# the matcher resolves those as IDENTIFIER 0.85 + review, never dropped.
_ID_RE = re.compile(r"\d{4,}")
_COURSE_RE = re.compile(r"\b(MCA|BCA|B\.?TECH|M\.?TECH|MBA|BBA|CAP[\- ]?MCA)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_HEADER_HINT = re.compile(r"\b(name|reg|roll|branch|course|batch|sr\.?\s*no|s\.?\s*no)\b",
                          re.IGNORECASE)


def ocr_text_to_rows(text: str, source: str = "ocr-image",
                     confidence: float = 0.6):  # noqa: ANN001
    """Parse raw OCR/table text lines into CandidateRows (deterministic).

    Each line with an identifier becomes one row; lines without identifiers
    (headers, titles) are skipped. Never invents names — id-only lines become
    name-empty rows for reg-only matching.
    """
    from pia_shared.schemas import CandidateRow

    rows: list[CandidateRow] = []
    if not text:
        return rows
    idx = 0
    for raw_line in text.splitlines():
        line = raw_line.strip(" |,\t")
        if len(line) < 4:
            continue
        ids = _ID_RE.findall(line)
        if not ids:
            continue
        # Prefer the longest digit group (reg nos are 8 digits; years are 4).
        identifier = max(ids, key=len)
        if len(identifier) < 4:
            continue
        # Strip identifiers, course tokens, years, leading serial numbers.
        name_part = _ID_RE.sub(" ", line)
        name_part = _COURSE_RE.sub(" ", name_part)
        name_part = _YEAR_RE.sub(" ", name_part)
        name_part = re.sub(r"^\s*\d{1,3}\s*[\.\)]\s*", " ", name_part)
        name_tokens = [t for t in re.split(r"\s+", name_part.strip())
                       if re.search(r"[A-Za-z]", t)]
        # Drop single-char noise tokens, keep real name parts.
        name_tokens = [t.strip(".,|_") for t in name_tokens if len(t.strip(".,|_")) >= 2]
        raw_name = " ".join(name_tokens).strip()
        # Header remnant without a real name and with only hint words -> skip
        # unless it also carries a second identifier (real data row).
        if not raw_name and _HEADER_HINT.search(line) and len(ids) < 2:
            # Id-only data row still counts (reg-only fallback); header text
            # like "Reg No Name" has no digits at all so never reaches here.
            # A hint-word line WITH an id but no name is kept as id-only.
            pass
        others: dict[str, str] = {}
        course = _COURSE_RE.search(line)
        if course:
            others["course"] = course.group(1).upper()
        year = _YEAR_RE.search(line)
        if year:
            others["batch"] = year.group(1)
        idx += 1
        rows.append(CandidateRow(
            raw_name=raw_name,
            normalized_name=normalize_name(raw_name),
            registration_number=identifier,
            other_identifiers=others,
            extraction_confidence=confidence,
            source_page_or_sheet=source,
            row_index=idx,
        ))
    return rows


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
