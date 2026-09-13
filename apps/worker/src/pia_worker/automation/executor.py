"""P14.1 — the form submit executor: the ONLY path from an approved draft to
the real world (ADR-008, SEC-004/005).

Trigger: the owner clicks ✓ Approve on a form_draft (routes/actions.py) and
FORM_AUTOMATION_ENABLED is on — the API enqueues this job. Flow:

  APPROVED -> EXECUTING -> SUCCEEDED | FAILED   (§10.4 machine, audited)

Safety rails:
- FORM_AUTOMATION_ENABLED=false disables everything (default; the per-
  capability switch ADR-012 turns on for form submission only).
- FORM_SUBMIT_DRY_RUN=true (default) fills the form, screenshots it to
  Telegram and reports — but never clicks Submit and never transitions the
  draft; the owner verifies one real form, then flips the flag.
- Only feedback-form drafts are touched: the real post-shortlist KYC is
  always manual (DEC-008) and never reaches this code.
- Questions that can't be filled smartly are reported, never guessed; a
  required field without a value BLOCKS submission and asks for manual help.
"""

import json

import sqlalchemy
import structlog

from pia_shared.crypto import decrypt
from pia_shared.enums import ActionStatus
from pia_shared.states import assert_valid_transition
from pia_worker.automation import fields, gform
from pia_worker.db import engine_for_current_host as _engine_for_current_host
from pia_worker.settings import get_settings
from pia_worker.teams.listener import _telegram_send

logger = structlog.get_logger()

_SENSITIVE = ("roll_number", "registration_number", "student_id")


def _read_prefill(engine) -> dict | None:
    """The owner's profile, decrypted for filling (SEC-002 read path)."""
    with engine.connect() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT p.canonical_name, p.roll_number, p.registration_number, "
                "p.student_id, p.branch, p.batch, p.cgpa, "
                "p.tenth_percent, p.twelfth_percent, p.backlog_count, u.email "
                "FROM candidate_profiles p JOIN users u ON u.id = p.user_id LIMIT 1"
            )
        ).mappings().first()
    if row is None:
        return None
    data = dict(row)
    secret = get_settings().pia_encryption_key
    for field in _SENSITIVE:
        data[field] = decrypt(data[field], secret)
    return data


def _load_action(engine, action_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            sqlalchemy.text(
                "SELECT id, type, status::text AS status, target, payload "
                "FROM actions WHERE id = CAST(:id AS uuid)"
            ),
            {"id": action_id},
        ).mappings().first()
    return dict(row) if row else None


