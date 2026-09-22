"""P14.1 login-wall detection: org-restricted forms must report loudly
("Google sign-in required") instead of a silent filled-0/skipped-0 dry run."""

from pia_worker.automation import gform


class _Body:
    def __init__(self, text: str) -> None:
        self._text = text

    def text_content(self) -> str:
        return self._text


class _FakePage:
    def __init__(self, url: str, title: str = "", body: str = "") -> None:
        self.url = url
        self._title = title
        self._body = body

    def title(self) -> str:
        return self._title

    def locator(self, _selector: str) -> _Body:
        return _Body(self._body)


class TestLoginWall:
    def test_accounts_url_is_wall(self) -> None:
        page = _FakePage("https://accounts.google.com/v3/signin/identifier?x=1")
        note = gform.login_wall_note(page)
        assert note is not None and "sign-in" in note.lower()

    def test_signin_title_and_compact_body_is_wall(self) -> None:
        # Real sign-in DOM concatenates words without spaces ("Sign into...").
        page = _FakePage(
            "https://docs.google.com/forms/d/e/ABC/viewform",
            title="Google Forms: Sign-in",
            body="Sign into continue to Google FormsEmail or phoneForgot email?",
        )
        assert gform.login_wall_note(page) is not None

    def test_real_form_is_not_wall(self) -> None:
        page = _FakePage(
            "https://docs.google.com/forms/d/e/ABC/viewform",
            title="Accenture Registration Confirmation",
            body="Registration Number * Name *",
        )
        assert gform.login_wall_note(page) is None

    def test_broken_page_never_raises(self) -> None:
        class _Broken:
            url = "https://docs.google.com/forms/d/e/ABC/viewform"

            def title(self) -> str:
                raise RuntimeError("detached")

            def locator(self, _selector: str) -> _Body:
                raise RuntimeError("closed")

        assert gform.login_wall_note(_Broken()) is None
