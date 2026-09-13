"""Ask PIA over Telegram — offline unit tests: owner gating (fail-closed),
answer rendering (HTML-escape, footer, 4096 cap), /start help, offset advance.
The httpx client is a MockTransport recorder; the answer function is a fake —
nothing ever touches api.telegram.org, Redis, or an LLM."""

import json

import httpx
import pytest

from pia_worker import telegram_ask as ta


def _recording_client(recorder: list) -> httpx.Client:
    """Bot API stub: records (method, payload) per call, answers ok."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads((request.content or b"{}").decode() or "{}")
        recorder.append((method, body))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _update(update_id: int, chat_id: str, text: str) -> dict:
    return {"update_id": update_id,
            "message": {"chat": {"id": int(chat_id)}, "text": text}}


def _answer_fn(answer="Yes — SOFTLINK.", **overrides):
    result = {"answer": answer, "citations": [
        {"kind": "message", "ref": "abcd1234-0000",
         "quote": "eligible for the drive *SOFTLINK*"}],
        "says_unavailable": False, "confidence": 0.8,
        "fallback": False, "source": "nim"}
    result.update(overrides)
    calls: list[str] = []

    def fn(_question: str) -> dict:
        calls.append(_question)
        return result

    fn.calls = calls  # type: ignore[attr-defined]
    fn.result = result  # type: ignore[attr-defined]
    return fn


def _sends(recorder: list) -> list[tuple[str, dict]]:
    return [(m, b) for m, b in recorder if m == "sendMessage"]


OWNER = "111111111"


class TestOwnerGating:
    def test_foreign_chat_never_answered(self) -> None:
        recorder: list = []
        answer_fn = _answer_fn()
        ta._handle_update(_recording_client(recorder), "tok", OWNER,
                          _update(1, "999999999", "was i eligible for X?"),
                          answer_fn)
        assert _sends(recorder) == []
        assert answer_fn.calls == []

    def test_owner_question_is_answered(self) -> None:
        recorder: list = []
        answer_fn = _answer_fn()
        ta._handle_update(_recording_client(recorder), "tok", OWNER,
                          _update(1, OWNER, "was i eligible for the softlink?"),
                          answer_fn)
        assert answer_fn.calls == ["was i eligible for the softlink?"]
        methods = [m for m, _ in recorder]
        assert methods[0] == "sendChatAction"  # typing indicator first
        sends = _sends(recorder)
        assert len(sends) == 2  # answer + sources
        text = sends[0][1]["text"]
        assert "Yes — SOFTLINK." in text
        assert "confidence 80%" in text
        assert sends[0][1]["parse_mode"] == "HTML"
        assert "abcd1234" in sends[1][1]["text"]  # source ref prefix

    def test_answer_html_is_escaped(self) -> None:
        recorder: list = []
        ta._handle_update(_recording_client(recorder), "tok", OWNER,
                          _update(1, OWNER, "question?"),
                          _answer_fn(answer="uses <b> tags & symbols"))
        text = _sends(recorder)[0][1]["text"]
        assert "<b>" not in text
        assert "&lt;b&gt; tags &amp; symbols" in text


class TestCommandsAndLimits:
    def test_start_sends_help_without_answering(self) -> None:
        recorder: list = []

        def boom(question: str) -> dict:
            raise AssertionError("help must not call the answer path")

        ta._handle_update(_recording_client(recorder), "tok", OWNER,
                          _update(1, OWNER, "/start"), boom)
        sends = _sends(recorder)
        assert len(sends) == 1
        assert "Ask PIA" in sends[0][1]["text"]

    def test_short_text_gets_help(self) -> None:
        recorder: list = []
        ta._handle_update(_recording_client(recorder), "tok", OWNER,
                          _update(1, OWNER, "hi"), _answer_fn())
        assert "Ask PIA" in _sends(recorder)[0][1]["text"]

    def test_answer_truncated_to_telegram_limit(self) -> None:
        recorder: list = []
        ta._handle_update(_recording_client(recorder), "tok", OWNER,
                          _update(1, OWNER, "question?"),
                          _answer_fn(answer="x" * 6000, confidence=0.5))
        text = _sends(recorder)[0][1]["text"]
        assert len(text) <= ta._MESSAGE_LIMIT
        assert "confidence 50%" in text  # footer survives truncation

    def test_answer_failure_sends_apology_not_crash(self) -> None:
        recorder: list = []

        def broken(_question: str) -> dict:
            raise RuntimeError("llm exploded")

        ta._handle_update(_recording_client(recorder), "tok", OWNER,
                          _update(1, OWNER, "question?"), broken)
        text = _sends(recorder)[-1][1]["text"]
        assert "try again" in text


class TestOffset:
    def test_offset_advances_past_highest_update_id(self) -> None:
        recorder: list = []
        state = ta.PollState(offset=0)
        ta._process_updates(_recording_client(recorder), "tok", OWNER,
                            [_update(5, OWNER, "one"), _update(6, OWNER, "two")],
                            state, _answer_fn())
        assert state.offset == 7


class TestGating:
    def test_disabled_flag_returns_immediately(self, monkeypatch) -> None:
        from types import SimpleNamespace
        monkeypatch.setattr(
            ta, "get_settings",
            lambda: SimpleNamespace(telegram_ask_enabled=False,
                                    telegram_bot_token="t",
                                    telegram_chat_id=OWNER))
        assert ta.run_polling() is None  # no client built, no API call

    def test_unconfigured_token_idles_instead_of_crash_looping(
        self, monkeypatch,
    ) -> None:
        """Compose `restart: unless-stopped` would spin on a crash; the
        unconfigured bot must enter the idle sleep loop instead."""
        from types import SimpleNamespace
        monkeypatch.setattr(
            ta, "get_settings",
            lambda: SimpleNamespace(telegram_ask_enabled=True,
                                    telegram_bot_token="",
                                    telegram_chat_id=""))
        sleeps: list[float] = []

        def stop_sleeping(seconds: float) -> None:
            sleeps.append(seconds)
            raise KeyboardInterrupt  # escape the idle loop

        monkeypatch.setattr(ta.time, "sleep", stop_sleeping)
        with pytest.raises(KeyboardInterrupt):
            ta.run_polling()
        assert sleeps == [3600]

    def test_run_polling_answers_and_acks_offset(self, monkeypatch) -> None:
        """One full loop turn: getUpdates -> answer -> _save_offset, then the
        escape (KeyboardInterrupt is BaseException — not swallowed by the
        poll's transient-error handler)."""
        from types import SimpleNamespace
        monkeypatch.setattr(
            ta, "get_settings",
            lambda: SimpleNamespace(telegram_ask_enabled=True,
                                    telegram_bot_token="tok",
                                    telegram_chat_id=OWNER))
        saved: list[int] = []
        monkeypatch.setattr(ta, "_save_offset", lambda o: saved.append(o))
        monkeypatch.setattr(ta, "_load_offset", lambda: 0)

        pending = [_update(5, OWNER, "was i eligible for the softlink?")]
        answer_fn = _answer_fn()
        recorder: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            body = json.loads((request.content or b"{}").decode() or "{}")
            recorder.append((method, body))
            if method == "getUpdates":
                if pending:
                    batch = list(pending)
                    pending.clear()
                    return httpx.Response(200, json={"ok": True,
                                                     "result": batch})
                raise KeyboardInterrupt("escape loop")
            return httpx.Response(200, json={"ok": True, "result": {}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(KeyboardInterrupt):
            ta.run_polling(client=client, answer_fn=answer_fn)
        assert answer_fn.calls == ["was i eligible for the softlink?"]
        assert saved == [6]  # highest update_id + 1 persisted
        assert any(m == "sendMessage" for m, _ in recorder)
