"""Message topics: stage routing without touching frozen PG enums.

Critical invariants: bare "test" never counts as system noise (OA
announcements must survive); defaulter language beats registration
language; feedback forms are surveys, not form events.
"""

from pia_worker.notify.topics import (
    MessageTopic,
    classify_topic,
    is_system_test,
)


class TestSystemTestFilter:
    def test_watcher_chatter_ignored(self) -> None:
        for text in ("watcher self-test", "ALERT-TEST ping",
                     "Dry run of the digest job", "health-check ok",
                     "please ignore this dummy message"):
            assert is_system_test(text), text

    def test_bare_test_survives(self) -> None:
        for text in ("Online assessment on Friday — test link will be shared.",
                     "OA test credentials mailed.",
                     "Shortlisted candidates: test on Monday 10 AM."):
            assert not is_system_test(text), text
            assert classify_topic(text) in (
                MessageTopic.ONLINE_TEST, MessageTopic.SHORTLIST)


class TestStageRouting:
    def test_defaulter_beats_registration(self) -> None:
        assert classify_topic(
            "List of the defaulters who are yet not registered on the "
            "MARS platforms.") is MessageTopic.DEFAULTER_CHECK

    def test_survey_beats_form(self) -> None:
        assert classify_topic(
            "Please fill this feedback form about yesterday's drive."
        ) is MessageTopic.SURVEY

    def test_shortlist(self) -> None:
        assert classify_topic(
            "Shortlisted students for Tohobo must report tomorrow."
        ) is MessageTopic.SHORTLIST

    def test_interview(self) -> None:
        assert classify_topic(
            "Interview reporting venue 29-402 at 9 AM."
        ) is MessageTopic.INTERVIEW

    def test_online_test(self) -> None:
        assert classify_topic(
            "AMCAT slots booked for Monday.") is MessageTopic.ONLINE_TEST

    def test_documents(self) -> None:
        assert classify_topic(
            "Carry all documents and photographs tomorrow."
        ) is MessageTopic.DOCUMENT_SUBMISSION

    def test_registration(self) -> None:
        assert classify_topic(
            "Register on the portal — last date 27 Sep."
        ) is MessageTopic.REGISTRATION

    def test_opportunity(self) -> None:
        assert classify_topic(
            "BANGMETRIC hiring SDE interns, CTC 4.25 LPA."
        ) is MessageTopic.PLACEMENT_OPPORTUNITY

    def test_training(self) -> None:
        assert classify_topic(
            "Hackathon this weekend in the seminar hall."
        ) is MessageTopic.TRAINING_SESSION

    def test_empty_is_unknown(self) -> None:
        assert classify_topic("") is MessageTopic.UNKNOWN

    def test_plain_sentence_is_informational(self) -> None:
        assert classify_topic(
            "Congrats to all placed students.") is MessageTopic.INFORMATIONAL
