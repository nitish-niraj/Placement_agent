"""Bundle window + reply-thread expansion (pure SQL helpers, fake conn)."""


from pia_worker import bundles as bd


class FakeResult:
    def __init__(self, mapping=None, mapping_list=None):
        self._mapping = mapping
        self._list = mapping_list or []

    def mappings(self):
        return self

    def first(self):
        return self._mapping

    def all(self):
        return self._list


class FakeConn:
    def __init__(self, script):
        self._script = script

    def execute(self, sql, params=None):
        return self._script(str(sql), params)


def _window_script(anchor, window_ids, parents):
    def script(sql, params=None):
        if "SELECT group_id, sent_at FROM messages" in sql:
            return FakeResult(mapping=anchor)
        if "SELECT id FROM messages" in sql:
            return FakeResult(mapping_list=[{"id": i} for i in window_ids])
        if "SELECT reply_to_message_id FROM messages" in sql:
            target = (params or {}).get("mid")
            return FakeResult(mapping={"reply_to_message_id": parents.get(target)})
        return FakeResult()
    return script


def test_window_ids_returned_in_order() -> None:
    conn = FakeConn(_window_script(
        {"group_id": "g", "sent_at": "t"}, ["m1", "m2"], {}))
    assert bd.resolve_bundle_message_ids(conn, "m1") == ["m1", "m2"]


def test_reply_parent_union_into_bundle() -> None:
    conn = FakeConn(_window_script(
        {"group_id": "g", "sent_at": "t"}, ["m2"], {"m2": "m0", "m0": None}))
    assert bd.resolve_bundle_message_ids(conn, "m2") == ["m2", "m0"]


def test_thread_chain_depth_and_cycle_safe() -> None:
    conn = FakeConn(_window_script(None, [], {}))
    assert bd.resolve_thread_ids(conn, "m") == []
    loop = FakeConn(_window_script(None, [],
                                   {"a": "b", "b": "a"}))
    assert bd.resolve_thread_ids(loop, "a") == ["b"]
    deep = FakeConn(_window_script(
        None, [], {"m5": "m4", "m4": "m3", "m3": "m2", "m2": "m1",
                   "m1": "m0", "m0": None}))
    assert bd.resolve_thread_ids(deep, "m5") == ["m4", "m3", "m2", "m1", "m0"]
    assert bd.resolve_thread_ids(deep, "m5", depth=2) == ["m4", "m3"]
    assert bd.resolve_thread_ids(None, None) == []