def _transition(engine, action_id: str, to_status: ActionStatus,
                reason: str) -> None:
    with engine.begin() as conn:
        current = conn.execute(
            sqlalchemy.text(
                "SELECT status::text AS status FROM actions "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": action_id},
        ).scalar()
        assert_valid_transition("action", ActionStatus(current), to_status)
        conn.execute(
            sqlalchemy.text(
                "UPDATE actions SET status = :status, updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"status": to_status.value, "id": action_id},
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO audit_logs (actor, action, entity_type, entity_id, "
                "result, metadata) VALUES ('executor', :action, 'action', "
                "CAST(:id AS uuid), 'ok', CAST(:meta AS jsonb))"
            ),
            {"action": f"action.{to_status.value.lower()}", "id": action_id,
             "meta": json.dumps({"to": to_status.value, "reason": reason})},
        )


def _run_form(form_url: str, bag: dict[str, str], submit_allowed: bool) -> dict:
    """Open the form, map every question smartly, fill what has values,
    screenshot the result, and (only when allowed) submit. All browser work
    lives here; everything else in the executor is policy and bookkeeping."""
    from playwright.sync_api import sync_playwright

    report: dict = {"filled": [], "skipped": [], "submitted": False,
                    "screenshot": None, "note": ""}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(form_url, timeout=30000, wait_until="domcontentloaded")
            questions = gform.read_questions(page)
            by_index = {q.index: q for q in questions}
            decisions = fields.decide_fills(
                questions, bag, fields.llm_map(questions, bag))
            for decision in decisions:
                if decision.status != "filled":
                    report["skipped"].append(
                        f"{decision.question[:80]} ({decision.status})")
                    continue
                try:
                    assert decision.value is not None  # guaranteed by "filled"
                    if gform.fill_question(page, by_index[decision.index],
                                           decision.value):
                        report["filled"].append(decision.question[:80])
                    else:
                        report["skipped"].append(
                            f"{decision.question[:80]} (option not found)")
                except Exception as exc:  # noqa: BLE001 — one field never fails the run
                    logger.warning("form_fill_failed",
                                   question=decision.question[:60],
                                   error=str(exc)[:120])
                    report["skipped"].append(
                        f"{decision.question[:80]} (fill error)")
            blocked = [d for d in decisions if d.required and d.status != "filled"]
            if blocked:
                report["note"] = "required fields missing: " + "; ".join(
                    d.question[:60] for d in blocked)
            report["screenshot"] = page.screenshot(full_page=True)
            if submit_allowed and not blocked:
                report["submitted"] = gform.submit(page)
        finally:
            browser.close()
    return report


def submit_form_action(action_id: str) -> str:
    """RQ entry: fill + (maybe) submit one approved form draft. Returns a
    short outcome string; every terminal state is audited and messaged."""
    settings = get_settings()
    if not settings.form_automation_enabled:
        logger.warning("form_submit_disabled", action_id=action_id)
        return "disabled"
    engine = _engine_for_current_host()
    row = _load_action(engine, action_id)
    if row is None:
        return "action_missing"
    if row["type"] != "form_draft":
        return "skipped_type"
    if row["status"] != ActionStatus.APPROVED.value:
        return "skipped_state"

    payload = row["payload"] or {}
    form_url = str(payload.get("form_url") or row["target"] or "")
    if not form_url:
        return "skipped_no_link"
    bag = fields.build_value_bag(_read_prefill(engine), payload)
    dry_run = settings.form_submit_dry_run

    if not dry_run:
        _transition(engine, action_id, ActionStatus.EXECUTING, "approved draft")
    try:
        report = _run_form(form_url, bag, submit_allowed=not dry_run)
    except Exception as exc:  # noqa: BLE001 — browser failures are terminal + loud
        logger.error("form_submit_error", action_id=action_id,
                     error=str(exc)[:180])
        if not dry_run:
            _transition(engine, action_id, ActionStatus.FAILED,
                        f"driver error: {str(exc)[:120]}")
        _telegram_send(
            text=f"❌ <b>Form robot failed</b> — fill it manually: {form_url}\n"
                 f"Error: {str(exc)[:200]}")
        return "failed"

    skipped_note = "\n".join(f"⚠️ {s}" for s in report["skipped"][:6])
    if dry_run:
        _telegram_send(
            text=f"🧪 <b>Dry run</b> — filled {len(report['filled'])} field(s), "
                 f"nothing submitted.\n{skipped_note}\n{report['note']}\n"
                 "Looks right? Set FORM_SUBMIT_DRY_RUN=false to let the robot "
                 "click Submit on the next approved draft.")
        if report["screenshot"]:
            _telegram_send(photo=report["screenshot"], caption="Dry-run preview")
        logger.info("form_dry_run", action_id=action_id,
                    filled=len(report["filled"]), skipped=len(report["skipped"]))
        return "dry_run"

    if report["submitted"]:
        _transition(engine, action_id, ActionStatus.SUCCEEDED, "form submitted")
        _telegram_send(text=(
            "✅ <b>Form submitted</b> for you (approved draft)."
            + (f"\n{skipped_note}" if skipped_note else "")))
        if report["screenshot"]:
            _telegram_send(photo=report["screenshot"], caption="Submission proof")
        logger.info("form_submitted", action_id=action_id)
        return "submitted"

    reason = report["note"] or "submission not confirmed"
    _transition(engine, action_id, ActionStatus.FAILED, reason)
    _telegram_send(
        text=f"❌ <b>Couldn't auto-submit</b> — {reason}\n"
             f"Fill manually: {form_url}\n{skipped_note}")
    if report["screenshot"]:
        _telegram_send(photo=report["screenshot"], caption="Where it stopped")
    logger.warning("form_submit_failed", action_id=action_id, reason=reason[:120])
    return "failed"
