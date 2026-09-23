"""Event subject identification — what is this message actually about?

Chain (first hit wins), never inventing:
1. resolved company — the P7 mention linkage or drive-code signal;
2. attachment filename — `COMPANY OC.xxxx` pattern via the P5 helper;
3. ALL-CAPS topic token from the body ("MARS platforms" -> MARS);
4. drive-code company phrase in the text;
5. honest fallback — never the blind string "General".

Company rows are only ever LINKED here (create=False): a topic token must
never mint a company. Display names are student-facing; "General" is banned.
"""

import re
from dataclasses import dataclass

import sqlalchemy

from pia_worker.companies.resolver import resolve_company

FALLBACK_DISPLAY = "📋 Placement Update"

_URL_RE = re.compile(r"https?://\S+")
_TOKEN_RE = re.compile(r"\b([A-Z][A-Z0-9&.\-]{2,})\b")

# ALL-CAPS words that are formatting/timezone noise, not entities.
_TOKEN_STOPLIST = frozenset({
    "AM", "PM", "IST", "URL", "HTTP", "HTTPS", "WWW", "COM", "NOTE", "DATE",
    "TIME", "VENUE", "LINK", "FORM", "LIST", "OC", "TC", "II", "III", "IV",
    "NO", "ON", "AT", "BY", "OF", "OR", "NA", "N/A",
})


@dataclass(frozen=True)
class Subject:
    display: str  # student-facing: canonical company, topic token, or fallback
    company_id: str | None  # only when linked to a real companies row
    is_company: bool
    confidence: float  # 1.0 linked company … 0.0 honest fallback
    evidence: str  # resolved_mention | attachment_filename | topic_token |
    # drive_code | none


def extract_topic_token(text: str) -> str | None:
    """First ALL-CAPS token (≥3 chars) outside URLs that is not stoplisted."""
    body = _URL_RE.sub(" ", text or "")
    for match in _TOKEN_RE.finditer(body):
        token = match.group(1).strip(".-")
        if len(token) >= 3 and token not in _TOKEN_STOPLIST:
            return token
    return None


def identify_subject(
    conn: sqlalchemy.Connection, *, text: str,
    company_id: str | None = None, canonical: str | None = None,
    file_name: str | None = None,
) -> Subject:
    """Resolve the display subject. `company_id`/`canonical` come from the
    existing mention/drive-code path; `file_name` from the message's first
    attachment. Only links — never creates — company rows."""
    if company_id and canonical:
        return Subject(display=canonical, company_id=company_id,
                       is_company=True, confidence=1.0,
                       evidence="resolved_mention")
    if file_name:
        from pia_worker.documents.orchestrator import company_from_file_name

        guess = company_from_file_name(file_name)
        if guess:
            # Link-only: an attachment filename must never mint a company row.
            # (The key/alias paths self-heal missing aliases in-transaction.)
            ref = resolve_company(conn, guess, create=False)
            if ref is not None:
                return Subject(display=ref.canonical_name,
                               company_id=ref.company_id, is_company=True,
                               confidence=0.8, evidence="attachment_filename")
    token = extract_topic_token(text)
    if token:
        return Subject(display=token, company_id=None, is_company=False,
                       confidence=0.6, evidence="topic_token")
    if company_id and not canonical:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT canonical_name FROM companies "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": company_id},
        ).first()
        if row is not None:
            return Subject(display=str(row.canonical_name),
                           company_id=company_id, is_company=True,
                           confidence=0.9, evidence="drive_code")
    return Subject(display=FALLBACK_DISPLAY, company_id=None, is_company=False,
                   confidence=0.0, evidence="none")
