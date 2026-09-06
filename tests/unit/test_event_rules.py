"""F-019 event-type detection + deterministic company/venue signals.

Cases mirror REAL group messages captured live (P8 design input)."""

from pia_shared.enums import EventType
from pia_worker.events import rules

REPORTING_MSG = (
    "Dear Students,\n"
    "You have the Reporting Schedule for the selection process of "
    "*RONAK GROUP OF COMPANIES* OC.41702.2027.63186 is as follows:-\n"
    "Reporting Date: 7 Sep 2026\n"
    "Reporting Time: 9:00 AM\n"
    "Reporting Venue: 29-402"
)
ELIGIBILITY_MSG = (
    "This is for the information that you are eligible for the drive "
    "*TECHADEMY LEARNING SOLUTIONS PVT. LTD.* OC.43620.2027.63637"
)


class TestEventTypeDetection:
    def test_all_twelve_types_detectable(self) -> None:
        cases = {
            "KYC session on 12 Sep at the campus": EventType.KYC,
            "Venue changed to 29-402 for the drive": EventType.VENUE,
            "You have been shortlisted the selection process of X": EventType.SHORTLIST,
            "Results declared for the drive X": EventType.RESULT,
            "Offer letters issued, joining next month": EventType.JOINING,
            "Submit your documents at the venue": EventType.DOCUMENT_SUBMISSION,
            "Online Assessment on Friday 11 AM": EventType.OA,
            "Report for the interview round at 9 AM": EventType.INTERVIEW,
            "CAPP401 exam on Monday": EventType.EXAM,
            "Fill and submit the mandatory form before the drive": EventType.FORM,
            "Registration link is open, register now": EventType.REGISTRATION,
        }
        for text, expected in cases.items():
            assert rules.detect_event_type(text) is expected, text

    def test_reporting_schedule_is_interview_not_venue(self) -> None:
        # "Reporting Venue:" is a detail — the event is the reporting itself.
        assert rules.detect_event_type(REPORTING_MSG) is EventType.INTERVIEW

    def test_eligibility_lists_are_not_events(self) -> None:
        assert rules.detect_event_type(ELIGIBILITY_MSG) is None

    def test_chatter_is_not_an_event(self) -> None:
        assert rules.detect_event_type("good morning all") is None
        assert rules.detect_event_type("") is None

    def test_first_rule_wins_on_overlap(self) -> None:
        # KYC outranks everything; OA outranks INTERVIEW.
        assert rules.detect_event_type("KYC before the OA") is EventType.KYC
        assert rules.detect_event_type("OA then interview") is EventType.OA


class TestCompanyFromText:
    def test_bold_marker_drive_code(self) -> None:
        assert rules.extract_company_from_text(
            "*RONAK GROUP OF COMPANIES* OC.41702.2027.63186"
        ) == "RONAK GROUP OF COMPANIES"

    def test_plain_sentence_with_code(self) -> None:
        # No bold markers: the walk must NOT swallow the sentence prose.
        assert rules.extract_company_from_text(
            "You have been shortlisted the selection process of "
            "MOVIDU TECHNOLOGY OC.31947.2027.62717."
        ) == "MOVIDU TECHNOLOGY"

    def test_no_drive_code_no_company(self) -> None:
        assert rules.extract_company_from_text("KYC on 12 Sep") is None


class TestVenueAndContext:
    def test_venue_extraction(self) -> None:
        assert rules.extract_venue(REPORTING_MSG) == "29-402"

    def test_deadline_vs_occurrence_context(self) -> None:
        assert rules.detect_deadline_context("Last date to register: 25/09/2026")
        assert rules.detect_occurrence_context(REPORTING_MSG)
        assert not rules.detect_deadline_context(REPORTING_MSG)


class TestDriveTemplateFields:
    """LPU drive announcements carry labeled details (role/package/location) —
    owner feedback: alerts without the designation/package are not actionable."""

    REAL_TECHADEMY = (
        "Dear Students,\U0001F44F\u0001F44F\n\nThis is for the information that you are "
        "eligible for the drive *TECHADEMY LEARNING SOLUTIONS PVT. LTD. TC.42520.2027."
        "63655*. You have to register on the placement portal.*\n\n"
        "*Last Date of Registration:-* Sep  9 2026 11:00 AM\n\n"
        "*Time Deadline* : 11:00AM\n\n"
        "*Designation :-* *University Market Development Intern*\n\n"
        "*Eligibility :-* *Upto 2 Standing Arrears and No Backlogs*\n\n"
        "*Salary Package :-* Stipend Rs.18000 PM Plus Upto Rs.1500 Incentives\n\n"
        "*Job Location :-* HSR Layout, Bengaluru\n\n"
        "*IMPORTANT NOTE :* Students must carry CV"
    )

    def test_all_labeled_fields_extracted(self) -> None:
        fields = rules.extract_drive_fields(self.REAL_TECHADEMY)
        assert fields["designation"] == "University Market Development Intern"
        assert fields["salary_package"] == "Stipend Rs.18000 PM Plus Upto Rs.1500 Incentives"
        assert fields["job_location"] == "HSR Layout, Bengaluru"
        assert fields["eligibility_note"] == "Upto 2 Standing Arrears and No Backlogs"

    def test_note_tail_is_trimmed(self) -> None:
        fields = rules.extract_drive_fields(self.REAL_TECHADEMY)
        assert "IMPORTANT" not in fields["job_location"]

    def test_plain_message_yields_nothing(self) -> None:
        assert rules.extract_drive_fields("OA tomorrow 9 AM, be on time") == {}

    def test_ctc_variant_label(self) -> None:
        fields = rules.extract_drive_fields(
            "*Designation :-* Business Development Executive\n"
            "*Salary Package :-* CTC Rs. 4 LPA")
        assert fields["designation"] == "Business Development Executive"
        assert fields["salary_package"] == "CTC Rs. 4 LPA"
