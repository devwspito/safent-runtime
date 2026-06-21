"""T503 — Tests revalidación domain whitelist + TTL antes de ejecutar replay.

3 tests que cubren los guards del orchestrator (threat-model control P2 #8):

  (a) TTL stale (created_at > 90d) → invalidate(SITE_CHANGED) + discovery.
  (b) Domain drift (site_id.domains_whitelist no incluye dominio en steps)
      → invalidate(SITE_CHANGED) + discovery.
  (c) Happy path: script joven + domain OK → ejecuta replay.

Constitución IV: fail-closed — guards fallidos → invalidate + discovery.
Constitución V: sin Chromium, sin red, sin DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hermes.browser.application.orchestrator import BrowserOrchestrator
from hermes.browser.domain.replay_script import (
    ReplayScript,
    ReplayStep,
)
from hermes.browser.infrastructure.replay_codec import sign_replay
from hermes.browser.testing.fakes import FakeBrowserDriver
from hermes.browser.testing.in_memory_replay_store import InMemoryReplayStore

_KEY = b"\xf0\x0b" * 16  # 32 bytes test key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signed_script(
    *,
    created_at: datetime | None = None,
    url: str = "https://stub.local/home",
    tenant_scope=None,
) -> ReplayScript:
    step = ReplayStep(
        selector_id=str(uuid4()),
        selector_version=1,
        action="navigate",
        payload_template={"url": url},
        risk="low",
    )
    script = ReplayScript(
        script_id=uuid4(),
        site_id="stub_local",
        flow_id="consulta_estado",
        tenant_scope=tenant_scope,
        runtime_version="0.2.1",
        steps=(step,),
        created_at=created_at or datetime.now(tz=UTC),
    )
    return sign_replay(script, key=_KEY)


async def _load_store_with(script: ReplayScript) -> InMemoryReplayStore:
    store = InMemoryReplayStore()
    await store.persist(script)
    return store


# ---------------------------------------------------------------------------
# (a) TTL stale → invalidate(SITE_CHANGED) + discovery_needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_ttl_invalidates_and_returns_discovery_needed() -> None:
    """Script con created_at > 90 días → stale → invalidate + discovery_needed.

    Threat-model E2 superficie 3: TTL limita "replay en contexto semánticamente cambiado".
    """
    stale_at = datetime.now(tz=UTC) - timedelta(days=91)
    script = _make_signed_script(created_at=stale_at)
    store = await _load_store_with(script)

    orchestrator = BrowserOrchestrator(
        replay_store=store,
        replay_signing_key=_KEY,
        replay_max_age_days=90,
    )

    outcome = await orchestrator.execute_flow(
        site_id=script.site_id,
        flow_id=script.flow_id,
        tenant_scope=script.tenant_scope,
    )

    assert outcome.mode == "discovery_needed", (
        f"Script stale debe devolver discovery_needed. Got: {outcome.mode}"
    )

    # Script debe haber sido invalidado en el store
    loaded = await store.load_for(
        site_id=script.site_id,
        flow_id=script.flow_id,
        tenant_scope=script.tenant_scope,
    )
    assert loaded is None, "Script stale debe quedar invalidado en el store"


# ---------------------------------------------------------------------------
# (b) Domain drift → invalidate + discovery_needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_drift_invalidates_and_returns_discovery_needed() -> None:
    """Script con URL fuera de la whitelist actual → domain drift → discovery.

    Simula que el SiteSpec cambió su domains_whitelist (e.g. sitio movido a
    nuevo dominio). El replay referencia el dominio viejo → SITE_CHANGED.
    Threat-model control P2 #8.
    """
    # Script referencia stub.old.example (dominio viejo)
    script = _make_signed_script(url="https://stub.old.example/home")
    store = await _load_store_with(script)

    orchestrator = BrowserOrchestrator(
        replay_store=store,
        replay_signing_key=_KEY,
    )

    # Whitelist actual ya NO incluye stub.old.example
    outcome = await orchestrator.execute_flow(
        site_id=script.site_id,
        flow_id=script.flow_id,
        tenant_scope=script.tenant_scope,
        domains_whitelist=("stub.new.example",),
    )

    assert outcome.mode == "discovery_needed", (
        f"Domain drift debe devolver discovery_needed. Got: {outcome.mode}"
    )

    loaded = await store.load_for(
        site_id=script.site_id,
        flow_id=script.flow_id,
        tenant_scope=script.tenant_scope,
    )
    assert loaded is None, "Script con domain drift debe quedar invalidado"


# ---------------------------------------------------------------------------
# (c) Happy path: script joven + domain OK → ejecuta replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_young_script_with_matching_domain_executes_replay() -> None:
    """Script joven + dominio en whitelist → modo replay_ok."""
    script = _make_signed_script(url="https://stub.local/home")
    store = await _load_store_with(script)

    driver = FakeBrowserDriver()
    orchestrator = BrowserOrchestrator(
        replay_store=store,
        replay_signing_key=_KEY,
        replay_max_age_days=90,
    )

    outcome = await orchestrator.execute_flow(
        site_id=script.site_id,
        flow_id=script.flow_id,
        tenant_scope=script.tenant_scope,
        domains_whitelist=("stub.local",),
        driver=driver,
    )

    assert outcome.mode == "replay_ok", (
        f"Script válido debe ejecutar replay. Got mode={outcome.mode}, "
        f"reason={outcome.invalidation_reason}, error={outcome.error}"
    )
    assert outcome.steps_executed >= 1
