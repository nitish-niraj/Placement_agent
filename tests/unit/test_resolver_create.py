"""Company creation is opt-in: unknown mentions link nothing by default.

Drive-code events, eligibility lists, and fixtures pass create=True; raw
mention text never mints rows. Orphans are reaped by gc_orphan_companies.
"""

import contextlib

import pytest

from pia_worker.companies import mentions, resolver


class FakeResult:
    def __init__(self, value=None, mapping=None, mapping_list=None):
        self._value = value
        self._mapping = mapping
        self._list = mapping_list or []

    def scalar(self):
        return self._value

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._list

    def one(self):
        return self._mapping


class FakeConn:
    def __init__(self, script):
        self._script = script
        self.executed: list = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._script(text, params)


class FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return contextlib.nullcontext(self._conn)

    def begin(self):
        return contextlib.nullcontext(self._conn)


def _misses_everything(sql: str, params: dict | None):
    if "WHERE normalized_key" in sql:
        return FakeResult(mapping=None)
    if "FROM company_aliases" in sql:
        return FakeResult(mapping=None)
    if "SELECT id, normalized_key FROM companies" in sql:
        return FakeResult(mapping_list=[])
    return FakeResult()


class TestCreateDefault:
    def test_unknown_mention_links_nothing_by_default(self) -> None:
        conn = FakeConn(_misses_everything)
        assert resolver.resolve_company(conn, "Some Random Corp") is None
        assert not any("INSERT INTO companies" in sql
                       for sql, _ in conn.executed)

    def test_explicit_create_mints_row(self) -> None:
        def script(sql: str, params: dict | None):
            base = _misses_everything(sql, params)
            if "INSERT INTO companies" in sql:
                from types import SimpleNamespace
                return FakeResult(mapping=SimpleNamespace(
                    id="c-1", canonical_name="Some Random Corp",
                    normalized_key="some random corp",
                    watch_state="NONE"))
            if "INSERT INTO company_aliases" in sql:
                return FakeResult()
            return base

        conn2 = FakeConn(script)
        ref = resolver.resolve_company(conn2, "Some Random Corp",
                                       create=True)
        assert ref is not None and ref.via == "created"

    def test_blank_never_creates(self) -> None:
        conn = FakeConn(_misses_everything)
        assert resolver.resolve_company(conn, "   ", create=True) is None


class TestMentionsSkipUnknown:
    def test_unresolvable_mention_skipped(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mentions, "resolve_company",
                            lambda conn, raw, create=False: None)
        conn = FakeConn(lambda sql, params: FakeResult(mapping=None))
        assert mentions.associate_message_companies(
            conn, "m-1", ["Mystery Corp"]) == []
        assert not any("INSERT INTO memory_records" in sql
                       for sql, _ in conn.executed)


class TestGcOrphans:
    def _script(self, orphans):
        def script(sql: str, params: dict | None):
            if "FROM companies c" in sql:
                return FakeResult(mapping_list=orphans)
            return FakeResult()
        return script

    def test_collects_only_orphans(self, monkeypatch) -> None:
        import pia_worker.jobs.process_message as pm

        orphans = [{"id": "c-old", "canonical_name": "Stale Mention Co"}]
        conn = FakeConn(self._script(orphans))
        monkeypatch.setattr(pm, "_engine", lambda: FakeEngine(conn))
        out = resolver.gc_orphan_companies()
        assert out == {"collected": 1}
        texts = [sql for sql, _ in conn.executed]
        assert any("DELETE FROM company_aliases" in sql for sql in texts)
        assert any("DELETE FROM companies WHERE id" in sql for sql in texts)

    def test_empty_is_zero(self, monkeypatch) -> None:
        import pia_worker.jobs.process_message as pm

        conn = FakeConn(self._script([]))
        monkeypatch.setattr(pm, "_engine", lambda: FakeEngine(conn))
        assert resolver.gc_orphan_companies() == {"collected": 0}
