"""ADR-011 Stage 1 — the tool-using answer agent (read-only).

A bounded ReAct loop over a registry of read-only tools; every tool wraps
existing deterministic SQL (the agent decides what to LOOK AT, code decides
what is TRUE — ADR-004). Hard boundaries: tools never mutate state
(ADR-003/SEC-005), answers restate tool results only, and every failure
degrades to the existing P12 single-shot ladder (never breaks)."""
