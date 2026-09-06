"""ADR-009 threshold validation: the committed FUZZY_MATCH_THRESHOLD and
AMBIGUOUS_MATCH_THRESHOLD must separate the P6 fixture corpus exactly as
documented in docs/07_P6_Threshold_Evaluation.md. If this test fails after a
corpus change, re-run the evaluation and update the thresholds + doc together —
never adjust one side silently."""

from pia_shared.textnorm import normalize_name
from pia_worker.eligibility.matcher import name_similarity
from pia_worker.settings import Settings
from tests.fixtures.eligibility_corpus import CORPUS, PROFILE_NAME

# Defaults carry the tuned values (ADR-009 rationale lives in the settings
# comments and docs/07); .env overrides are a deployment concern, not a test one.
_SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]
FUZZY = _SETTINGS.fuzzy_match_threshold
AMBIGUOUS = _SETTINGS.ambiguous_match_threshold


def _similarity(row_name: str) -> float:
    return name_similarity(normalize_name(row_name), PROFILE_NAME)


def test_owner_variants_land_at_or_above_fuzzy_threshold() -> None:
    for item in CORPUS:
        if item["label"] == "me":
            assert _similarity(item["row"]) >= FUZZY, item


def test_unrelated_names_stay_below_ambiguous_threshold() -> None:
    for item in CORPUS:
        if item["label"] == "not_me":
            assert _similarity(item["row"]) < AMBIGUOUS, item


def test_could_be_me_names_stay_in_or_above_the_review_band() -> None:
    for item in CORPUS:
        if item["label"] == "could_be_me":
            assert _similarity(item["row"]) >= AMBIGUOUS, item


def test_thresholds_leave_a_measured_margin() -> None:
    by_label: dict[str, list[float]] = {}
    for item in CORPUS:
        by_label.setdefault(item["label"], []).append(_similarity(item["row"]))
    assert min(by_label["me"]) >= FUZZY
    assert max(by_label["not_me"]) < AMBIGUOUS
    assert AMBIGUOUS < FUZZY


def test_no_threshold_admits_a_not_me_name_into_the_fuzzy_band() -> None:
    for item in CORPUS:
        if item["label"] == "not_me":
            assert _similarity(item["row"]) < FUZZY, item
