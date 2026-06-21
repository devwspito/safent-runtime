from __future__ import annotations

import pytest

from hermes.browser.domain.selector import Selector, SelectorStrategy
from hermes.browser.infrastructure import (
    InMemorySelectorRegistry,
    SelectorTamperedError,
    SignedSelectorRegistry,
    StoredSelector,
    build_signed,
    sign_selector,
    verify_selector_signature,
)

_KEY = b"\x01" * 32
_OTHER_KEY = b"\x02" * 32


def _make_selector(version: int = 1, value: str = "#btn-303") -> Selector:
    return Selector.new(
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        step_id="boton_presentar",
        strategy=SelectorStrategy.CSS,
        value=value,
        intent_desc="boton presentar 303",
        version=version,
    )


def test_sign_and_verify_roundtrip() -> None:
    s = _make_selector()
    sig = sign_selector(s, key=_KEY)
    assert verify_selector_signature(s, sig, key=_KEY) is True


def test_verify_fails_with_wrong_key() -> None:
    s = _make_selector()
    sig = sign_selector(s, key=_KEY)
    assert verify_selector_signature(s, sig, key=_OTHER_KEY) is False


def test_verify_fails_when_value_tampered() -> None:
    s = _make_selector()
    sig = sign_selector(s, key=_KEY)
    tampered = Selector(
        selector_id=s.selector_id,
        site_id=s.site_id,
        flow_id=s.flow_id,
        step_id=s.step_id,
        strategy=s.strategy,
        value="#evil-selector",  # cambiado
        intent_desc=s.intent_desc,
        tenant_scope=s.tenant_scope,
        version=s.version,
        created_at=s.created_at,
    )
    assert verify_selector_signature(tampered, sig, key=_KEY) is False


def test_build_signed_returns_consistent_signature() -> None:
    stored = build_signed(
        signing_key=_KEY,
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        step_id="boton_presentar",
        strategy=SelectorStrategy.CSS,
        value="#btn-303",
        intent_desc="x",
    )
    assert verify_selector_signature(
        stored.selector, stored.signature_hex, key=_KEY
    ) is True


@pytest.mark.asyncio
async def test_signed_registry_fetch_returns_active_selector() -> None:
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)

    stored = build_signed(
        signing_key=_KEY,
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        step_id="boton_presentar",
        strategy=SelectorStrategy.CSS,
        value="#btn-303",
        intent_desc="x",
    )
    await store.persist(stored)

    out = await registry.fetch_latest(
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        step_id="boton_presentar",
    )
    assert out is not None
    assert out.value == "#btn-303"


@pytest.mark.asyncio
async def test_signed_registry_fail_closed_on_tamper() -> None:
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)

    selector = _make_selector(value="#legit")
    bad_signature = sign_selector(selector, key=_OTHER_KEY)
    await store.persist(StoredSelector(selector=selector, signature_hex=bad_signature))

    with pytest.raises(SelectorTamperedError):
        await registry.fetch_latest(
            site_id="aeat_sede",
            flow_id="modelo_303_borrador",
            step_id="boton_presentar",
        )


@pytest.mark.asyncio
async def test_persist_marks_previous_version_deprecated() -> None:
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)

    v1 = _make_selector(version=1, value="#btn-303-v1")
    await registry.persist(v1)

    v2 = _make_selector(version=2, value="#btn-303-v2")
    await registry.persist(v2)

    history = await registry.history(
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        step_id="boton_presentar",
    )
    assert len(history) == 2
    # v1 deprecated
    v1_in_hist = next(s for s in history if s.version == 1)
    assert v1_in_hist.deprecated_at is not None
    # v2 active
    v2_in_hist = next(s for s in history if s.version == 2)
    assert v2_in_hist.deprecated_at is None

    # fetch_latest devuelve v2
    out = await registry.fetch_latest(
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        step_id="boton_presentar",
    )
    assert out is not None
    assert out.version == 2


def test_sign_with_empty_key_raises() -> None:
    s = _make_selector()
    with pytest.raises(ValueError, match="signing key"):
        sign_selector(s, key=b"")
