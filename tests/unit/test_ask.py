"""P12 conversational search — pure behavior: context composition, citation
filtering, deterministic fallback (NIM flakiness must degrade, never fail)."""

import contextlib

import pytest

from pia_shared.schemas import AnswerCitation, ConversationalAnswer
from pia_worker.ai.provider import ProviderError
from pia_worker.search import answer as ask_module
from pia_worker.search.answer import _context_block, ask_question


class FakeEngine:
    """Stands in for the SQLAlchemy engine — the retrieval layer is
    monkeypatched, so no database is touched."""

    def connect(self):
        return contextlib.nullcontext(None)


class TestSchema:
    def test_defaults_and_bounds(self) -> None:
        answer = ConversationalAnswer(answer="something")
        assert answer.citations == []
        assert answer.says_unavailable is False
        with pytest.raises(ValueError):
            ConversationalAnswer(answer="x", confidence=1.5)

    def test_citation_shape(self) -> None:
        citation = AnswerCitation(kind="message", ref="abc", quote="hi")
        assert citation.model_dump(mode="json")["kind"] == "message"


class TestContextBlock:
    def test_composes_facts_and_messages(self) -> None:
        context = {
            "facts": {
                "eligibility": [{"company": "Accenture", "state": "ELIGIBLE",
                                 "match_method": "IDENTIFIER",
                                 "detected_at": "2026-09-05"}],
                "events": [{"company": "Accenture", "type": "OA", "status": "ACTIVE",
                            "designation": "Analyst", "salary_package": None,
                            "job_location": None, "deadline_at": None,
                            "start_at": None}],
                "watched_companies": [{"canonical_name": "Accenture",
                                       "lifecycle_stage": "ELIGIBLE"}],
            },
            "message_matches": [
                {"id": "m-1", "text": "OA on Friday", "group_name": "Placement"},
            ],
        }
        block = _context_block(context)
        assert "eligibility: Accenture = ELIGIBLE" in block
        assert "event: Accenture OA (ACTIVE) — designation: Analyst" in block
        assert "[m-1]" in block

    def test_truncated_to_budget(self) -> None:
        context = {
            "facts": {"eligibility": [], "events": [], "watched_companies": []},
            "message_matches": [
                {"id": str(i), "text": "x" * 500, "group_name": "g"}
                for i in range(20)
            ],
        }
        assert len(_context_block(context)) <= 9100


class TestAskPaths:
    CONTEXT = {
        "facts": {
            "eligibility": [{"company": "Accenture", "state": "ELIGIBLE",
                             "match_method": "IDENTIFIER",
                             "detected_at": "2026-09-05"}],
            "events": [], "watched_companies": [],
        },
        "message_matches": [
            {"id": "m-1", "text": "Accenture OA Friday", "group_name": "Placement"},
            {"id": "m-2", "text": "unrelated", "group_name": "Placement"},
        ],
        "vector_error": None,
    }

    def test_llm_path_citations_filtered_to_known_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ask_module, "_engine", lambda: FakeEngine())
        monkeypatch.setattr(ask_module, "retrieve_context",
                            lambda conn, q, p: self.CONTEXT)

        class StubProvider:
            def complete_structured(self, **kwargs):
                answer = ConversationalAnswer(
                    answer="Accenture OA is Friday.",
                    citations=[
                        AnswerCitation(kind="message", ref="m-1"),
                        AnswerCitation(kind="message", ref="INVENTED"),  # must drop
                    ],
                    confidence=0.8,
                )
                return answer, None

        monkeypatch.setattr(ask_module, "NIMProvider", StubProvider)
        result = ask_question("when is the accenture OA?")
        assert result["fallback"] is False
        assert result["answer"] == "Accenture OA is Friday."
        assert [c["ref"] for c in result["citations"]] == ["m-1"]  # invented dropped

    def test_provider_failure_falls_back_to_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ask_module, "_engine", lambda: FakeEngine())
        monkeypatch.setattr(ask_module, "retrieve_context",
                            lambda conn, q, p: self.CONTEXT)

        class DeadProvider:
            def complete_structured(self, **kwargs):
                raise ProviderError("nim down")

        monkeypatch.setattr(ask_module, "NIMProvider", DeadProvider)
        result = ask_question("when is the accenture OA?")
        assert result["fallback"] is True
        assert "Accenture" in result["answer"]
        assert len(result["citations"]) == 2  # both matching messages cited
        assert result["citations"][0]["ref"] == "m-1"
