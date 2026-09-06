"""Attachment parsing orchestrator (P5, F-011..F-014 + FR-ELG-001 detection).

Routing ladder:
- xlsx/csv            → deterministic parsers (openpyxl/csv), confidence 0.95
- pdf (text)          → PyMuPDF text, per-page LLM structuring into rows
- pdf (scanned)       → page renders → vision LLM (llama-3.2-11b-vision)
- image               → vision LLM primary; RapidOCR fallback when NIM unavailable
- anything else       → NEEDS_REVIEW

FR-ELG-001 (is this an eligibility list?) is decided by rules ∪ vision, with the
LLM detector enriching the company name. Vision output is schema-validated
(VisionExtraction); ambiguous/low-confidence rows keep needs_review=True and are
never auto-matched later (ADR-005).
"""

import re

import structlog

from pia_shared.schemas import CandidateRow, EligibilityDetection, VisionExtraction
from pia_worker.ai.provider import NIMProvider, ProviderError
from pia_worker.ai.rules import eligible_list_signal
from pia_worker.documents import images, pdfdoc
from pia_worker.documents.excel import parse_csv, parse_xlsx
from pia_worker.documents.types import ParsedDocument, collapse_ws
from pia_worker.settings import get_settings

logger = structlog.get_logger()

VISION_SYSTEM = """You read candidate eligibility lists from document images.
Extract EVERY candidate row. For each row: raw_name exactly as printed;
normalized_name = lowercase, punctuation stripped; roll_number / registration_number /
student_id only when a column or value clearly is one (never guess); put any other
identifying columns (branch, cgpa, gender...) into other_identifiers keyed by the
printed column header; extraction_confidence = your per-row certainty 0-1.
Set company from the document title/header if present. is_candidate_list=false if
the document is not an eligibility/shortlist/result list.
Respond with ONLY JSON matching:
{"rows": [...], "company": null, "is_candidate_list": true, "notes": null}"""

DETECT_PROMPT = """Does this message/document text announce an eligible-candidate list,
shortlist, or selection result for a company? Respond with ONLY JSON:
{"is_candidate_list": true/false, "company": "<name or null>",
 "evidence": ["short quotes from the text"]}"""


def _rows_to_dicts(rows: list[CandidateRow]) -> list[dict]:
    return [r.model_dump(mode="json") for r in rows]


def _vision_extract(image_png: bytes, provider: NIMProvider | None,
                    correlation_id: str) -> tuple[VisionExtraction | None, NIMProvider]:
    provider = provider or NIMProvider()
    try:
        result, _usage = provider.complete_vision_structured(
            task="vision_extraction",
            system=VISION_SYSTEM,
            user="Extract all candidate rows from this eligibility list image as JSON.",
            image_png=image_png,
            schema=VisionExtraction,
            correlation_id=correlation_id,
        )
        return result, provider
    except ProviderError as exc:
        logger.warning("vision_extraction_failed", error=str(exc)[:120])
        return None, provider


def _detect(parsed: ParsedDocument, file_name: str,
            provider: NIMProvider | None, correlation_id: str) -> None:
    """FR-ELG-001: rules first; LLM only enriches a positive signal (cost guard)."""
    row_names = " ".join(r.raw_name for r in parsed.rows[:50])
    haystack = f"{file_name}\n{parsed.text[:2000] or row_names}"
    # A table with many candidate rows IS a candidate list, even without keywords.
    row_signal = len(parsed.rows) >= 5
    rule_hit = eligible_list_signal(haystack) or row_signal
    if parsed.is_candidate_list is not None:  # vision already answered
        parsed.is_candidate_list = parsed.is_candidate_list or rule_hit
    elif rule_hit:
        parsed.is_candidate_list = True
    else:
        parsed.is_candidate_list = False
        return

    if parsed.company is None:
        parsed.company = company_from_file_name(file_name)
    if parsed.company is None and provider is not None:
        try:
            detection: EligibilityDetection = provider.complete_structured(
                task="eligibility_detector",
                system=DETECT_PROMPT,
                user=collapse_ws(haystack)[:3500],
                schema=EligibilityDetection,
                correlation_id=correlation_id,
            )[0]
            if detection.company:
                parsed.company = detection.company
        except ProviderError as exc:
            logger.warning("eligibility_detection_llm_failed", error=str(exc)[:120])
            parsed.notes.append("company detection unavailable (LLM unreachable)")


_COMPANY_FROM_NAME = re.compile(
    r"^\s*(?P<company>[A-Za-z0-9][A-Za-z0-9 .&,'-]*?)\s+(?:O[CT]|T[CE])\.\d+",
    re.IGNORECASE)


def company_from_file_name(file_name: str | None) -> str | None:
    """LPU placement files are named 'COMPANY OC.43620.2027.63637.xlsx' — a free,
    deterministic company signal."""
    if not file_name:
        return None
    match = _COMPANY_FROM_NAME.match(file_name)
    return collapse_ws(match.group("company")) if match else None


