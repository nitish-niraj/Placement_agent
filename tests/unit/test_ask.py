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

        # Integration-test collection loads real provider keys into the
        # environment, so the ladder rungs MUST be stubbed here — unit tests
        # never touch the network.
        monkeypatch.setattr(
            ask_module, "openrouter_chat",
            lambda s, u, max_tokens=1500: (_ for _ in ()).throw(
                ProviderError("openrouter: down")))
        monkeypatch.setattr(
            ask_module, "groq_chat",
            lambda s, u, max_tokens=1500: (_ for _ in ()).throw(
                ProviderError("groq: down")))
        monkeypatch.setattr(ask_module, "NIMProvider", DeadProvider)
        result = ask_question("when is the accenture OA?")
        assert result["fallback"] is True
        assert result["source"] == "deterministic"
        assert "Accenture" in result["answer"]
        assert len(result["citations"]) == 2  # both matching messages cited
        assert result["citations"][0]["ref"] == "m-1"


class TestQuestionReachesPrompt:
    """Regression (live bug 2026-09-13): the model was never sent the
    question, so it honestly answered "no question was provided"."""

    CONTEXT = TestAskPaths.CONTEXT

    def test_question_prefixes_llm_user_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ask_module, "_engine", lambda: FakeEngine())
        monkeypatch.setattr(ask_module, "retrieve_context",
                            lambda conn, q, p: self.CONTEXT)
        captured: dict = {}

        class StubProvider:
            def complete_structured(self, **kwargs):
                captured.update(kwargs)
                return ConversationalAnswer(answer="ok", confidence=0.9), None

        monkeypatch.setattr(ask_module, "NIMProvider", StubProvider)
        ask_question("was i eligible for the softlink?")
        assert captured["user"].startswith(
            "QUESTION: was i eligible for the softlink?")
        assert "STRUCTURED FACTS" in captured["user"]


class TestAnswerLadder:
    """DEC-010 extended to Ask: NIM -> OpenRouter -> Groq -> deterministic.
    Rung order is locked here, mirroring test_listener_summary.py."""

    CONTEXT = TestAskPaths.CONTEXT

    def _wire(self, monkeypatch: pytest.MonkeyPatch, *, or_fn, groq_fn) -> None:
        monkeypatch.setattr(ask_module, "_engine", lambda: FakeEngine())
        monkeypatch.setattr(ask_module, "retrieve_context",
                            lambda conn, q, p: self.CONTEXT)

        class DeadProvider:
            def complete_structured(self, **kwargs):
                raise ProviderError("nim down")

        monkeypatch.setattr(ask_module, "NIMProvider", DeadProvider)
        monkeypatch.setattr(ask_module, "openrouter_chat", or_fn)
        monkeypatch.setattr(ask_module, "groq_chat", groq_fn)

    def test_nim_failure_lands_on_openrouter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def or_fn(system, user, max_tokens=1500):
            calls.append("openrouter")
            assert "QUESTION:" in user
            return "OpenRouter says: Accenture, yes."

        def groq_fn(system, user, max_tokens=1500):
            raise AssertionError("groq must not run after openrouter answered")

        self._wire(monkeypatch, or_fn=or_fn, groq_fn=groq_fn)
        result = ask_question("was i eligible for the softlink?")
        assert calls == ["openrouter"]
        assert result["source"] == "openrouter"
        assert result["fallback"] is False
        assert result["answer"] == "OpenRouter says: Accenture, yes."
        assert [c["ref"] for c in result["citations"]] == ["m-1", "m-2"]

    def test_openrouter_failure_lands_on_groq(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def or_fn(system, user, max_tokens=1500):
            calls.append("openrouter")
            raise ProviderError("openrouter: 429")

        def groq_fn(system, user, max_tokens=1500):
            calls.append("groq")
            return "Groq says: yes."

        self._wire(monkeypatch, or_fn=or_fn, groq_fn=groq_fn)
        result = ask_question("was i eligible for the softlink?")
        assert calls == ["openrouter", "groq"]
        assert result["source"] == "groq"
        assert result["answer"] == "Groq says: yes."

    def test_all_brains_dead_falls_back_to_deterministic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def dead(system, user, max_tokens=1500):
            raise ProviderError("down")

        self._wire(monkeypatch, or_fn=dead, groq_fn=dead)
        result = ask_question("was i eligible for the softlink?")
        assert result["source"] == "deterministic"
        assert result["fallback"] is True
        assert "Accenture (2026-09-05)" in result["answer"]


class TestDeterministicLatestCompany:
    """Question-aware deterministic rung: 'latest' = newest detected_at from
    the stored eligibility records — never a guess, never the LLM."""

    CONTEXT = {
        "facts": {
            "eligibility": [
                {"company": "SOFTLINK", "state": "ELIGIBLE",
                 "match_method": "IDENTIFIER",
                 "detected_at": "2026-09-12 18:40:00+00:00"},
                {"company": "DEXIAN INDIA TECHNOLOGIES", "state": "ELIGIBLE",
                 "match_method": "IDENTIFIER",
                 "detected_at": "2026-09-10 09:00:00+00:00"},
                {"company": "OLD CORP", "state": "NOT_FOUND",
                 "match_method": None, "detected_at": "2026-09-11"},
            ],
            "events": [], "watched_companies": [],
        },
        "message_matches": [],
        "vector_error": None,
    }

    def test_latest_question_answers_newest_eligible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ask_module, "_engine", lambda: FakeEngine())
        monkeypatch.setattr(ask_module, "retrieve_context",
                            lambda conn, q, p: self.CONTEXT)

        class DeadProvider:
            def complete_structured(self, **kwargs):
                raise ProviderError("nim down")

        monkeypatch.setattr(ask_module, "NIMProvider", DeadProvider)
        monkeypatch.setattr(
            ask_module, "openrouter_chat",
            lambda s, u, max_tokens=1500: (_ for _ in ()).throw(
                ProviderError("openrouter: down")))
        monkeypatch.setattr(
            ask_module, "groq_chat",
            lambda s, u, max_tokens=1500: (_ for _ in ()).throw(
                ProviderError("groq: down")))
        result = ask_question(
            "which was the latest company that i was eligible for?")
        assert ("Latest company you became eligible for: "
                "SOFTLINK (detected 2026-09-12).") in result["answer"]
        assert "DEXIAN INDIA TECHNOLOGIES (2026-09-10)" in result["answer"]
        assert "OLD CORP" not in result["answer"]  # NOT_FOUND is not eligible
