"""ADR-011 Stage 1 read-only tools over a scripted connection: dispatch,
unknown-tool observation, company filtering, size capping. No Postgres."""


from pia_worker.agent import tools


class FakeResult:
    def __init__(self, rows=None, mapping=None):
        self._rows = rows or []
        self._mapping = mapping

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._mapping


class FakeConn:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        text = str(sql)
        self.executed.append((text, params))
        return self._results.pop(0) if self._results else FakeResult()


def test_run_tool_dispatches_search_and_returns_rows() -> None:
    conn = FakeConn([FakeResult(rows=[{"id": "m-1", "text": "eligible SOFTLINK",
                                       "group_name": "g", "sent_at": "now"}])])
    rows = tools.run_tool(conn, "search_messages", {"query": "softlink"})
    assert rows[0]["id"] == "m-1"
    assert rows[0]["text"].startswith("eligible")


def test_unknown_tool_is_an_observation_not_an_error() -> None:
    result = tools.run_tool(FakeConn(), "weather_forecast", {})
    assert "unknown tool" in result["error"]


def test_get_eligibility_filters_by_company() -> None:
    conn = FakeConn([FakeResult(rows=[{"company": "SOFTLINK", "state": "ELIGIBLE"}])])
    rows = tools.get_eligibility(conn, company="softlink")
    sql, params = conn.executed[0]
    assert params["frag"] == "%softlink%"
    assert rows[0]["company"] == "SOFTLINK"


def test_get_deadlines_uppercases_state() -> None:
    conn = FakeConn([FakeResult(rows=[{"state": "DUE_SOON"}])])
    tools.get_deadlines(conn, state="due_soon")
    assert conn.executed[0][1]["state"] == "DUE_SOON"


def test_get_document_bad_id_yields_empty() -> None:
    conn = FakeConn([FakeResult(mapping=None)])
    assert tools.get_document(conn, "00000000-0000-0000-0000-000000000001") == {}


def test_long_text_capped() -> None:
    conn = FakeConn([FakeResult(rows=[{"company": "X" * 500}])])
    rows = tools.get_events(conn)
    assert len(rows[0]["company"]) <= 301
