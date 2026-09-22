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


class _FakeBtn:
    """Playwright button stand-in: knows whether it lives in app-shell nav."""

    def __init__(self, in_nav: bool, visible: bool = True,
                 enabled: bool = True) -> None:
        self.in_nav = in_nav
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    def evaluate(self, _js: str) -> int:
        return 1 if self.in_nav else 0

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def click(self, timeout: int | None = None) -> None:
        self.clicked = True


class _FakeLocator:
    def __init__(self, buttons: list[_FakeBtn]) -> None:
        self._buttons = buttons
        self.legacy_clicked = False

    def all(self) -> list[_FakeBtn]:
        return list(self._buttons)

    @property
    def first(self) -> "_FakeLocator":
        return self

    def click(self, timeout: int | None = None) -> None:
        self.legacy_clicked = True

    def count(self) -> int:
        return 1  # Leave button present: still in the meeting


class _FakePage:
    def __init__(self, buttons: list[_FakeBtn]) -> None:
        self.locator = _FakeLocator(buttons)

    def get_by_role(self, _role: str, name=None,
                    exact: bool | None = None) -> _FakeLocator:
        return self.locator

    def wait_for_timeout(self, _ms: int) -> None:
        pass


class TestOpenChatPanel:
    """The chat click must dock the in-meeting panel, never navigate to the
    app-shell Chat page (leaving kills the caption pane and the transcript)."""

    def test_skips_app_shell_nav(self) -> None:
        nav_btn, toolbar_btn = _FakeBtn(in_nav=True), _FakeBtn(in_nav=False)
        page = _FakePage([nav_btn, toolbar_btn])
        L._open_chat_panel(page)  # type: ignore[arg-type]
        assert not nav_btn.clicked
        assert toolbar_btn.clicked
        assert not page.locator.legacy_clicked

    def test_all_in_nav_falls_back_to_legacy(self) -> None:
        page = _FakePage([_FakeBtn(in_nav=True)])
        L._open_chat_panel(page)  # type: ignore[arg-type]
        assert page.locator.legacy_clicked

    def test_no_candidates_falls_back_to_legacy(self) -> None:
        page = _FakePage([])
        L._open_chat_panel(page)  # type: ignore[arg-type]
        assert page.locator.legacy_clicked


class _FakeTextEl:
    def __init__(self, text: str) -> None:
        self._text = text

    def text_content(self) -> str:
        return self._text


class _FakeCaptionPage:
    """query_selector_all dispatch by selector substring."""

    def __init__(self, mapping: dict[str, list[str]], fail: set[str] = frozenset()) -> None:
        self.mapping = mapping
        self.fail = fail

    def query_selector_all(self, selector: str) -> list[_FakeTextEl]:
        if selector in self.fail:
            raise RuntimeError("dom shifted")
        return [_FakeTextEl(t) for t in self.mapping.get(selector, [])]


class TestExtractCaptions:
    def test_speech_captured_and_deduped(self) -> None:
        page = _FakeCaptionPage(
            {"span[data-tid='closed-caption-text']": ["hello class", "hello class"]})
        seen: set[str] = set()
        assert L._extract_captions(page, seen) == ["hello class"]  # type: ignore[arg-type]
        assert L._extract_captions(page, seen) == []  # type: ignore[arg-type]

    def test_ui_chrome_never_transcript(self) -> None:
        page = _FakeCaptionPage(
            {"[data-tid*='caption']": ["Captions will be shown in English (UK).",
                                       "welcome everyone"]})
        seen: set[str] = set()
        assert L._extract_captions(page, seen) == ["welcome everyone"]  # type: ignore[arg-type]

    def test_broad_selectors_catch_unmarked_pane(self) -> None:
        page = _FakeCaptionPage({"[aria-label*='aption']": ["good morning"]})
        assert L._extract_captions(page, set()) == ["good morning"]  # type: ignore[arg-type]

    def test_inventory_counts_and_marks_failures(self) -> None:
        page = _FakeCaptionPage(
            {"span[data-tid='closed-caption-text']": ["a", "b"]},
            fail={"[data-tid*='caption']"})
        inventory = L._caption_inventory(page)  # type: ignore[arg-type]
        assert inventory["span[data-tid='closed-caption-text']"] == 2
        assert inventory["[data-tid*='caption']"] == -1
