"""AES-GCM-256 encryption for StorageState blobs with mandatory AAD.

AAD (Additional Authenticated Data) binds each ciphertext to the exact
(tenant_id, site_id, kid) triple it was created for.  Decrypting with any
different combination raises ``cryptography.exceptions.InvalidTag`` —
fail-closed, no exception swallowing.

Wire format:   nonce (12 bytes) || ciphertext_with_tag (variable)

Key requirement: exactly 32 bytes (256-bit).

The key MUST be derived by the caller via HKDF-SHA256 with
``info=b"hermes.browser.storage_state.v1"`` in the composition root.
This adapter receives the derived key — it does NOT derive.

Threat-model control P1 #1 — surface 2 (StorageState cross-tenant replay).
Constitution IV: fail-closed; InvalidTag propagates to caller.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from hermes.browser.domain.ports.storage_state_port import EncryptedStorageState

_AAD_PREFIX = b"hermes.browser.storage_state.v1|"
_NONCE_BYTES = 12
_KEY_BYTES = 32


def _require_aesgcm() -> object:
    """Lazy-import so the module is importable without [browser] extras."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import (  # noqa: PLC0415
            AESGCM,  # type: ignore[import-untyped]
        )
        return AESGCM
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "cryptography package is required for StorageState encryption. "
            "Install with: pip install 'hermes-runtime[browser]'"
        ) from exc


def _build_aad(*, tenant_id: UUID | str, site_id: str, kid: str) -> bytes:
    """Canonical AAD bytes for the (tenant_id, site_id, kid) triple."""
    return (
        _AAD_PREFIX
        + str(tenant_id).encode("utf-8")
        + b"|"
        + site_id.encode("utf-8")
        + b"|"
        + kid.encode("utf-8")
    )


def _validate_inputs(*, key: bytes, site_id: str, kid: str) -> None:
    if len(key) != _KEY_BYTES:
        raise ValueError(
            f"key must be exactly {_KEY_BYTES} bytes for AES-256; got {len(key)}"
        )
    if not site_id:
        raise ValueError("site_id must not be empty")
    if not kid:
        raise ValueError("kid must not be empty")


def encrypt(
    plaintext: bytes,
    *,
    tenant_id: UUID | str,
    site_id: str,
    kid: str,
    key: bytes,
) -> bytes:
    """Encrypt ``plaintext`` under AES-GCM-256, binding AAD to the triple.

    Returns ``nonce || ciphertext_with_tag`` (12 + len(plaintext) + 16 bytes).

    Raises:
        ValueError: if ``key`` is not 32 bytes, or ``site_id``/``kid`` are empty.
        ModuleNotFoundError: if ``cryptography`` is not installed.
    """
    _validate_inputs(key=key, site_id=site_id, kid=kid)
    AESGCM = _require_aesgcm()
    aead = AESGCM(key)  # type: ignore[operator]
    aad = _build_aad(tenant_id=tenant_id, site_id=site_id, kid=kid)
    # os.urandom(12) — cryptographically secure nonce.
    nonce = os.urandom(_NONCE_BYTES)
    ct_with_tag = aead.encrypt(nonce, plaintext, aad)
    return nonce + ct_with_tag


def decrypt(
    ciphertext: bytes,
    *,
    tenant_id: UUID | str,
    site_id: str,
    kid: str,
    key: bytes,
) -> bytes:
    """Decrypt a blob produced by :func:`encrypt`.

    ``ciphertext`` must be ``nonce (12 bytes) || ct_with_tag``.

    Raises:
        ValueError: if ``key`` is not 32 bytes, or ``site_id``/``kid`` are empty.
        cryptography.exceptions.InvalidTag: if AAD, key, or ciphertext is wrong
            (fail-closed; never caught here).
        ModuleNotFoundError: if ``cryptography`` is not installed.
    """
    _validate_inputs(key=key, site_id=site_id, kid=kid)
    AESGCM = _require_aesgcm()
    aead = AESGCM(key)  # type: ignore[operator]
    aad = _build_aad(tenant_id=tenant_id, site_id=site_id, kid=kid)
    nonce = ciphertext[:_NONCE_BYTES]
    ct_with_tag = ciphertext[_NONCE_BYTES:]
    # InvalidTag propagates — caller decides how to handle (constitution IV).
    return aead.decrypt(nonce, ct_with_tag, aad)


# ---------------------------------------------------------------------------
# High-level EncryptedStorageState helpers (T405)
# ---------------------------------------------------------------------------


def encrypt_state(
    plaintext_json: bytes,
    *,
    tenant_id: UUID,
    site_id: str,
    kid: str,
    key: bytes,
) -> EncryptedStorageState:
    """Wrap low-level :func:`encrypt` into an ``EncryptedStorageState`` blob.

    The ``key`` MUST already be the derived 32-byte key from the composition
    root (HKDF-SHA256, info=b"hermes.browser.storage_state.v1").
    This function does NOT derive; it only encrypts.

    Raises:
        ValueError: if ``key`` is not 32 bytes, or ``site_id``/``kid`` empty.
        ModuleNotFoundError: if ``cryptography`` is not installed.

    Security: Constitution III — plaintext_json never logged here.
    """
    # Lazy import to keep the module importable without [browser] extras.
    from hermes.browser.domain.ports.storage_state_port import (  # noqa: PLC0415
        EncryptedStorageState as _ESS,
    )

    ciphertext = encrypt(plaintext_json, tenant_id=tenant_id, site_id=site_id, kid=kid, key=key)
    return _ESS(
        tenant_id=tenant_id,
        site_id=site_id,
        ciphertext=ciphertext,
        kid=kid,
        alg="AES-GCM-256",
        updated_at=datetime.now(tz=UTC),
    )


def decrypt_state(
    state: EncryptedStorageState,
    *,
    key: bytes,
) -> bytes:
    """Decrypt an ``EncryptedStorageState`` blob back to JSON bytes.

    Uses the ``(tenant_id, site_id, kid)`` fields from the state as AAD
    so cross-tenant/cross-site reuse is rejected by AES-GCM (T201).

    Raises:
        cryptography.exceptions.InvalidTag: AAD mismatch or tampered blob
            (fail-closed; caller must catch and call port.invalidate()).
        ValueError: invalid key length.
        ModuleNotFoundError: if ``cryptography`` is not installed.
    """
    # InvalidTag propagates — caller converts to StorageStateCorrupt (IV).
    return decrypt(
        state.ciphertext,
        tenant_id=state.tenant_id,
        site_id=state.site_id,
        kid=state.kid,
        key=key,
    )


