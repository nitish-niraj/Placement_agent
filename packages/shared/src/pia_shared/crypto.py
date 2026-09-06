"""App-level encryption for sensitive profile fields (SEC-002, TRD §9).

AES-256-GCM via `cryptography`. Ciphertext format: `enc:v1:<base64(nonce|ct|tag)>`.
Values without the prefix are treated as legacy plaintext and returned as-is —
read paths keep working during migration; write paths always encrypt when a key
is configured. Key source: PIA_ENCRYPTION_KEY (32-byte urlsafe-base64), never
committed (SEC-001).
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "enc:v1:"


class EncryptionUnavailable(RuntimeError):
    """Raised on write paths when no key is configured."""


def _key(secret: str) -> bytes:
    return base64.urlsafe_b64decode(secret)


def encrypt(plaintext: str | None, secret: str) -> str | None:
    if plaintext is None or plaintext == "":
        return plaintext
    if not secret:
        raise EncryptionUnavailable("PIA_ENCRYPTION_KEY not configured")
    nonce = os.urandom(12)
    ct = AESGCM(_key(secret)).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(value: str | None, secret: str) -> str | None:
    if value is None or value == "":
        return value
    if not value.startswith(_PREFIX):
        return value  # legacy plaintext (pre-encryption row) — reads keep working
    if not secret:
        raise EncryptionUnavailable("PIA_ENCRYPTION_KEY not configured")
    raw = base64.urlsafe_b64decode(value.removeprefix(_PREFIX))
    return AESGCM(_key(secret)).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def is_encrypted(value: str | None) -> bool:
    return value is not None and value.startswith(_PREFIX)
