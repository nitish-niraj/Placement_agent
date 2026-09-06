"""P9 dedup engine — pure decisions (FR-DED-001..003, DEC-009).

Threshold values come from the live-corpus evaluation recorded in
docs/08_P9_Dedup_Evaluation.md: same-company re-announcements >= 0.720,
template-only different-company pairs max 0.705, threshold 0.72."""


import pytest

from pia_shared.dedup import (
    decide_exact_duplicate,
    decide_near_duplicate,
    decide_visual_duplicate,
    normalized_tokens,
    token_jaccard,
)
from pia_shared.metrics import counter_key
from pia_shared.phash import dhash, hamming_hex

THRESHOLD = 0.72  # DEDUP_NEAR_THRESHOLD (docs/08)


class TestExactLayer:
    def test_recent_identical_copy_suppressed(self) -> None:
        verdict = decide_exact_duplicate(0.5, reminder_days=7)
        assert verdict.suppress and verdict.layer == "exact"

    def test_identical_copy_after_seven_days_is_a_reminder(self) -> None:
        verdict = decide_exact_duplicate(8.2, reminder_days=7)
        assert not verdict.suppress
        assert "legitimate reminder" in verdict.reason

    def test_no_prior_copy(self) -> None:
        assert not decide_exact_duplicate(None, 7).suppress

    def test_boundary_exactly_seven_days_is_a_reminder(self) -> None:
        assert not decide_exact_duplicate(7.0, 7).suppress


class TestNearLayer:
    REAL_REMINDER_PAIR = 0.720  # same company, Gentle Reminder rewording
    REAL_TEMPLATE_PAIR = 0.705  # different companies, same template

    def test_same_company_rewording_suppressed(self) -> None:
        verdict = decide_near_duplicate(
            0.795, {"codehood"}, {"codehood"}, THRESHOLD
        )
        assert verdict.suppress and verdict.layer == "near"

    def test_different_companies_never_suppressed(self) -> None:
        verdict = decide_near_duplicate(
            self.REAL_TEMPLATE_PAIR, {"dexian india technologies"}, {"softlink"},
            THRESHOLD,
        )
        assert not verdict.suppress

    def test_different_companies_high_similarity_still_not_suppressed(self) -> None:
        verdict = decide_near_duplicate(0.95, {"dexian"}, {"softlink"}, THRESHOLD)
        assert not verdict.suppress
        assert "different companies" in verdict.reason

    def test_no_company_signal_needs_a_higher_bar(self) -> None:
        below = decide_near_duplicate(THRESHOLD, set(), set(), THRESHOLD)
        assert not below.suppress
        above = decide_near_duplicate(THRESHOLD + 0.05, set(), set(), THRESHOLD)
        assert above.suppress

    def test_below_threshold_is_never_suppressed(self) -> None:
        assert not decide_near_duplicate(0.5, {"a"}, {"a"}, THRESHOLD).suppress


class TestVisualLayer:
    def test_identical_hash_suppressed(self) -> None:
        verdict = decide_visual_duplicate(0)
        assert verdict.suppress and verdict.layer == "visual"

    def test_small_recompression_distance_suppressed(self) -> None:
        assert decide_visual_duplicate(3).suppress

    def test_distinct_image_not_suppressed(self) -> None:
        assert not decide_visual_duplicate(20).suppress

    def test_missing_hash_never_suppresses(self) -> None:
        assert not decide_visual_duplicate(None).suppress


class TestSimilarity:
    def test_identical_and_disjoint(self) -> None:
        assert token_jaccard("OA tomorrow 9 AM", "OA tomorrow 9 AM") == 1.0
        assert token_jaccard("registration open", "venue changed") == 0.0

    def test_empty_text(self) -> None:
        assert token_jaccard("", "anything") == 0.0

    def test_tokens_are_normalized(self) -> None:
        assert normalized_tokens("Dear Students!") == frozenset({"dear", "students"})


class TestDHash:
    def test_deterministic_and_distinct(self) -> None:
        cv2 = pytest.importorskip("cv2")
        import numpy as np

        flat = np.full((64, 64), 128, dtype=np.uint8)
        gradient = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1))
        ok, buf1 = cv2.imencode(".png", flat)
        ok2, buf2 = cv2.imencode(".png", gradient)
        assert ok and ok2
        h1, h2 = dhash(buf1.tobytes()), dhash(buf2.tobytes())
        assert h1 is not None and h2 is not None
        assert h1 == dhash(buf1.tobytes())  # deterministic
        assert hamming_hex(h1, h2) is not None and hamming_hex(h1, h2) > 0

    def test_garbage_bytes_return_none(self) -> None:
        assert dhash(b"not an image") is None
        assert hamming_hex(None, "ab") is None


class TestMetrics:
    def test_counter_key_format(self) -> None:
        assert counter_key("duplicate_suppressed_total", layer="exact") == \
            'pia:metrics:duplicate_suppressed_total{layer="exact"}'
