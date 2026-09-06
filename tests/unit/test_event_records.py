"""F-019/F-021 canonical keys, F-021 material delta, F-020 sweep decision."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pia_shared.enums import EventType
from pia_worker.events.canonical import canonical_key
from pia_worker.events.records import EventPlan, decide_deadline_state, material_delta

NOW = datetime(2026, 9, 6, 19, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
DUE = datetime(2026, 9, 10, 23, 59, tzinfo=ZoneInfo("Asia/Kolkata"))


def plan(**overrides) -> EventPlan:
    base = dict(
        event_type=EventType.REGISTRATION,
        company_key="accenture",
        action="register",
        deadline_at=DUE,
        links=["https://example.com/apply"],
    )
    base.update(overrides)
    return EventPlan(**base)


class TestCanonicalKey:
    def test_identical_facts_identical_key(self) -> None:
        a = canonical_key("Accenture", EventType.REGISTRATION, "register", DUE,
                          "https://example.com/apply")
        b = canonical_key("accenture", EventType.REGISTRATION, "Register ",
                          DUE, "https://example.com/apply/")
        assert a == b

    def test_changed_deadline_changes_key(self) -> None:
        a = canonical_key("accenture", EventType.REGISTRATION, None, DUE, None)
        b = canonical_key("accenture", EventType.REGISTRATION, None,
                          DUE + timedelta(days=1), None)
        assert a != b

    def test_no_company_and_changed_link_change_key(self) -> None:
        assert canonical_key(None, EventType.OA, None, None, None) != \
            canonical_key("x", EventType.OA, None, None, None)
        assert canonical_key("x", EventType.OA, None, None, "https://a.com") != \
            canonical_key("x", EventType.OA, None, None, "https://b.com")


class TestMaterialDelta:
    def test_identical_reannouncement_is_empty(self) -> None:
        old = plan().payload()
        assert material_delta(DUE, None, old, plan()) == {}

    def test_changed_deadline_is_material(self) -> None:
        delta = material_delta(DUE, None, plan().payload(),
                               plan(deadline_at=DUE + timedelta(days=2)))
        assert delta["deadline_at"]["to"].endswith("+05:30")

    def test_new_deadline_where_none_existed(self) -> None:
        delta = material_delta(None, None, plan().payload(), plan())
        assert delta["deadline_at"]["from"] is None

    def test_new_link_is_material(self) -> None:
        old = plan().payload()
        delta = material_delta(None, None, old,
                               plan(links=["https://example.com/apply",
                                           "https://example.com/new"]))
        assert "https://example.com/new" in delta["links"]["to"]

    def test_venue_change_is_material(self) -> None:
        old = plan().payload()
        delta = material_delta(None, None, old, plan(venue="31-101"))
        assert delta["venue"] == {"from": None, "to": "31-101"}


class TestDeadlineSweepDecision:
    def test_open_becomes_due_soon_inside_first_window(self) -> None:
        due = NOW + timedelta(hours=20)
        assert decide_deadline_state("OPEN", due, NOW, (24, 6, 1)) == "DUE_SOON"

    def test_open_stays_open_outside_window(self) -> None:
        due = NOW + timedelta(days=5)
        assert decide_deadline_state("OPEN", due, NOW, (24, 6, 1)) is None

    def test_past_due_expires_from_any_active_state(self) -> None:
        due = NOW - timedelta(hours=1)
        assert decide_deadline_state("OPEN", due, NOW, (24, 6, 1)) == "EXPIRED"
        assert decide_deadline_state("DUE_SOON", due, NOW, (24, 6, 1)) == "EXPIRED"

    def test_terminal_states_never_change(self) -> None:
        due = NOW + timedelta(days=5)
        for state in ("EXPIRED", "CANCELLED", "COMPLETED"):
            assert decide_deadline_state(state, due, NOW, (24, 6, 1)) is None
