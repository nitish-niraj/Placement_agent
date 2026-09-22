"""P14.1 smart field mapping — pure logic, no browser: deterministic hints,
LLM plan validation (the model may only choose catalog keys, never invent),
dropdown fuzzy matching, and the required-blocks-submission policy. New form
columns MUST degrade to an explicit 'skipped' status, never a failure."""

from pia_worker.automation import fields as F
from pia_worker.automation.fields import Question


def _q(index: int, text: str, kind: str = "text", required: bool = False,
       options: tuple[str, ...] = ()) -> Question:
    return Question(index=index, text=text, kind=kind, required=required,
                    options=options)


class TestValueBag:
    def test_profile_and_payload_merge(self) -> None:
        profile = {"full_name": "Nitish Kumar", "email": "x@lpu.in",
                   "branch": "MCA", "roll_number": None, "cgpa": ""}
        bag = F.build_value_bag(profile, {"company": "SOFTLINK",
                                          "presenters": ["Dr. Sharma"]})
        assert bag["full_name"] == "Nitish Kumar"
        assert bag["company"] == "SOFTLINK"
        assert bag["teacher"] == "Dr. Sharma"  # first presenter wins
        assert "roll_number" not in bag  # None/empty never enters the bag

    def test_none_company_placeholder_dropped(self) -> None:
        bag = F.build_value_bag(None, {"company": "None", "presenters": []})
        assert "company" not in bag and "teacher" not in bag


class TestHints:
    def test_specific_fields_win(self) -> None:
        bag = F.build_value_bag({"full_name": "N", "registration_number": "R"},
                                {"company": "C", "presenters": ["T"]})
        assert F.map_hint("Registration Number", bag) == "registration_number"
        assert F.map_hint("Name of the faculty who conducted the session",
                          bag) == "teacher"
        assert F.map_hint("Company name", bag) == "company"
        assert F.map_hint("Your good name", bag) == "full_name"
        assert F.map_hint("Email address", bag) == "email"
        assert F.map_hint("Mobile No", bag) == "mobile"
        assert F.map_hint("Contact number", bag) == "mobile"
        assert F.map_hint("Course / Branch", bag) == "branch"

    def test_unmappable_question_has_no_hint(self) -> None:
        assert F.map_hint("How was the session?", {}) is None


class TestDecideFills:
    BAG = F.build_value_bag(
        {"full_name": "Nitish Kumar", "email": "x@lpu.in",
         "registration_number": "12515641", "branch": "MCA"},
        {"company": "SOFTLINK", "presenters": ["Dr. Sharma"]})

    def test_filled_via_hints(self) -> None:
        questions = [
            _q(0, "Your Name", required=True),
            _q(1, "Registration Number", required=True),
            _q(2, "Name of the teacher who conducted the session",
               kind="dropdown"),
            _q(3, "Company"),
        ]
        decisions = F.decide_fills(questions, self.BAG)
        by_index = {d.index: d for d in decisions}
        assert by_index[0].status == "filled"
        assert by_index[1].value == "12515641"
        assert by_index[2].status == "skipped_no_option"  # dropdown, no options read
        assert by_index[3].value == "SOFTLINK"

    def test_new_unknown_column_is_skipped_not_fatal(self) -> None:
        questions = [_q(0, "How was the session? Rate 1-5"),
                     _q(1, "Any other feedback (new column next month)")]
        decisions = F.decide_fills(questions, self.BAG)
        assert all(d.status == "skipped_unmappable" for d in decisions)
        assert not F.blocked_required(decisions)  # optional unknowns never block

    def test_llm_mapping_fills_unmapped_question(self) -> None:
        questions = [_q(2, "Where should we contact you?")]
        plan = {2: "email"}
        decisions = F.decide_fills(questions, self.BAG, llm_plan=plan)
        assert decisions[0].status == "filled"
        assert decisions[0].source == "llm"

    def test_llm_cannot_invent_keys_or_values(self) -> None:
        questions = [_q(0, "Describe the session in your own words")]
        plan = {0: "hobby"}  # not a catalog key
        decisions = F.decide_fills(questions, self.BAG, llm_plan=plan)
        assert decisions[0].status == "skipped_unmappable"

    def test_matched_field_without_value_reports_no_value(self) -> None:
        questions = [_q(0, "Roll Number", required=True)]
        decisions = F.decide_fills(questions, self.BAG)  # no roll_number in bag
        assert decisions[0].status == "skipped_no_value"
        assert F.blocked_required(decisions)  # required + unfilled BLOCKS

    def test_unknown_kind_skipped(self) -> None:
        decisions = F.decide_fills(
            [_q(0, "Signature grid", kind="other")], self.BAG)
        assert decisions[0].status == "skipped_kind"


class TestPickOption:
    def test_exact_and_containment(self) -> None:
        options = ("Dr. Anil Sharma", "Prof. Meena", "Mr. Rao")
        assert F.pick_option(options, "Dr. Anil Sharma") == "Dr. Anil Sharma"
        assert F.pick_option(options, "Sharma") == "Dr. Anil Sharma"

    def test_no_confident_match_returns_none(self) -> None:
        options = ("Prof. Meena", "Mr. Rao")
        assert F.pick_option(options, "Dr. Sharma") is None
        assert F.pick_option((), "anything") is None
