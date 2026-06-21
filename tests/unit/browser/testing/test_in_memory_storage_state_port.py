"""T401: Tests for InMemoryStorageStatePort.

Covers:
  - Happy-path roundtrip: save(state) -> load(tenant, site) == state.
  - Concurrent save without lock -> second save raises StorageStateLocked.
  - Decrypt with different (tenant_id, site_id) -> StorageStateCorrupt via AAD.
  - Reauth flow after corrupt: invalidate(reason=CORRUPT_DECRYPT) + next load = None.
  - invalidate() is idempotent (calling twice does not raise).

Constitution V: no Postgres, no network, deterministic.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from hermes.browser.domain.ports.storage_state_port import (
    EncryptedStorageState,
    StorageStateInvalidationReason,
    StorageStateLocked,
)
from hermes.browser.testing.in_memory_storage_state import InMemoryStorageStatePort


def _make_state(
    *,
    tenant_id=None,
    site_id="aeat_sede",
    ciphertext=b"cipher_bytes",
    kid="key-1",
) -> EncryptedStorageState:
    return EncryptedStorageState(
        tenant_id=tenant_id or uuid4(),
        site_id=site_id,
        ciphertext=ciphertext,
        kid=kid,
    )


# ---------------------------------------------------------------------------
# Happy-path roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_load_roundtrip() -> None:
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    state = _make_state(tenant_id=tenant_id, site_id="aeat_sede")

    async with port.lock(tenant_id=tenant_id, site_id="aeat_sede"):
        await port.save(state)

    loaded = await port.load(tenant_id=tenant_id, site_id="aeat_sede")
    assert loaded is not None
    assert loaded.ciphertext == b"cipher_bytes"
    assert loaded.kid == "key-1"
    assert loaded.tenant_id == tenant_id
    assert loaded.site_id == "aeat_sede"
    assert loaded.invalidated_at is None


# ---------------------------------------------------------------------------
# Concurrent save without lock raises StorageStateLocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_without_lock_raises_storage_state_locked() -> None:
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    state = _make_state(tenant_id=tenant_id)

    with pytest.raises(StorageStateLocked, match="outside lock"):
        await port.save(state)


@pytest.mark.asyncio
async def test_concurrent_second_save_blocked_until_first_releases() -> None:
    """Second lock acquisition blocks while first holds it."""
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    site_id = "concurrency_test"

    first_entered = asyncio.Event()
    first_release = asyncio.Event()

    async def first_session() -> None:
        async with port.lock(tenant_id=tenant_id, site_id=site_id):
            first_entered.set()
            await first_release.wait()

    async def second_session() -> bool:
        await first_entered.wait()
        # Try to acquire with a short timeout — should block briefly then succeed.
        async with port.lock(tenant_id=tenant_id, site_id=site_id, timeout_s=5.0):
            return True
        return False  # pragma: no cover

    t1 = asyncio.create_task(first_session())
    t2 = asyncio.create_task(second_session())

    # Let first take the lock.
    await asyncio.sleep(0)
    await first_entered.wait()

    # Release first lock — second should now acquire.
    first_release.set()

    await t1
    result = await t2
    assert result is True


@pytest.mark.asyncio
async def test_second_save_raises_locked_when_first_holds_timeout() -> None:
    """Second caller times out when first holds the lock too long."""
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    site_id = "timeout_test"

    hold_forever = asyncio.Event()

    async def first_session() -> None:
        async with port.lock(tenant_id=tenant_id, site_id=site_id):
            await hold_forever.wait()  # Hold indefinitely in test.

    t1 = asyncio.create_task(first_session())
    await asyncio.sleep(0)  # Let first acquire.

    with pytest.raises(StorageStateLocked):
        async with port.lock(tenant_id=tenant_id, site_id=site_id, timeout_s=0.05):
            pass

    hold_forever.set()
    await t1


# ---------------------------------------------------------------------------
# AAD mismatch → StorageStateCorrupt (via T201 crypto)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decrypt_with_different_tenant_rejected() -> None:
    """Decrypting a blob with a different tenant_id fails due to AAD mismatch.

    The InMemoryStorageStatePort stores ciphertext as-is; the caller is
    responsible for decrypting with the correct AAD.  This test exercises the
    full path: encrypt under tenant_A, store, load as if tenant_B, decrypt →
    cryptography.InvalidTag which the caller converts to StorageStateCorrupt.
    """
    try:
        from cryptography.exceptions import InvalidTag

        from hermes.browser.infrastructure.storage_state_crypto import (
            decrypt_state,
            encrypt_state,
        )
    except ModuleNotFoundError:
        pytest.skip("cryptography not installed")

    port = InMemoryStorageStatePort()
    tenant_a = uuid4()
    tenant_b = uuid4()
    site_id = "aeat"
    kid = "k1"
    key = b"\x00" * 32  # deterministic test key

    plaintext = b'{"cookies": [], "origins": []}'
    state = encrypt_state(plaintext, tenant_id=tenant_a, site_id=site_id, kid=kid, key=key)

    async with port.lock(tenant_id=tenant_a, site_id=site_id):
        await port.save(state)

    # Simulate "loaded as tenant_b" — reconstruct state with wrong tenant_id.
    loaded = await port.load(tenant_id=tenant_a, site_id=site_id)
    assert loaded is not None

    wrong_tenant_state = EncryptedStorageState(
        tenant_id=tenant_b,           # wrong
        site_id=loaded.site_id,
        ciphertext=loaded.ciphertext,
        kid=loaded.kid,
        alg=loaded.alg,
    )

    # Decrypting with tenant_b's AAD → InvalidTag (fail-closed, constitution IV).
    with pytest.raises(InvalidTag):
        decrypt_state(wrong_tenant_state, key=key)


# ---------------------------------------------------------------------------
# Reauth flow after corrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reauth_flow_after_corrupt_decrypt() -> None:
    """Corrupt decrypt → invalidate(CORRUPT_DECRYPT) → next load returns None."""
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    site_id = "sede"

    state = _make_state(tenant_id=tenant_id, site_id=site_id)
    async with port.lock(tenant_id=tenant_id, site_id=site_id):
        await port.save(state)

    # Caller detects corrupt and invalidates.
    await port.invalidate(
        tenant_id=tenant_id,
        site_id=site_id,
        reason=StorageStateInvalidationReason.CORRUPT_DECRYPT,
    )

    # Next load must return None (start with clean state).
    loaded = await port.load(tenant_id=tenant_id, site_id=site_id)
    assert loaded is None


# ---------------------------------------------------------------------------
# invalidate() is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_idempotent() -> None:
    """Calling invalidate twice does not raise."""
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    site_id = "idempotent_test"

    state = _make_state(tenant_id=tenant_id, site_id=site_id)
    async with port.lock(tenant_id=tenant_id, site_id=site_id):
        await port.save(state)

    # First invalidation.
    await port.invalidate(
        tenant_id=tenant_id,
        site_id=site_id,
        reason=StorageStateInvalidationReason.MANUAL,
    )

    # Second call must not raise.
    await port.invalidate(
        tenant_id=tenant_id,
        site_id=site_id,
        reason=StorageStateInvalidationReason.MANUAL,
    )

    # Still None after double-invalidate.
    assert await port.load(tenant_id=tenant_id, site_id=site_id) is None


@pytest.mark.asyncio
async def test_invalidate_on_nonexistent_key_is_noop() -> None:
    """Invalidating a key that was never saved does not raise."""
    port = InMemoryStorageStatePort()
    # Should not raise even though nothing was saved.
    await port.invalidate(
        tenant_id=uuid4(),
        site_id="never_saved",
        reason=StorageStateInvalidationReason.MANUAL,
    )
