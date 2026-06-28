"""Regression: a TOTP code must be single-use (no replay within the ±window)."""
from __future__ import annotations

import pytest
from hermes.shell_server.security.mfa import MfaStore, ProtectionLevel, totp_now

pytestmark = pytest.mark.unit


def test_totp_cannot_be_replayed(tmp_path):
    store = MfaStore(store_dir=tmp_path)
    secret = store.enroll_totp() if hasattr(store, "enroll_totp") else None
    # enroll via the public API if present, else seed directly
    if secret is None:
        from hermes.shell_server.security.mfa import generate_secret
        secret = generate_secret()
        d = store._load(); d["totp_secret"] = secret; store._save(d)
    code = totp_now(secret)
    ok1, _ = store.verify(level=ProtectionLevel.MFA, totp=code)
    ok2, reason2 = store.verify(level=ProtectionLevel.MFA, totp=code)
    assert ok1 is True
    assert ok2 is False and reason2 == "totp_replayed"
