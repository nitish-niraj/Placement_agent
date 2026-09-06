"""SEC-002 encryption contract (pia_shared.crypto)."""

import base64
import secrets

import pytest

from pia_shared.crypto import decrypt, encrypt, is_encrypted

KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def test_roundtrip() -> None:
    ct = encrypt("12515641", KEY)
    assert ct != "12515641"
    assert is_encrypted(ct)
    assert decrypt(ct, KEY) == "12515641"


def test_ciphertext_is_nondeterministic() -> None:
    assert encrypt("12515641", KEY) != encrypt("12515641", KEY)


def test_empty_passthrough() -> None:
    assert encrypt(None, KEY) is None
    assert encrypt("", KEY) == ""
    assert decrypt(None, KEY) is None
    assert decrypt("", KEY) == ""


def test_legacy_plaintext_decrypts_transparently() -> None:
    """Pre-encryption rows read fine without migration failures."""
    assert decrypt("plain-value", KEY) == "plain-value"
    assert not is_encrypted("plain-value")


def test_write_without_key_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="PIA_ENCRYPTION_KEY"):
        encrypt("secret", "")


def test_wrong_key_fails_to_decrypt() -> None:
    from cryptography.exceptions import InvalidTag

    other = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    with pytest.raises(InvalidTag):
        decrypt(encrypt("x", KEY), other)
