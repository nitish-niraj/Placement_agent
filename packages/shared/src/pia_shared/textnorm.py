"""Name normalization for identity matching (FR-ELG-005).

Canonical normalization applied to profile aliases AND candidate-list names so
stage-2 (NORMALIZED) matching compares like with like: Unicode NFKC, casefold,
punctuation → space, whitespace collapse, honorific/suffix trim.
"""

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_HONORIFICS = {"mr", "mrs", "ms", "dr", "shri", "smt", "md"}


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _WS.sub(" ", text).split(" ") if t]
    tokens = [t for t in tokens if t not in _SUFFIXES and t not in _HONORIFICS]
    return " ".join(tokens)


def digits_only(value: str | None) -> str:
    """Identifier comparison form: digits only (roll/registration numbers)."""
    if not value:
        return ""
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", value))
