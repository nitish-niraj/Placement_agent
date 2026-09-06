"""Regression corpus gate (P3 exit criterion, master §18/§20).

The deterministic rule classifier must clear a quality bar on the fixture corpus
WITHOUT the LLM — the LLM improves on this, never below it (rules win conflicts).
Corpus is synthetic-but-realistic until real group samples are contributed
(tracked as an open item; extend tests/fixtures/regression_messages.json freely).
"""

import json
import pathlib

import pytest

from pia_worker.ai.rules import classify_rules

CORPUS = json.loads(
    (pathlib.Path(__file__).parents[1] / "fixtures" / "regression_messages.json").read_text(
        encoding="utf-8"
    )
)
# Quality bar (P3): rules alone must hit >= 70% exact (domain+importance);
# combined with the LLM path the bar is >= 90% (tracked live, not in unit CI).
RULES_THRESHOLD = 0.70


@pytest.mark.parametrize("case", CORPUS, ids=[c["text"][:30] for c in CORPUS])
def test_case_shape(case: dict) -> None:
    """Corpus sanity: expected values are real enums."""
    MsgDomain_ = __import__("pia_shared.enums", fromlist=["MsgDomain"]).MsgDomain
    Importance_ = __import__("pia_shared.enums", fromlist=["Importance"]).Importance
    assert MsgDomain_(case["domain"])
    assert Importance_(case["importance"])


def test_rules_meet_quality_threshold() -> None:
    hits = 0
    misses = []
    for case in CORPUS:
        domain, importance = classify_rules(case["text"])
        if domain.value == case["domain"] and importance.value == case["importance"]:
            hits += 1
        else:
            misses.append((case["text"][:40], case["domain"], case["importance"],
                           domain.value, importance.value))
    accuracy = hits / len(CORPUS)
    assert accuracy >= RULES_THRESHOLD, (
        f"rule accuracy {accuracy:.0%} < {RULES_THRESHOLD:.0%}; misses: {misses}"
    )
