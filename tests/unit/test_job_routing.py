"""Queue routing: bulk pipeline stays on default, user-facing jobs ride
realtime, worker queue sets parse fail-fast. RQ/Redis are fakes."""

import pytest

from pia_api import jobs as jobs_mod
from pia_shared.queues import parse_queue_names


class FakeRedis:
    @staticmethod
    def from_url(url: str):
        return "redis-conn"


class FakeQueue:
    seen: list[tuple] = []

    def __init__(self, name, connection=None):
        self.name = name
        FakeQueue.seen.append((name, connection))

    def enqueue(self, *args, **kwargs):
        FakeQueue.seen.append(("enqueue", self.name, args[0] if args else None))
        return None


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeQueue.seen = []
    monkeypatch.setattr(jobs_mod, "Queue", FakeQueue)
    monkeypatch.setattr(jobs_mod, "Redis", FakeRedis)


class TestApiRouting:
    def test_bulk_stays_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch)
        jobs_mod.enqueue_process_message("m", "c")
        jobs_mod.enqueue_download_attachment("a")
        jobs_mod.enqueue_form_submit("x")
        queues = [entry[0] for entry in FakeQueue.seen if len(entry) == 2]
        assert queues == ["default", "default", "default"]

    def test_approved_proposal_is_realtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch)
        jobs_mod.enqueue_proposal_executor("x")
        assert ("enqueue", "realtime",
                "pia_worker.executors.proposal_executors.run_proposal_executor"
                ) in FakeQueue.seen


class TestParseQueues:
    def test_order_preserved(self) -> None:
        assert parse_queue_names("realtime,maintenance") == ["realtime",
                                                             "maintenance"]

    def test_blank_means_defaults(self) -> None:
        assert parse_queue_names("") == ["default", "maintenance"]
        assert parse_queue_names(None) == ["default", "maintenance"]

    def test_unknown_name_fails_fast(self) -> None:
        with pytest.raises(ValueError):
            parse_queue_names("default,typo")
