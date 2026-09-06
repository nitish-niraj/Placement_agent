"""XLSX/CSV parsers (F-011, FR-ELG-002): deterministic table → CandidateRows."""

import csv
import io

import openpyxl

from pia_shared.schemas import CandidateRow
from pia_shared.textnorm import normalize_name
from pia_worker.documents.headers import find_header_row
from pia_worker.documents.types import ParsedDocument, cell_to_str

STRUCTURED_CONFIDENCE = 0.95


def _rows_from_mapping(
    grid: list[list[str]], mapping: dict[int, str], header_idx: int,
    sheet_name: str, confidence: float,
) -> tuple[list[CandidateRow], dict[int, str]]:
    rows: list[CandidateRow] = []
    # Columns without a dedicated CandidateRow field (branch, batch, Sr, ...) are
    # preserved in other_identifiers — nothing from the list is silently dropped.
    dedicated = {"name", "roll_number", "registration_number", "student_id"}
    unmapped_headers: dict[int, str] = {
        col: grid[header_idx][col]
        for col in range(len(grid[header_idx]))
        if mapping.get(col) not in dedicated and grid[header_idx][col]
    }
    field_cols = {field: col for col, field in mapping.items()}  # field → column
    name_col = field_cols.get("name")
    roll_col = field_cols.get("roll_number")
    reg_col = field_cols.get("registration_number")
    student_col = field_cols.get("student_id")
    for i, raw_row in enumerate(grid[header_idx + 1:], start=header_idx + 2):
        values = [cell_to_str(v) for v in raw_row]
        if not any(values):
            continue
        name = values[name_col] if name_col is not None and name_col < len(values) else ""
        if not name:
            continue  # rows without a name are not candidate rows
        others: dict[str, str] = {}
        for col, header in unmapped_headers.items():
            if col < len(values) and values[col]:
                others[header] = values[col]

        def ident(col: int | None, row_values: list[str]) -> str | None:
            if col is not None and col < len(row_values) and row_values[col]:
                return row_values[col]
            return None

        rows.append(CandidateRow(
            raw_name=name,
            normalized_name=normalize_name(name),
            roll_number=ident(roll_col, values),
            registration_number=ident(reg_col, values),
            student_id=ident(student_col, values),
            other_identifiers=others,
            extraction_confidence=confidence,
            source_page_or_sheet=sheet_name,
            row_index=i,
        ))
    return rows, unmapped_headers


def parse_xlsx(data: bytes) -> ParsedDocument:
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parsed = ParsedDocument(parser="xlsx-v1", pages_or_sheets=len(workbook.sheetnames),
                            confidence=STRUCTURED_CONFIDENCE)
    for sheet in workbook.worksheets:
        grid = [[cell_to_str(c) for c in row] for row in sheet.iter_rows(values_only=True)]
        header = find_header_row(grid)
        if header is None:
            parsed.notes.append(f"sheet '{sheet.title}': no candidate-list header found")
            continue
        header_idx, mapping = header
        rows, unmapped = _rows_from_mapping(grid, mapping, header_idx, sheet.title,
                                            STRUCTURED_CONFIDENCE)
        parsed.rows.extend(rows)
        if unmapped:
            parsed.notes.append(f"sheet '{sheet.title}': unmapped columns "
                                f"{sorted(unmapped.values())}")
    if not parsed.rows:
        parsed.needs_review = True
        parsed.notes.append("no candidate rows found in any sheet")
    return parsed


def parse_csv(data: bytes) -> ParsedDocument:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|") if len(sample) > 8 else csv.excel
    reader = list(csv.reader(io.StringIO(text), dialect))
    parsed = ParsedDocument(parser="csv-v1", pages_or_sheets=1,
                            confidence=STRUCTURED_CONFIDENCE)
    header = find_header_row(reader)
    if header is None:
        parsed.needs_review = True
        parsed.notes.append("no candidate-list header found")
        parsed.text = text[:5000]
        return parsed
    header_idx, mapping = header
    rows, unmapped = _rows_from_mapping(reader, mapping, header_idx, "csv",
                                        STRUCTURED_CONFIDENCE)
    parsed.rows = rows
    if unmapped:
        parsed.notes.append(f"unmapped columns {sorted(unmapped.values())}")
    if not rows:
        parsed.needs_review = True
        parsed.notes.append("no candidate rows found")
    return parsed
