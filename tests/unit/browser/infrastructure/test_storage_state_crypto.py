"""Tests for storage_state_crypto.py — AES-GCM-256 with AAD.

T201 acceptance:
  - test_aad_cross_tenant_rejected  ← primary acceptance criterion
  - test_aad_cross_site_rejected
  All 8 cases specified in tasks.md are covered.

Constitution IV: InvalidTag must NOT be caught by the module; it propagates.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from hermes.browser.infrastructure.storage_state_crypto import decrypt, encrypt

_KEY_32 = b"\xab" * 32
_PLAINTEXT = b'{"cookies": [{"name": "session", "value": "tok_abc"}]}'

_TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_SITE = "aeat_sede"
_SITE_ALT = "tgss_red"
_KID = "key-v1"
_KID_ALT = "key-v2"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_roundtrip_happy_path() -> None:
    ct = encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=_KEY_32)
    recovered = decrypt(ct, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=_KEY_32)
    assert recovered == _PLAINTEXT


# ---------------------------------------------------------------------------
# AAD binding — cross-context rejection (constitution IV: fail-closed)
# ---------------------------------------------------------------------------


def test_aad_cross_tenant_rejected() -> None:
    """Blob encrypted for tenant A must not decrypt under tenant B."""
    ct = encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=_KEY_32)
    from cryptography.exceptions import InvalidTag  # type: ignore[import-untyped]
    with pytest.raises(InvalidTag):
        decrypt(ct, tenant_id=_TENANT_B, site_id=_SITE, kid=_KID, key=_KEY_32)


def test_aad_cross_site_rejected() -> None:
    """Blob encrypted for site A must not decrypt under site B."""
    ct = encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=_KEY_32)
    from cryptography.exceptions import InvalidTag  # type: ignore[import-untyped]
    with pytest.raises(InvalidTag):
        decrypt(ct, tenant_id=_TENANT_A, site_id=_SITE_ALT, kid=_KID, key=_KEY_32)


def test_aad_cross_kid_rejected() -> None:
    """Blob encrypted with kid=key-v1 must not decrypt under kid=key-v2."""
    ct = encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=_KEY_32)
    from cryptography.exceptions import InvalidTag  # type: ignore[import-untyped]
    with pytest.raises(InvalidTag):
        decrypt(ct, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID_ALT, key=_KEY_32)


def test_tampered_ciphertext_rejected() -> None:
    """Flipping a single bit of the ciphertext (past the nonce) must raise InvalidTag."""
    ct = encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=_KEY_32)
    # Flip a byte in the ciphertext-with-tag region (after the 12-byte nonce).
    tampered = bytearray(ct)
    tampered[12] ^= 0xFF
    from cryptography.exceptions import InvalidTag  # type: ignore[import-untyped]
    with pytest.raises(InvalidTag):
        decrypt(bytes(tampered), tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=_KEY_32)


# ---------------------------------------------------------------------------
# Key validation
# ---------------------------------------------------------------------------


def test_invalid_key_length_raises() -> None:
    """A 16-byte key (AES-128) must raise ValueError before any crypto op."""
    short_key = b"\x00" * 16
    with pytest.raises(ValueError, match="32 bytes"):
        encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id=_SITE, kid=_KID, key=short_key)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_kid_raises() -> None:
    with pytest.raises(ValueError, match="kid"):
        encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id=_SITE, kid="", key=_KEY_32)


def test_empty_site_raises() -> None:
    with pytest.raises(ValueError, match="site_id"):
        encrypt(_PLAINTEXT, tenant_id=_TENANT_A, site_id="", kid=_KID, key=_KEY_32)


# ---------------------------------------------------------------------------
# tenant_id type coercion — UUID and str must produce identical AAD
# ---------------------------------------------------------------------------


def test_uuid_or_str_tenant_id_both_work() -> None:
    """Passing UUID('aaa...') and the equivalent string must encrypt identically."""
    tenant_uuid = _TENANT_A
    tenant_str = str(_TENANT_A)

    ct_from_uuid = encrypt(
        _PLAINTEXT, tenant_id=tenant_uuid, site_id=_SITE, kid=_KID, key=_KEY_32
    )
    ct_from_str = encrypt(
        _PLAINTEXT, tenant_id=tenant_str, site_id=_SITE, kid=_KID, key=_KEY_32
    )

    # Different nonces -> different ciphertexts, but cross-decryption must succeed.
    recovered_uuid = decrypt(
        ct_from_uuid, tenant_id=tenant_str, site_id=_SITE, kid=_KID, key=_KEY_32
    )
    recovered_str = decrypt(
        ct_from_str, tenant_id=tenant_uuid, site_id=_SITE, kid=_KID, key=_KEY_32
    )
    assert recovered_uuid == _PLAINTEXT
    assert recovered_str == _PLAINTEXT
