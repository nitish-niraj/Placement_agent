"""Header-variant detection for candidate-list tables (FR-ELG-002).

College lists use many header spellings ("Roll No.", "Registration No",
"Name of Student", "Enrollment"...). Pure functions — unit-tested.
"""

from pia_shared.textnorm import normalize_name

# canonical field -> normalized header aliases (subset match allowed via prefix rule)
HEADER_ALIASES: dict[str, set[str]] = {
    "name": {"name", "student", "student name", "name of student", "candidate name",
             "name of candidate", "applicant name", "full name"},
    "roll_number": {"roll", "roll no", "roll number", "rollno", "roll num"},
    "registration_number": {"reg", "reg no", "reg number", "regno", "reg num",
                            "registration", "registration no", "registration number",
                            "prov reg no", "provisional reg no"},
    "student_id": {"student id", "studentid", "id", "enrollment", "enrollment no",
                   "enrollment number", "uid", "admission no"},
    "branch": {"branch", "course", "program", "programme", "trade"},
    "batch": {"batch", "session", "passing year"},
}

# long names that must not prefix-match short aliases (e.g. "name" inside "nickname")
_MIN_PREFIX_LEN = 4


def _header_key(value: str) -> str:
    return normalize_name(value)


def map_headers(header_row: list[str]) -> dict[int, str]:
    """Map column indices to canonical fields. Unmapped columns are ignored here
    (they land in other_identifiers)."""
    mapping: dict[int, str] = {}
    for idx, raw in enumerate(header_row):
        key = _header_key(raw)
        if not key:
            continue
        for field, aliases in HEADER_ALIASES.items():
            if field in mapping:
                continue
            # prefix rule: "registration number (rev)" starts with "registration"
            if key in aliases or (
                len(key) >= _MIN_PREFIX_LEN
                and any(a.startswith(key) or key.startswith(a)
                        for a in aliases if len(a) >= _MIN_PREFIX_LEN)
            ):
                mapping[idx] = field
                break
    return mapping


def find_header_row(
    rows: list[list[str]], min_mapped: int = 2,
) -> tuple[int, dict[int, str]] | None:
    """Scan the first rows for the header line (needs >= min_mapped known headers,
    at least one of them being the name column)."""
    best: tuple[int, dict[int, str]] | None = None
    for i, row in enumerate(rows[:10]):
        mapping = map_headers(row)
        if "name" in mapping.values() and len(mapping) >= min_mapped and (
            best is None or len(mapping) > len(best[1])
        ):
            best = (i, mapping)
        if i > 5 and best is not None:
            break
    return best
