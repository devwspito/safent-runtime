"""Skill signing key resolution — v1 (deprecated) and v2 (native keystore).

Split out of the retired teach-by-browser feature's ``training/persist.py``
(retired 10-sep-2026): key resolution is generic to ALL skill signing
(Composio-origin, cage-authored, hub-installed), not specific to teaching.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def build_signing_key(db_path: Path) -> bytes:
    """Derive a stable HMAC key from the db path (deterministic per node).

    DEPRECATED — retained as a READ-ONLY reference for documentation
    purposes only. Must NEVER be called for signing new skills (CWE-321:
    the key equals SHA-256(public path) and is publicly derivable).
    Calling this function for signing is a security regression.
    """
    return hashlib.sha256(str(db_path).encode()).digest()


def resolve_signing_key(db_path: Path) -> tuple[bytes, str]:  # noqa: ARG001
    """Return (key_bytes, 'v2') for signing a NEW skill — fail-closed.

    Uses the native keystore (SecretsVault.derive_subkey via
    NativeKeyStoreAdapter) exclusively. If master.key is absent this
    function raises SigningKeyError rather than falling back to v1.

    Rationale: hermes-keygen.service is declared
      Before=hermes-shell-server.service
    so absent master.key is a fatal misconfiguration, not a transient state.
    Signing with a predictable path-derived key (v1) produces forgeable
    signatures and must be rejected unconditionally.

    Args:
        db_path: accepted for signature compatibility but ignored — the
                 signing key is derived from master.key, never from a path.

    Raises:
        SigningKeyError: if master.key is absent or corrupt.
    """
    from hermes.shell_server.skills.native_keystore_adapter import (  # noqa: PLC0415
        NativeKeyStoreAdapter,
    )

    adapter = NativeKeyStoreAdapter()
    return adapter.get_signing_key_sync(), "v2"
