"""P13 listener→draft bridge: _relay_form_link must (a) still send the
Telegram relay first, (b) propose the meeting form draft with the detected
presenters, and (c) never die when the DB write fails — the relay already
went out and the session must continue."""

import pytest

from pia_worker.teams import listener as L


@pytest.fixture
def relayed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(L, "_telegram_send",
                        lambda text=None, **_: sent.append(text) or True)
    return sent


def test_relay_proposes_draft_with_presenters(
    monkeypatch: pytest.MonkeyPatch, relayed: list[str]
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        "pia_worker.jobs.propose_actions.propose_meeting_form_action",
        lambda link, presenters=(), company=None: calls.append(
            (link, presenters)) or "proposed")
    L._relay_form_link("https://docs.google.com/forms/d/e/ABC/viewform",
                       presenters=("Dr. Sharma",))
    assert relayed and "KYC feedback form is up" in relayed[0]
    assert calls == [("https://docs.google.com/forms/d/e/ABC/viewform",
                      ("Dr. Sharma",))]


def test_db_failure_never_breaks_the_relay(
    monkeypatch: pytest.MonkeyPatch, relayed: list[str]
) -> None:
    def boom(link, presenters=(), company=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "pia_worker.jobs.propose_actions.propose_meeting_form_action", boom)
    L._relay_form_link("https://docs.google.com/forms/d/e/ABC/viewform")
    assert relayed  # the Telegram relay still went out


def test_relay_without_presenters_still_proposes(
    monkeypatch: pytest.MonkeyPatch, relayed: list[str]
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        "pia_worker.jobs.propose_actions.propose_meeting_form_action",
        lambda link, presenters=(), company=None: calls.append(presenters)
        or "proposed")
    L._relay_form_link("https://forms.office.com/r/xyz")
    assert calls == [()]  # empty presenters tuple, draft proposed anyway
