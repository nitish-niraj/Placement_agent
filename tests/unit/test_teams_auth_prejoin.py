"""Auth + prejoin helpers: media classifier, join predicates, session probe,
consent guard, sign-in click, unified login driver. Playwright fakes."""

import json
import time
from types import SimpleNamespace

import pia_worker.teams.auth as A
import pia_worker.teams.prejoin as P
from pia_worker.teams import listener as L


class _FakeBtn:
    def __init__(self, aria="", pressed="", visible=True, enabled=True):
        self._aria = aria
        self._pressed = pressed
        self._visible = visible
        self._enabled = enabled
        self.clicked = 0

    def get_attribute(self, name):
        return {"aria-label": self._aria,
                "aria-pressed": self._pressed}.get(name)

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def click(self, timeout=None):
        self.clicked += 1


class TestMediaClassifier:
    def test_live_action_wins_over_state_substring(self) -> None:
        # Run-9 bug: "Turn camera on" contains "camera on" — must NOT click.
        assert P._media_button_should_click(
            _FakeBtn("Turn camera off")) is True
        assert P._media_button_should_click(
            _FakeBtn("Turn camera on")) is False
        assert P._media_button_should_click(_FakeBtn("Mute mic")) is True
        assert P._media_button_should_click(_FakeBtn("Unmute mic")) is False

    def test_dead_state_phrases(self) -> None:
        assert P._media_button_should_click(
            _FakeBtn("Microphone is off")) is False
        assert P._media_button_should_click(
            _FakeBtn("With camera on")) is True

    def test_pressed_and_bare_labels(self) -> None:
        assert P._media_button_should_click(
            _FakeBtn("Camera", pressed="true")) is True
        assert P._media_button_should_click(
            _FakeBtn("Camera", pressed="false")) is False
        assert P._media_button_should_click(_FakeBtn("Camera")) is True
        assert P._media_button_should_click(_FakeBtn("Leave")) is False
        assert P._media_button_should_click(_FakeBtn("")) is False


class TestPredicates:
    def test_authenticated_join_needs_both_signals(self) -> None:
        assert A.is_authenticated_join("signin=n name=none joinnow=on")
        assert not A.is_authenticated_join("signin=y name=none joinnow=off")
        assert not A.is_authenticated_join("signin=n name='X' joinnow=on")

    def test_verified_session_needs_email(self) -> None:
        state = "signin=n name=none joinnow=on"
        assert A.is_verified_session(state, "…nitish@lpu…", "NITISH@LPU")
        assert not A.is_verified_session(state, "someone else", "NITISH@LPU")
        assert not A.is_verified_session("signin=y name=none", "NITISH@LPU",
                                         "NITISH@LPU")


class TestSessionProbe:
    def _write(self, tmp_path, payload) -> str:
        p = tmp_path / "teams_session.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_missing(self, tmp_path) -> None:
        assert A.session_status(str(tmp_path / "nope.json"))[0] == "missing"

    def test_ok_with_future_cookie(self, tmp_path) -> None:
        path = self._write(tmp_path, {"cookies": [
            {"domain": ".login.microsoftonline.com",
             "expires": time.time() + 9000}]})
        status, reason = A.session_status(path)
        assert status == "ok" and "2h" in reason

    def test_expired_cookie(self, tmp_path) -> None:
        path = self._write(tmp_path, {"cookies": [
            {"domain": ".teams.microsoft.com", "expires": time.time() - 10}]})
        assert A.session_status(path)[0] == "expired"

    def test_no_microsoft_cookie(self, tmp_path) -> None:
        path = self._write(tmp_path, {"cookies": [
            {"domain": ".example.com", "expires": time.time() + 7200}]})
        assert A.session_status(path)[0] == "expired"

    def test_garbage_file_is_expired(self, tmp_path) -> None:
        p = tmp_path / "teams_session.json"
        p.write_text("not json", encoding="utf-8")
        assert A.session_status(str(p))[0] == "expired"


class TestCheckSession:
    def test_missing_returns_login_needed(self, tmp_path) -> None:
        calls: list = []
        out = A.check_session(str(tmp_path / "nope.json"),
                              alert=lambda **kw: calls.append(kw))
        assert out == "login_needed: run pia_worker.teams.login_save first"
        assert calls == []

    def test_expired_alerts_but_allows_attempt(self, tmp_path) -> None:
        p = tmp_path / "teams_session.json"
        p.write_text('{"cookies": []}', encoding="utf-8")
        sent: list = []
        assert A.check_session(str(p),
                               alert=lambda **kw: sent.append(kw)) is None
        assert len(sent) == 1 and "expired" in sent[0]["text"]

    def test_ok_is_silent(self, tmp_path) -> None:
        p = tmp_path / "teams_session.json"
        p.write_text(json.dumps({"cookies": [
            {"domain": ".teams.microsoft.com",
             "expires": time.time() + 9000}]}), encoding="utf-8")
        sent: list = []
        assert A.check_session(str(p),
                               alert=lambda **kw: sent.append(kw)) is None
        assert sent == []

    def test_guest_note(self) -> None:
        sent: list = []
        A.note_guest_join(alert=lambda **kw: sent.append(kw))
        assert len(sent) == 1 and "guest" in sent[0]["text"]


