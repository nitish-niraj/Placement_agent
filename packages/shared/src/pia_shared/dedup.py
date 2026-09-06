"""P9 dedup engine — pure decisions (FR-DED-001..005, DEC-009, ADR-006).

Layers (evaluated cheap-to-expensive by the caller):
1. EXACT   content-hash match within the reminder window (FR-DED-001). The
           same content reappearing after DEDUP_EXACT_REMINDER_DAYS (default
           7) is a legitimate reminder, not noise — never suppressed (DEC-009).
2. NEAR    token-Jaccard similarity within the near-dup window (FR-DED-002),
           GATED on company agreement: the live corpus shows announcement
           templates score up to 0.705 across DIFFERENT companies, while
           same-company re-announcements score >= 0.720 — text similarity
           alone would wrongly collapse distinct companies' announcements.
3. SEMANTIC fact-tuple comparison on canonical events — implemented in the
           P8 event layer (canonical_key + near-match); P9 adds the
           suppression-reason audit trail (FR-DED-005).
4. VISUAL  dHash on images (DEC-009) — see pia_shared.phash.

No I/O here: callers (API ingestion) bring the data, this module decides.
"""

import hashlib
import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")


def normalized_tokens(text: str | None) -> frozenset[str]:
    return frozenset(_TOKEN.findall((text or "").lower()))


def token_jaccard(a: str | None, b: str | None) -> float:
    """FR-DED-002 similarity: |A∩B| / |A∪B| over normalized tokens."""
    ta, tb = normalized_tokens(a), normalized_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass(frozen=True)
class DedupVerdict:
    suppress: bool
    layer: str | None  # 'exact' | 'near' | 'visual' | None
    reason: str


def decide_exact_duplicate(
    newest_same_hash_age_days: float | None, reminder_days: int
) -> DedupVerdict:
    """FR-DED-001 + DEC-009 7-day rule. `newest_same_hash_age_days` is the age
    of the most recent identical message (None = no identical message)."""
    if newest_same_hash_age_days is None:
        return DedupVerdict(False, None, "")
    if newest_same_hash_age_days >= reminder_days:
        # identical content after the window is a legitimate reminder (DEC-009)
        return DedupVerdict(
            False, None,
            f"exact repeat after {newest_same_hash_age_days:.1f}d >= "
            f"{reminder_days}d: legitimate reminder, not suppressed",
        )
    return DedupVerdict(
        True, "exact",
        f"identical content_hash seen {newest_same_hash_age_days:.1f}d ago "
        f"(< {reminder_days}d reminder window)",
    )


def decide_near_duplicate(
    similarity: float, companies_a: set[str], companies_b: set[str],
    threshold: float, no_company_margin: float = 0.05,
) -> DedupVerdict:
    """FR-DED-002, company-gated. Two messages are near-duplicates only when
    similarity clears the tuned threshold AND their company signals agree:
    - shared company keys -> template similarity is meaningful, suppress;
    - no company signal on EITHER side -> allow suppression only at
      `threshold + no_company_margin` (weaker evidence);
    - conflicting companies -> different announcements, never suppress
      (live corpus: template-only pairs reach 0.705 across companies)."""
    if similarity < threshold:
        return DedupVerdict(False, None, "")
    if companies_a & companies_b:
        return DedupVerdict(
            True, "near",
            f"token similarity {similarity:.2f} >= {threshold} and same company",
        )
    if not companies_a and not companies_b:
        if similarity >= threshold + no_company_margin:
            return DedupVerdict(
                True, "near",
                f"similarity {similarity:.2f} >= {threshold + no_company_margin:.2f} "
                "(no company signal on either side)",
            )
        return DedupVerdict(False, None, "")
    return DedupVerdict(
        False, None,
        f"similarity {similarity:.2f} >= {threshold} but different companies",
    )


def decide_visual_duplicate(
    hamming_distance: int | None, threshold_bits: int = 4
) -> DedupVerdict:
    """DEC-009: re-forwarded/re-compressed screenshots keep a near-identical
    dHash; a distance of 0 is a verbatim re-send, small distances survive
    recompression. None = nothing comparable."""
    if hamming_distance is None:
        return DedupVerdict(False, None, "")
    if hamming_distance <= threshold_bits:
        return DedupVerdict(
            True, "visual",
            f"image dHash distance {hamming_distance} <= {threshold_bits} bits",
        )
    return DedupVerdict(False, None, "")


def content_fingerprint(text: str | None) -> str:
    """Standalone exact-dup fingerprint for layers that cannot reuse the
    messages.content_hash column shape (e.g. tests, tools)."""
    normalized = _WS.sub(" ", text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