def parse_document_bytes(
    mime_type: str, file_name: str | None, data: bytes,
    provider: NIMProvider | None = None, correlation_id: str = "",
) -> ParsedDocument:
    settings = get_settings()
    name = (file_name or "").lower()
    mime = (mime_type or "").lower()

    if "spreadsheetml" in mime or name.endswith(".xlsx"):
        parsed = parse_xlsx(data)
    elif "csv" in mime or name.endswith(".csv"):
        parsed = parse_csv(data)
    elif name.endswith(".xls"):
        parsed = ParsedDocument(parser="none", needs_review=True,
                                notes=["legacy .xls not supported in MVP (FR-ELG-002 scope)"])
    elif "pdf" in mime or name.endswith(".pdf"):
        pages = pdfdoc.extract_pages(data)
        parsed = _parse_pdf(pages, data, provider, correlation_id, settings)
    elif mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        parsed = _parse_image(data, provider, correlation_id)
    else:
        parsed = ParsedDocument(parser="none", needs_review=True,
                                notes=[f"unsupported type: {mime or 'unknown'}"])

    _detect(parsed, file_name or "", None if parsed.parser in ("none",) else provider,
            correlation_id)
    # Vision confidence floor (ADR-009: provisional, tuned in P6 evaluation)
    floor = settings.vision_confidence_floor
    if parsed.parser == "vision-v1" and parsed.rows:
        weak = [r for r in parsed.rows if r.extraction_confidence < floor]
        if weak:
            parsed.needs_review = True
            parsed.notes.append(f"{len(weak)} row(s) below confidence floor {floor}")
    return parsed


def _parse_pdf(pages: list[dict], data: bytes, provider: NIMProvider | None,
               correlation_id: str, settings) -> ParsedDocument:
    total, text = pdfdoc.pdf_summary(pages)
    if pdfdoc.looks_scanned(pages):
        parsed = ParsedDocument(parser="vision-v1", pages_or_sheets=total,
                                text=text[:100_000])
        for p in pages[:5]:  # vision per rendered page (cap: 5 pages)
            png = pdfdoc.render_page_png(data, p["page"])
            result, _provider = _vision_extract(png, provider, correlation_id)
            if result is None:
                parsed.needs_review = True
                parsed.notes.append(f"page {p['page']}: vision unavailable")
                continue
            parsed.is_candidate_list = parsed.is_candidate_list or result.is_candidate_list
            parsed.company = parsed.company or result.company
            for r in result.rows:
                r.source_page_or_sheet = f"page {p['page']}"
                parsed.rows.append(r)
        if not parsed.rows:
            parsed.needs_review = True
            parsed.notes.append("scanned pdf: no rows extracted")
        return parsed

    # Text PDF: deterministic text kept; rows structured per page via LLM
    parsed = ParsedDocument(parser="pdf-text-v1", pages_or_sheets=total,
                            text=text[:100_000], confidence=0.9)
    for p in pages[:5]:
        page_text = p["text"].strip()
        if not page_text:
            continue
        try:
            active_provider = provider or NIMProvider()
            structured: VisionExtraction = active_provider.complete_structured(
                task="pdf_row_extraction",
                system=VISION_SYSTEM,
                user=f"[page {p['page']}]\n{page_text[:3500]}",
                schema=VisionExtraction,
                correlation_id=correlation_id,
                max_tokens=1500,
            )[0]
        except ProviderError:
            parsed.needs_review = True
            parsed.notes.append(f"page {p['page']}: LLM structuring unavailable")
            continue
        parsed.is_candidate_list = parsed.is_candidate_list or structured.is_candidate_list
        parsed.company = parsed.company or structured.company
        for r in structured.rows:
            r.source_page_or_sheet = f"page {p['page']}"
            parsed.rows.append(r)
    if not parsed.rows:
        parsed.needs_review = True
        parsed.notes.append("text pdf: no candidate rows found")
    return parsed


def _parse_image(data: bytes, provider: NIMProvider | None,
                 correlation_id: str) -> ParsedDocument:
    result, provider = _vision_extract(data, provider, correlation_id)
    if result is not None:
        parsed = ParsedDocument(
            parser="vision-v1", rows=result.rows, company=result.company,
            is_candidate_list=result.is_candidate_list or bool(result.rows),
            confidence=round(
                sum(r.extraction_confidence for r in result.rows)
                / max(len(result.rows), 1), 3) if result.rows else 0.5,
            notes=[result.notes] if result.notes else [],
        )
        if not result.rows:
            parsed.needs_review = True
            parsed.notes.append("vision found no candidate rows")
        return parsed
    # Vision unavailable → RapidOCR fallback (user decision: P5 OCR strategy)
    fallback = images.rapid_ocr(data)
    fallback.is_candidate_list = (
        eligible_list_signal(fallback.text) if fallback.text else None)
    return fallback
