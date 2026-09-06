"""F-024 delivery channel — DEC-002 adapter pattern.

`NotificationChannel` is the pluggable interface (email/WhatsApp-self are
interface-only in MVP); `TelegramChannel` implements the MVP channel via the
Bot API. Delivery is decoupled from decisioning: callers pass an already
rendered message; a send failure raises `DeliveryError` for the job layer to
turn into retries and eventually PENDING_DELIVERY (never a silent drop).
"""

from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"


class DeliveryError(RuntimeError):
    """Send failed — the caller decides retry vs PENDING_DELIVERY."""


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    channel: str
    error: str | None = None


class NotificationChannel:
    def send(self, chat_id: str, text: str) -> DeliveryResult:
        raise NotImplementedError  # interface only (DEC-002)


class TelegramChannel(NotificationChannel):
    def __init__(self, bot_token: str, timeout_seconds: float = 15.0,
                 client: httpx.Client | None = None) -> None:
        self._token = bot_token
        self._timeout = timeout_seconds
        self._client = client  # injectable for tests

    def send(self, chat_id: str, text: str) -> DeliveryResult:
        if not self._token:
            raise DeliveryError("TELEGRAM_BOT_TOKEN not configured")
        if not chat_id:
            raise DeliveryError("TELEGRAM_CHAT_ID not configured")
        try:
            if self._client is not None:
                response = self._client.post(
                    f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                    json={"chat_id": chat_id, "text": text,
                          "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=self._timeout,
                )
            else:
                response = httpx.post(
                    f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                    json={"chat_id": chat_id, "text": text,
                          "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=self._timeout,
                )
        except httpx.HTTPError as exc:
            raise DeliveryError(f"telegram unreachable: {exc}") from exc
        body = response.json() if response.headers.get("content-type", "").startswith(
            "application/json") else {}
        if response.status_code != 200 or not body.get("ok"):
            raise DeliveryError(
                f"telegram send failed: {response.status_code} "
                f"{body.get('description', '')}"
            )
        logger.info("telegram_delivered", chat_id=chat_id,
                    message_id=(body.get("result") or {}).get("message_id"))
        return DeliveryResult(True, "telegram")
