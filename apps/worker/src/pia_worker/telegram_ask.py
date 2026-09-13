"""Ask PIA over Telegram (P12 extension): a long-polling bridge between the
owner's Telegram chat and pia_worker.search.answer.ask_question — the same
DEC-010 ladder the dashboard uses, so answers stay source-backed and
deterministic when the LLMs fail.

Posture inherited from the repo's invariants:
- ADR-003: strictly informational — a question can never mutate
  eligibility/events state (ask_question is read-only by construction).
- Fail-closed owner gating (SEC-003 posture): only the chat matching
  TELEGRAM_CHAT_ID is answered; any other chat is logged and ignored.
- No inbound port: getUpdates long polling only (deleteWebhook first so a
  stale webhook can never 409-conflict the poll).

Run: python -m pia_worker.telegram_ask  (compose service `telegram-ask`)."""

import html
import time
from dataclasses import dataclass

import httpx
import redis as redis_lib
import structlog

from pia_worker.search.answer import ask_question
from pia_worker.settings import get_settings

logger = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"
_POLL_SECONDS = 25          # long-poll window per getUpdates call
_MESSAGE_LIMIT = 4096       # Telegram sendMessage hard cap
_MAX_SOURCES = 3
_OFFSET_KEY = "pia:tg:offset"
_HELP_TEXT = (
    "Ask PIA — answers come only from your stored placement data "
    "(cited messages, never invented).\n"
    "Examples:\n"
    "• which companies am I eligible for?\n"
    "• what is the TECHADEMY package and role?\n"
    "• which was the latest company I was eligible for?"
)


@dataclass
class PollState:
    """Offset = next update_id Telegram must deliver (acked = offset-1)."""
    offset: int = 0


def _call(client: httpx.Client, token: str, method: str,
          payload: dict | None = None) -> dict:
    """One Bot API call; raises on transport or Telegram-level failure."""
    response = client.post(f"{TELEGRAM_API}/bot{token}/{method}",
                           json=payload or {})
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(
            f"telegram {method}: {str(body.get('description'))[:120]}")
    return body


def _send(client: httpx.Client, token: str, chat_id: str, text: str) -> None:
    _call(client, token, "sendMessage", {
        "chat_id": chat_id,
        "text": text[:_MESSAGE_LIMIT],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def _render_answer(result: dict) -> str:
    """HTML-escaped answer + footer (confidence, answering rung). The answer
    body is truncated so the footer always survives the 4096 cap."""
    answer = html.escape(str(result.get("answer") or "").strip())
    footer_parts: list[str] = []
    confidence = result.get("confidence")
    if isinstance(confidence, (int, float)):
        footer_parts.append(f"confidence {round(float(confidence) * 100)}%")
    source = str(result.get("source") or "")
    if source and source != "nim":
        footer_parts.append(f"answered via {html.escape(source)}")
    if result.get("fallback"):
        footer_parts.append("deterministic evidence")
    footer = " · ".join(footer_parts)
    text = answer
    if footer:
        footer_text = f"\n\n<i>{html.escape(footer)}</i>"
        text = answer[: _MESSAGE_LIMIT - len(footer_text) - 1] + footer_text
    return text


def _render_sources(result: dict) -> str | None:
    citations = result.get("citations") or []
    lines: list[str] = []
    for c in citations[:_MAX_SOURCES]:
        ref = html.escape(str(c.get("ref") or "")[:8])
        quote = html.escape(str(c.get("quote") or ""))[:120]
        lines.append(f"📄 <code>{ref}</code> — {quote}")
    return "\n".join(lines) if lines else None


def _handle_update(client: httpx.Client, token: str, owner_chat_id: str,
                   update: dict, answer_fn) -> None:
    """One Telegram update -> one answer. Owner-gated, fail-closed: anything
    without text, or from any chat other than the owner's, is dropped."""
    message = update.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not text or not chat_id:
        return
    if chat_id != str(owner_chat_id):
        logger.warning("telegram_ask_foreign_chat_ignored", chat_id=chat_id)
        return
    if text.startswith("/"):
        _send(client, token, chat_id, _HELP_TEXT)
        return
    if len(text) < 3:
        _send(client, token, chat_id, _HELP_TEXT)
        return
    try:
        _call(client, token, "sendChatAction",
              {"chat_id": chat_id, "action": "typing"})
        result = answer_fn(text)
        _send(client, token, chat_id, _render_answer(result))
        sources = _render_sources(result)
        if sources:
            _send(client, token, chat_id, sources)
    except Exception as exc:  # noqa: BLE001 — one bad question never kills the poll
        logger.error("telegram_ask_failed", error=str(exc)[:180])
        _send(client, token, chat_id,
              "Something went wrong while answering — please try again.")


def _process_updates(client: httpx.Client, token: str, owner_chat_id: str,
                     updates: list[dict], state: PollState, answer_fn) -> None:
    for update in updates:
        state.offset = max(state.offset, int(update.get("update_id", 0)) + 1)
        _handle_update(client, token, owner_chat_id, update, answer_fn)


def _load_offset() -> int:
    """Best-effort offset restore so a restart does not re-answer old
    messages; Redis unavailable → start from 0 (Telegram re-delivers, the
    owner may see one repeat answer)."""
    try:
        settings = get_settings()
        value = redis_lib.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2).get(_OFFSET_KEY)
        return int(value) if value else 0
    except Exception as exc:  # noqa: BLE001 — offset persistence is optional
        logger.warning("telegram_offset_load_failed", error=str(exc)[:120])
        return 0


def _save_offset(offset: int) -> None:
    try:
        settings = get_settings()
        redis_lib.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2).set(_OFFSET_KEY, offset)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_offset_save_failed", error=str(exc)[:120])


def run_polling(client: httpx.Client | None = None, answer_fn=ask_question) -> None:
    """Long-poll loop. Injectable httpx client + answer function keep the
    unit tests fully offline (MockTransport + fake ask)."""
    settings = get_settings()
    if not settings.telegram_ask_enabled:
        logger.info("telegram_ask_disabled")
        return
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        # Idle instead of exit: compose `restart: unless-stopped` would
        # otherwise crash-loop while the owner has not configured the bot.
        logger.warning("telegram_ask_unconfigured",
                       hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        while True:
            time.sleep(3600)

    token = settings.telegram_bot_token
    owner = settings.telegram_chat_id
    http = client or httpx.Client(timeout=_POLL_SECONDS + 15)
    _call(http, token, "deleteWebhook")
    state = PollState(offset=_load_offset())
    logger.info("telegram_ask_started", offset=state.offset)

    while True:
        try:
            body = _call(http, token, "getUpdates",
                         {"offset": state.offset, "timeout": _POLL_SECONDS})
        except Exception as exc:  # noqa: BLE001 — transient network, keep polling
            logger.warning("telegram_poll_failed", error=str(exc)[:120])
            time.sleep(5)
            continue
        updates = body.get("result") or []
        if updates:
            _process_updates(http, token, owner, updates, state, answer_fn)
            _save_offset(state.offset)


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
    )
    run_polling()


if __name__ == "__main__":
    main()
