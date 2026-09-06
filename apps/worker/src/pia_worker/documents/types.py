"""Shared parsing types for document intelligence (P5)."""

import re
from dataclasses import dataclass, field

from pia_shared.schemas import CandidateRow

_WS = re.compile(r"\s+")


@dataclass
class ParsedDocument:
    """Outcome of parsing one attachment (stored in document_extractions)."""

    rows: list[CandidateRow] = field(default_factory=list)
    text: str = ""  # extracted raw text (pdf/ocr); truncated before persisting
    pages_or_sheets: int = 0
    parser: str = ""  # xlsx-v1 | csv-v1 | pdf-text-v1 | vision-v1 | rapidocr-v1
    confidence: float = 0.0
    needs_review: bool = False
    notes: list[str] = field(default_factory=list)
    is_candidate_list: bool | None = None  # FR-ELG-001 (detection layer refines)
    company: str | None = None


def collapse_ws(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", str(text)).strip()


def cell_to_str(value: object) -> str:
    """Excel cells arrive as str/int/float/datetime/None — stringify stably."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return collapse_ws(str(value))
