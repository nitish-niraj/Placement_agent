"""Form-provider detection: both the API prefill endpoint and the worker
propose path classify resolved URLs identically (pure string logic)."""

from pia_shared import formlinks as fl


class TestDetect:
    def test_google(self) -> None:
        assert fl.detect_form_provider(
            "https://docs.google.com/forms/d/e/ABC/viewform") == fl.GOOGLE

    def test_microsoft_shapes(self) -> None:
        assert fl.detect_form_provider(
            "https://forms.office.com/r/AbCdEf123") == fl.MICROSOFT
        assert fl.detect_form_provider(
            "https://forms.office.com/Pages/ResponsePage.aspx?id=xyz"
        ) == fl.MICROSOFT

    def test_microsoft_homepage_is_not_a_form(self) -> None:
        assert fl.detect_form_provider("https://forms.office.com/") == fl.UNKNOWN

    def test_glide(self) -> None:
        assert fl.detect_form_provider(
            "https://forms.glide.com/f/abc") == fl.GLIDE
        assert fl.detect_form_provider(
            "https://myapp.glide.page/dl/xyz") == fl.GLIDE

    def test_teams_never_a_form(self) -> None:
        assert fl.detect_form_provider(
            "https://teams.microsoft.com/dl/launcher/launcher.html?url=x"
        ) == fl.TEAMS

    def test_unknown_and_garbage(self) -> None:
        assert fl.detect_form_provider("https://example.com/form") == fl.UNKNOWN
        assert fl.detect_form_provider("not a url") == fl.UNKNOWN
        assert fl.detect_form_provider("") == fl.UNKNOWN
