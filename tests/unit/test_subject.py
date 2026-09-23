"""Subject identification: resolved company > filename > topic token > fallback.

Never invents: topic tokens never mint company rows; the fallback is never
the blind string "General".
"""

import contextlib

from pia_worker.companies import subject as subj


class FakeResult:
    def __init__(self, mapping=None, rows=None):
        self._mapping = mapping
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._rows

    def one(self):
        return self._mapping


class FakeConn:
    """resolve_company(create=False) paths: key/alias miss, no companies."""

    def execute(self, sql, params=None):
        text = str(sql)
        if "FROM companies WHERE normalized_key" in text:
            return FakeResult(mapping=None)
        if "FROM company_aliases" in text:
            return FakeResult(mapping=None)
        if "SELECT id, normalized_key FROM companies" in text:
            return FakeResult(rows=[])
        if "canonical_name FROM companies" in text:
            return FakeResult(mapping=None)
        return FakeResult(mapping=None)


class FakeEngine:
    def begin(self):
        return contextlib.nullcontext(FakeConn())


def test_resolved_company_wins() -> None:
    s = subj.identify_subject(FakeConn(), text="whatever",
                              company_id="c-1", canonical="Accenture")
    assert (s.display, s.is_company, s.confidence, s.evidence) == (
        "Accenture", True, 1.0, "resolved_mention")


def test_topic_token_for_mars() -> None:
    assert subj.extract_topic_token(
        "List of the defaulters who are yet not registered on the "
        "MARS platforms") == "MARS"
    s = subj.identify_subject(FakeConn(), text="MARS registration pending")
    assert s.display == "MARS" and not s.is_company
    assert s.evidence == "topic_token"


def test_noise_tokens_skipped() -> None:
    assert subj.extract_topic_token("Meeting at 10 AM IST, note the venue") is None
    assert subj.extract_topic_token("hello world") is None


def test_fallback_is_never_general() -> None:
    s = subj.identify_subject(FakeConn(), text="hello world")
    assert s.display == subj.FALLBACK_DISPLAY
    assert "General" not in s.display
    assert s.confidence == 0.0 and s.evidence == "none"


def test_filename_never_mints_company() -> None:
    executed: list[str] = []

    class WatchConn(FakeConn):
        def execute(self, sql, params=None):
            executed.append(str(sql))
            return super().execute(sql, params)

    s = subj.identify_subject(WatchConn(), text="list attached",
                              file_name="UNKNOWNCO OC.12345.2027.99999.xlsx")
    assert not s.is_company  # no match + create=False
    assert not any("INSERT INTO companies" in sql for sql in executed)
