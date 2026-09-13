"""ADR-011 Stage 2 — the proactive reviewer (proposes, NEVER acts).

Runs daily (after the digest): a bounded agent pass over the read-only tools
with ONE extra capability — propose_action — which files findings as rows in
the `actions` table (PROPOSED -> WAITING_APPROVAL). The owner approves or
rejects on the dashboard. Deterministic executors (Stage 3) remain the only
path to real-world effect (SEC-005); DEC-008 holds (KYC reminders allowed,
automation never).

Guardrails, all in code (ADR-004):
- action types come from a fixed allowlist;
- every proposal needs a target + an evidence reason from tool results;
- max 3 proposals per run, deduplicated per target (shared _propose);
- any failure skips the day silently — the reviewer never breaks anything.
"""

import structlog

from pia_worker.agent.loop import run_agent
from pia_worker.jobs.propose_actions import _propose
from pia_worker.settings import get_settings
from pia_worker.teams.listener import _telegram_send

logger = structlog.get_logger()

REVIEWER_ACTION_TYPES: dict[str, str] = {
    "deadline_nudge": "a deadline is close and follow-up looks missing",
    "verify_field": "a stored record has an NA/missing field worth verifying",
    "data_quality": "something in the stored data looks inconsistent",
    "kyc_reminder": "a KYC session reminder (DEC-008: reminders allowed, "
                    "automation never)",
    "follow_up": "a company/event worth following up on",
}

_MAX_PROPOSALS = 3

REVIEW_GOAL = ("Review today's placement data and propose up to "
               f"{_MAX_PROPOSALS} concrete, evidence-backed items that need "
               "the student's attention.")

REVIEWER_SYSTEM = """You are PIA's daily placement reviewer for ONE student.
Your job: find up to {_MAX_PROPOSALS} concrete items worth the student's
attention TODAY, and propose each one with the propose_action tool.
Where to look (use the read tools first):
- deadlines in OPEN/DUE_SOON state and how close they are;
- eligibility for companies that have no follow-up event yet (no
  registration/OA after becoming eligible);
- events with missing designation or salary_package (verify_field);
- anything inconsistent or stale in the stored data (data_quality).
Rules:
- Every proposal MUST carry evidence you actually saw in a tool result —
  quote it inside `reason`. Never invent companies, dates, or numbers.
- At most {_MAX_PROPOSALS} proposals this run; prefer fewer, sharper ones.
  If nothing deserves attention, propose nothing and say so.
- Finish with final_answer: a 2-3 line summary of what you proposed and why.
propose_action arguments:
- type: one of {types}
- target: what it is about (an event id, a company name, or a URL)
- title: short headline for the dashboard
- reason: the evidence-backed explanation shown to the owner
Stage 3 work fields (the executor runs them ONLY after the owner approves):
- verify_field: also pass event_id, field (one of designation, salary_package,
  job_location, eligibility_note) and value (the corrected value you saw
  evidence for in a tool result — never invented).
- data_quality: also pass message_id (the stored message to re-parse).
Available tools:
{tool_docs}"""

# Fields an approved verify_field executor may rewrite on an event.
CORRECTABLE_FIELDS = ("designation", "salary_package", "job_location",
                      "eligibility_note")


def _make_propose_handler(proposed: list[dict]):
    """The one mutating tool the reviewer gets. Everything is validated in
    code: allowlisted type, required fields, per-run cap, per-target dedup
    (shared _propose), and the §10.4 machine (PROPOSED -> WAITING_APPROVAL).
    Even a hallucinated proposal can only ever become a dashboard row."""

    def propose_action(type: str = "", target: str = "", title: str = "",
                       reason: str = "", event_id: str = "", field: str = "",
                       value: str = "", message_id: str = ""
                       ) -> dict:  # noqa: A002 — LLM-facing name
        if type not in REVIEWER_ACTION_TYPES:
            return {"error": f"type must be one of {sorted(REVIEWER_ACTION_TYPES)}"}
        if not target.strip() or not reason.strip():
            return {"error": "target and reason are required"}
        if len(proposed) >= _MAX_PROPOSALS:
            return {"error": f"proposal limit ({_MAX_PROPOSALS}) reached for this run"}
        payload: dict[str, object] = {
            "source": "stage2_reviewer",
            "proposal_type": type,
            "title": title.strip()[:200] or f"{type}: {target.strip()[:80]}",
            "reason": reason.strip()[:500],
        }
        if type == "verify_field":  # Stage 3: what the correction executor will do
            if not (event_id.strip() and field.strip() and value.strip()):
                return {"error": "verify_field needs event_id, field and value"}
            if field.strip() not in CORRECTABLE_FIELDS:
                return {"error": f"field must be one of {list(CORRECTABLE_FIELDS)}"}
            payload["correction"] = {"event_id": event_id.strip(),
                                     "field": field.strip(),
                                     "value": value.strip()[:200]}
        if type == "data_quality":  # Stage 3: the re-parse executor's input
            if not message_id.strip():
                return {"error": "data_quality needs message_id"}
            payload["reparse_message_id"] = message_id.strip()
        outcome = _propose(target.strip(), payload, action_type=type)
        proposed.append({"type": type, "target": target.strip()[:120],
                         "title": payload["title"], "outcome": outcome})
        logger.info("reviewer_proposed", type=type, target=target.strip()[:80],
                    outcome=outcome)
        return {"outcome": outcome, "type": type}

    return propose_action


def daily_review() -> dict:
    """Scheduled entry (daily, after the digest). Returns a small summary;
    never raises — a bad LLM day skips the review, tomorrow retries."""
    settings = get_settings()
    if not settings.reviewer_enabled:
        logger.info("reviewer_disabled")
        return {"status": "disabled"}
    proposed: list[dict] = []
    extra_tools = {
        "propose_action": (
            "propose one item for the owner's dashboard Approvals screen — "
            "evidence-backed, max 3 per run",
            _make_propose_handler(proposed)),
    }
    try:
        result = run_agent(
            REVIEW_GOAL, system=REVIEWER_SYSTEM, extra_tools=extra_tools,
            max_steps=8)
    except Exception as exc:  # noqa: BLE001 — a bad LLM day skips the review
        logger.warning("reviewer_skipped_today", error=str(exc)[:150])
        return {"status": "skipped", "error": str(exc)[:150]}

    proposals = [p for p in proposed if p["outcome"] == "proposed"]
    summary = result.get("answer", "")
    lines = [f"• {p['title']}" for p in proposals]
    _telegram_send(
        text=f"🧠 <b>Daily review</b> — {len(proposals)} proposal(s) waiting on "
             f"the dashboard (Approvals).\n" + "\n".join(lines)
             + (f"\n\n<i>{summary[:300]}</i>" if summary else ""))
    logger.info("daily_review_done", proposals=len(proposals))
    return {"status": "ok", "proposals": proposals, "summary": summary}
