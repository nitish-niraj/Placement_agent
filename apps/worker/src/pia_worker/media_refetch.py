"""Evolution media refetch — recovery ladder Case 1.

When the stored webhook payload has no inline base64 (the connector ran with
base64=false when the message arrived), the bytes may still live in
Evolution's own message store:

    POST {base}/chat/getBase64FromMediaMessage/{instance}
    headers: apikey: <EVOLUTION_API_KEY>
    body: {"message": {"key": {"id": "<whatsapp-key-id>"}}, "convertToMp4": false}

Outcomes:
- bytes — recovered, the download proceeds as if inline;
- RefetchUnavailable — Evolution answers 400/404 or ok:false: the message is
  not in its store (retention/store disabled). Permanent for this row: mark
  FAILED, never retry;
- transport/5xx/timeout errors propagate — RQ retries those with backoff.
"""

import httpx
import structlog

logger = structlog.get_logger()


class RefetchUnavailable(RuntimeError):
    """Evolution does not hold this media (400/404/ok:false) — permanent."""


def extract_wa_key_id(data: dict) -> str | None:
    """WhatsApp message key.id from a stored webhook payload (Baileys shape:
    data.message.key.id; tolerate a top-level data.key.id)."""
    message = data.get("message") or {}
    key = message.get("key") or data.get("key") or {}
    key_id = key.get("id")
    return str(key_id) if key_id else None


def _extract_base64(body: object) -> bytes | None:
    """Defensive base64 pull: {base64}, {result:{base64}}, or a bare data-uri
    string. Returns None when the shape is unrecognized (caller treats that
    as unavailable, never as empty bytes)."""
    import base64 as _b64

    candidate: object = None
    if isinstance(body, dict):
        candidate = body.get("base64")
        if candidate is None and isinstance(body.get("result"), dict):
            candidate = body["result"].get("base64")
        if candidate is None and isinstance(body.get("data"), str):
            candidate = body["data"]
    elif isinstance(body, str):
        candidate = body
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    try:
        return _b64.b64decode(candidate.split(",", 1)[-1])
    except Exception:  # noqa: BLE001 — undecodable is "unavailable", not fatal
        return None


def fetch_base64_from_evolution(
    *, base_url: str, api_key: str, instance: str, wa_key_id: str,
    client: httpx.Client | None = None, timeout_seconds: float = 30.0,
) -> bytes:
    """Refetch one message's media bytes. Raises RefetchUnavailable when
    Evolution definitively lacks it; anything else transport-related raises
    the httpx error for the caller's retry policy."""
    url = (base_url.rstrip("/")
           + f"/chat/getBase64FromMediaMessage/{instance}")
    payload = {"message": {"key": {"id": wa_key_id}}, "convertToMp4": False}
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    if client is not None:
        response = client.post(url, json=payload, headers=headers,
                               timeout=timeout_seconds)
    else:
        response = httpx.post(url, json=payload, headers=headers,
                              timeout=timeout_seconds)
    if response.status_code in (400, 404):
        raise RefetchUnavailable(
            f"evolution has no stored media for key {wa_key_id[:12]} "
            f"(http {response.status_code})")
    response.raise_for_status()
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise RefetchUnavailable(
            f"evolution returned non-JSON for key {wa_key_id[:12]}") from exc
    if isinstance(body, dict) and body.get("ok") is False:
        raise RefetchUnavailable(
            f"evolution ok:false for key {wa_key_id[:12]}: "
            f"{str(body.get('message') or body.get('error'))[:100]}")
    content = _extract_base64(body)
    if content is None:
        raise RefetchUnavailable(
            f"evolution response carried no base64 for key {wa_key_id[:12]}")
    logger.info("evolution_media_refetched", key_prefix=wa_key_id[:8],
                size=len(content))
    return content
