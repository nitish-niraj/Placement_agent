"""ADR-011 Stage 2 — the proactive reviewer, fully offline.

The propose handler is the reviewer's ONE mutating tool: allowlisted types,
required evidence fields, per-run cap, and the shared dedup/state pipeline.
The daily run must degrade silently on a bad LLM day and summarize to
Telegram on a good one."""

from types import SimpleNamespace

import pytest

from pia_worker.agent import reviewer as rev


@pytest.fixture
def proposes(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(rev, "_propose",
                        lambda target, payload, action_type="form_draft":
                        calls.append({"target": target, "payload": payload,
                                      "action_type": action_type})
                        or "proposed")
    return calls


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list:
    out: list = []
    monkeypatch.setattr(rev, "_telegram_send", lambda **kw: out.append(kw) or True)
    return out


def _settings(enabled: bool = True):
    return SimpleNamespace(reviewer_enabled=enabled)


class TestProposeHandler:
    def test_valid_proposal_reaches_the_pipeline(
        self, monkeypatch: pytest.MonkeyPatch, proposes: list[dict]
    ) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings())
        handler = rev._make_propose_handler([])
        out = handler(type="deadline_nudge", target="event:abc",
                      title="CODEHOOD tomorrow",
                      reason="due Sep 13 18:30 IST, no registration seen")
        assert out["outcome"] == "proposed"
        assert proposes[0]["action_type"] == "deadline_nudge"
        assert proposes[0]["payload"]["source"] == "stage2_reviewer"
        assert "18:30" in proposes[0]["payload"]["reason"]

    def test_non_allowlisted_type_rejected(
        self, monkeypatch: pytest.MonkeyPatch, proposes: list[dict]
    ) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings())
        handler = rev._make_propose_handler([])
        out = handler(type="auto_submit_everything", target="x", reason="r")
        assert "error" in out
        assert proposes == []

    def test_missing_evidence_rejected(
        self, monkeypatch: pytest.MonkeyPatch, proposes: list[dict]
    ) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings())
        handler = rev._make_propose_handler([])
        assert "error" in handler(type="follow_up", target="x", reason="")
        assert "error" in handler(type="follow_up", target="  ", reason="r")
        assert proposes == []

    def test_capped_at_three(self, monkeypatch: pytest.MonkeyPatch,
                             proposes: list[dict]) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings())
        handler = rev._make_propose_handler([])
        for i in range(3):
            out = handler(type="follow_up", target=f"t{i}", reason="r")
            assert out["outcome"] == "proposed"
        assert "error" in handler(type="follow_up", target="t4", reason="r")
        assert len(proposes) == 3


class TestDailyReview:
    def _wire_run(self, monkeypatch: pytest.MonkeyPatch, proposals: int,
                  raise_error: Exception | None = None) -> None:
        def fake_run(question, *, system=None, extra_tools=None,
                     max_steps=None):
            if raise_error:
                raise raise_error
            handler = extra_tools["propose_action"][1]
            for i in range(proposals):
                handler(type="deadline_nudge", target=f"event:{i}",
                        title=f"Proposal {i}", reason="evidence seen in tools")
            return {"answer": f"proposed {proposals} items", "citations": [],
                    "steps": [{"step": 1, "thought": "t", "tool":
                               "get_deadlines", "args": {}}],
                    "source": "agent", "confidence": 0.7, "fallback": False}

        monkeypatch.setattr(rev, "run_agent", fake_run)

    def test_success_proposes_and_summarizes(
        self, monkeypatch: pytest.MonkeyPatch, proposes: list[dict],
        sent: list
    ) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings())
        self._wire_run(monkeypatch, proposals=2)
        result = rev.daily_review()
        assert result["status"] == "ok"
        assert len(result["proposals"]) == 2
        assert len(proposes) == 2
        assert any("Daily review" in str(kw.get("text", "")) for kw in sent)
        assert any("Proposal 0" in str(kw.get("text", "")) for kw in sent)

    def test_disabled_flag(self, monkeypatch: pytest.MonkeyPatch,
                           proposes: list[dict], sent: list) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings(False))
        assert rev.daily_review() == {"status": "disabled"}
        assert proposes == [] and sent == []

    def test_llm_failure_skips_silently(
        self, monkeypatch: pytest.MonkeyPatch, proposes: list[dict],
        sent: list
    ) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings())
        self._wire_run(monkeypatch, proposals=0,
                       raise_error=RuntimeError("nim 503"))
        result = rev.daily_review()
        assert result["status"] == "skipped"
        assert proposes == [] and sent == []  # no noise on a bad LLM day

    def test_zero_findings_is_a_clean_no_op(
        self, monkeypatch: pytest.MonkeyPatch, proposes: list[dict],
        sent: list
    ) -> None:
        monkeypatch.setattr(rev, "get_settings", lambda: _settings())
        self._wire_run(monkeypatch, proposals=0)
        result = rev.daily_review()
        assert result["status"] == "ok"
        assert result["proposals"] == []
        assert any("0 proposal(s)" in str(kw.get("text", "")) for kw in sent)