class _FakeLocator:
    def __init__(self, present=True, visible=True, value=""):
        self._present = present
        self._visible = visible
        self._value = value
        self.filled: list = []
        self.clicked = 0

    def count(self):
        return 1 if self._present else 0

    def is_visible(self, timeout=None):
        return self._visible

    def is_enabled(self, timeout=None):
        return True

    @property
    def first(self):
        return self

    def fill(self, value):
        self.filled.append(value)

    def click(self, timeout=None):
        self.clicked += 1

    def input_value(self):
        return self._value

    def text_content(self):
        return ""

    def get_by_role(self, *a, **k):
        return _FakeLocator(present=False)

    def get_by_text(self, *a, **k):
        return _FakeLocator(present=False)

    def locator(self, *a, **k):
        return _FakeLocator(present=False)


class _FakeSigninPage:
    """Pre-join page exposing only the auth-sign-in-link tid."""

    def __init__(self):
        self.link = _FakeLocator()

    @property
    def url(self):
        return "https://teams.live.com/_#/meet/x"

    @property
    def frames(self):
        return []

    def locator(self, selector):
        if "auth-sign-in-link" in selector:
            return self.link
        return _FakeLocator(present=False)

    def get_by_role(self, *a, **k):
        return _FakeLocator(present=False)

    def get_by_text(self, *a, **k):
        return _FakeLocator(present=False)

    def text_content(self, *a, **k):
        return ""


class TestClickSignin:
    def test_tid_link_clicked(self) -> None:
        page = _FakeSigninPage()
        assert A._click_signin(page) is True
        assert page.link.clicked == 1

    def test_no_control_is_false(self) -> None:
        assert A._click_signin(_FakeLocator(present=False)) is False


class _FakeDialogInput:
    def __init__(self, has_input):
        self._has = has_input

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._has else 0

    def is_visible(self):
        return True


class _FakeConsentPage:
    def __init__(self, has_dialog_input):
        self._has = has_dialog_input

    def get_by_text(self, *a, **k):
        class _Head:
            @property
            def first(self):
                return self

            def count(self):
                return 1

            def is_visible(self):
                return True

        return _Head()

    def locator(self, selector):
        return _FakeDialogInput(selector == "[role='dialog'] input"
                                and self._has)

    def query_selector_all(self, selector):
        return []


class TestConsentGuard:
    def test_login_dialog_never_touched(self) -> None:
        # Probe-5 self-sabotage: the sign-in dialog carries the same footer.
        assert A._dismiss_consent(_FakeConsentPage(True), {}) == ""

    def test_plain_notice_without_buttons_is_empty(self) -> None:
        assert A._dismiss_consent(_FakeConsentPage(False), {}) == ""


class TestDriveLoginPage:
    def _settings(self):
        return SimpleNamespace(teams_email="u@lpu", teams_password="pw")

    def test_fills_and_reports(self) -> None:
        from pia_worker.teams import login_save as LS

        said: list = []
        email = _FakeLocator()
        password = _FakeLocator()
        submit = _FakeLocator()

        class _Page:
            def locator(self, selector):
                if "password" in selector:
                    return password
                if "loginfmt" in selector or "email" in selector:
                    return email
                return submit

            def get_by_role(self, *a, **k):
                return _FakeLocator(present=False)

        A.drive_login_page(_Page(), self._settings(), say=said.append)
        assert email.filled == ["u@lpu"]
        assert password.filled == ["pw"]
        assert said == ["login email submitted", "login password submitted"]
        # login_save keeps its thin alias working.
        LS._drive_login_page(_Page(), self._settings())

    def test_listener_reexports(self) -> None:
        assert L._click_signin is A._click_signin
        assert L._drive_signin_dialog is A._drive_signin_dialog
        assert L._prejoin_state is A._prejoin_state
        assert L.drive_login_page is A.drive_login_page
        assert L._turn_media_off_prejoin is P._turn_media_off_prejoin
        assert L._mute_if_live is P._mute_if_live
        assert L._resolve_live_page is P._resolve_live_page
        assert L._safe_wait is P._safe_wait
