"""Borrowed-company provenance: record gates its bundle but never activates
the company-wide watch; direct provenance behaves as before."""

import contextlib

from pia_shared.enums import EligibilityState, MatchMethod, MatchStatus
from pia_shared.schemas import EvidenceRef
from pia_worker.eligibility import records as rec
from pia_worker.eligibility.matcher import ListOutcome, MatchThresholds


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar


class FakeConn:
    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(str(sql))
        _ = params
        return FakeResult(scalar="rec-uuid")


class FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def begin(self):
        return contextlib.nullcontext(self._conn)


def _outcome() -> ListOutcome:
    return ListOutcome(
        status=MatchStatus.MATCHED, state=EligibilityState.MATCHED,
        match_method=MatchMethod.IDENTIFIER, confidence=0.99,
        requires_user_review=False,
        evidence=[EvidenceRef(kind="row", location="S!row 1", quote="me")],
        rationale="identifier match", row_results=[],
    )


def _thresholds() -> MatchThresholds:
    return MatchThresholds(fuzzy=0.95, ambiguous=0.90, confidence_floor=0.75)


def _run(monkeypatch, provenance: str) -> tuple[FakeConn, list]:
    conn = FakeConn()
    calls: list = []
    monkeypatch.setattr(
        rec, "activate_watch",
        lambda c, company_id: calls.append(company_id))
    # find_existing_record is checked by the caller, not persist_outcome.
    rec.persist_outcome(
        conn, user_id="u", extraction_id="e", message_id="m",
        company_id="c", outcome=_outcome(), thresholds=_thresholds(),
        company_provenance=provenance)
    return conn, calls


def test_direct_provenance_activates_watch(monkeypatch) -> None:
    conn, calls = _run(monkeypatch, "direct")
    assert calls == ["c"]
    assert any("INSERT INTO eligibility_records" in s for s in conn.statements)


def test_borrowed_provenance_skips_watch(monkeypatch) -> None:
    conn, calls = _run(monkeypatch, "bundle_borrowed")
    assert calls == []
    assert any("INSERT INTO eligibility_records" in s for s in conn.statements)
