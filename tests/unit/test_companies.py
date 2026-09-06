"""P7 company memory — pure-logic tests (F-017 containment resolution,
FR-MEM-005 watch bump, FR-MEM-004 lifecycle machine)."""

import pytest

from pia_shared.enums import CompanyLifecycle, Importance
from pia_shared.states import (
    InvalidTransitionError,
    assert_valid_transition,
    can_transition,
)
from pia_worker.companies.mentions import watch_bump
from pia_worker.companies.resolver import find_containment_candidate


class TestContainmentResolution:
    """Stage-3 fallback of the F-017 ladder: qualifier variants of one company
    must resolve to the existing entity (FR-MEM-001: "Accenture"/"Accenture
    India" map to one entity)."""

    COMPANIES = [
        ("11111111-1111-1111-1111-111111111111", "accenture"),
        ("22222222-2222-2222-2222-222222222222", "techademy learning solutions pvt ltd"),
    ]

    def test_subset_mention_resolves(self) -> None:
        # "Techademy" ⊂ "Techademy Learning Solutions Pvt. Ltd."
        assert find_containment_candidate("techademy", self.COMPANIES) == \
            "22222222-2222-2222-2222-222222222222"

    def test_superset_mention_resolves(self) -> None:
        # "Accenture India" ⊃ "Accenture"
        assert find_containment_candidate("accenture india", self.COMPANIES) == \
            "11111111-1111-1111-1111-111111111111"

    def test_qualifier_variant_resolves(self) -> None:
        assert find_containment_candidate(
            "techademy learning solutions private limited", self.COMPANIES
        ) == "22222222-2222-2222-2222-222222222222"

    def test_disjoint_name_does_not_resolve(self) -> None:
        assert find_containment_candidate("infosys", self.COMPANIES) is None

    def test_partial_token_overlap_only_is_not_enough(self) -> None:
        # shares no complete token — "Dexian" vs "Dex" must not collide
        assert find_containment_candidate("dex", self.COMPANIES) is None

    def test_blank_mention(self) -> None:
        assert find_containment_candidate("", self.COMPANIES) is None


class TestWatchPriorityBump:
    """FR-MEM-005: watched-company messages are prioritized automatically; the
    bump is deterministic and never downgrades."""

    def test_low_priority_messages_are_raised_to_high(self) -> None:
        for importance in (Importance.MEDIUM, Importance.LOW, Importance.IGNORE):
            assert watch_bump(importance) is Importance.HIGH, importance

    def test_critical_and_high_are_never_touched(self) -> None:
        assert watch_bump(Importance.CRITICAL) is None
        assert watch_bump(Importance.HIGH) is None


class TestCompanyLifecycleMachine:
    """FR-MEM-004: event-driven forward stages, reversible via correction,
    SELECTED/REJECTED terminal."""

    def test_p7_wired_stages(self) -> None:
        assert_valid_transition("company_lifecycle",
                                CompanyLifecycle.DISCOVERED, CompanyLifecycle.ELIGIBLE)

    def test_forward_path_to_selected(self) -> None:
        path = [
            CompanyLifecycle.ELIGIBLE, CompanyLifecycle.REGISTRATION,
            CompanyLifecycle.OA, CompanyLifecycle.SHORTLISTED,
            CompanyLifecycle.INTERVIEW, CompanyLifecycle.SELECTED,
        ]
        for cur, nxt in zip(path[:-1], path[1:], strict=True):
            assert_valid_transition("company_lifecycle", cur, nxt)

    def test_rejected_is_reachable_from_interview(self) -> None:
        assert_valid_transition("company_lifecycle",
                                CompanyLifecycle.INTERVIEW, CompanyLifecycle.REJECTED)

    def test_every_stage_can_step_back_except_terminals(self) -> None:
        for stage in (CompanyLifecycle.ELIGIBLE, CompanyLifecycle.REGISTRATION,
                      CompanyLifecycle.OA, CompanyLifecycle.SHORTLISTED):
            previous = {
                CompanyLifecycle.ELIGIBLE: CompanyLifecycle.DISCOVERED,
                CompanyLifecycle.REGISTRATION: CompanyLifecycle.ELIGIBLE,
                CompanyLifecycle.OA: CompanyLifecycle.REGISTRATION,
                CompanyLifecycle.SHORTLISTED: CompanyLifecycle.OA,
            }[stage]
            assert can_transition("company_lifecycle", stage, previous)

    def test_terminal_stages_have_no_exits(self) -> None:
        assert not can_transition("company_lifecycle",
                                  CompanyLifecycle.SELECTED, CompanyLifecycle.INTERVIEW)
        assert not can_transition("company_lifecycle",
                                  CompanyLifecycle.REJECTED, CompanyLifecycle.INTERVIEW)

    def test_skip_forward_is_illegal(self) -> None:
        with pytest.raises(InvalidTransitionError):
            assert_valid_transition("company_lifecycle",
                                    CompanyLifecycle.DISCOVERED, CompanyLifecycle.OA)

    def test_unknown_machine_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            assert_valid_transition("company_lifecycles",
                                    CompanyLifecycle.DISCOVERED, CompanyLifecycle.ELIGIBLE)
