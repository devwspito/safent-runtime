"""DbusRuntimeServiceWiring.reload_companion_presence (028 T017) — re-reads
companions.json/bearer for a slug and re-seeds + reconnects its MCP entry
WITHOUT restarting the daemon, closing the gap `_import_seed_companion_
servers` (boot-only) leaves for a companion installed AFTER the daemon is
already running (`safent companion install|repair`, T016).

Covers: authZ (shell-server uid only, same gate as
mint_companion_owner_assertion), the `not_installed` short-circuit when
companions.json does not yet validate for the slug, that a successful
reload calls the seed importer + reconnects the MCP entry + reports fresh
health, and that an MCP reconnect failure degrades to a reported health
state rather than raising (GetCompanionHealth right after is the source of
truth for reachability, not this call's own success).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hermes.agents_os.infrastructure import dbus_runtime_service as svc
from hermes.agents_os.infrastructure.companion_health_check import (
    CompanionHealthChecker,
    CompanionHealthReport,
)
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.shell_server.companions import CompanionEndpoint
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_AUTHORIZED_UID = 1000  # hermes-user (direct operator) — never allowed here
_PROXY_UID = 880  # shell-server process — the ONLY caller this verb allows
_UNAUTHORIZED_UID = 9999
_SLUG = "safent-ads"


class _FakeApprovalGate:
    async def approve(self, **_kwargs) -> str:
        return "token"

    async def reject(self, **_kwargs) -> None:
        return None


class _FakeMcpManager:
    pass


def _make_wiring(
    *, proxy_uid: int | None = _PROXY_UID, mcp_manager: object | None = _FakeMcpManager()
) -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_FakeApprovalGate(),
        authorized_uids=frozenset({_AUTHORIZED_UID}),
        proxy_uid=proxy_uid,
        mcp_server_manager=mcp_manager,
    )


def _endpoint(**overrides: object) -> CompanionEndpoint:
    fields = {
        "slug": _SLUG,
        "url": "https://ads.safent.internal:8443/mcp",
        "host": "ads.safent.internal",
        "ip": "10.201.0.10",
        "port": 8443,
        "ca_path": "/etc/hermes/companions/ads-ca.crt",
        "ca_fingerprint": "sha256:" + "a" * 64,
        "bearer_ref": "file:/etc/hermes/companions/ads.bearer",
    }
    fields.update(overrides)
    return CompanionEndpoint(**fields)  # type: ignore[arg-type]


def _healthy_report() -> CompanionHealthReport:
    return CompanionHealthReport(
        state="ready", reachable=True, http_status=200,
        contract_version="1.0.0", accounts_linked={"google": True},
    )


class TestUnauthorized:
    @pytest.mark.asyncio
    async def test_wrong_uid_raises_without_touching_companions(self) -> None:
        wiring = _make_wiring()
        with (
            patch("hermes.shell_server.companions.get_companion") as get_companion_mock,
            pytest.raises(DbusAuthorizationError),
        ):
            await wiring.reload_companion_presence(slug=_SLUG, sender_uid=_UNAUTHORIZED_UID)
        get_companion_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_operators_own_uid_is_also_refused(self) -> None:
        """This is stricter than most mutators: even hermes-user (the
        direct operator, allowed almost everywhere else) is NOT the
        shell-server's own uid."""
        wiring = _make_wiring()
        with pytest.raises(DbusAuthorizationError):
            await wiring.reload_companion_presence(slug=_SLUG, sender_uid=_AUTHORIZED_UID)

    @pytest.mark.asyncio
    async def test_no_proxy_uid_configured_fails_closed(self) -> None:
        wiring = _make_wiring(proxy_uid=None)
        with pytest.raises(DbusAuthorizationError):
            await wiring.reload_companion_presence(slug=_SLUG, sender_uid=_PROXY_UID)


class TestNotInstalled:
    @pytest.mark.asyncio
    async def test_missing_companion_short_circuits_without_reconnecting(self) -> None:
        wiring = _make_wiring()
        with (
            patch("hermes.shell_server.companions.get_companion", return_value=None),
            patch.object(svc, "_import_seed_companion_servers") as seed_mock,
            patch.object(svc, "_mcp_connect", new_callable=AsyncMock) as connect_mock,
        ):
            result = await wiring.reload_companion_presence(slug=_SLUG, sender_uid=_PROXY_UID)

        assert result == {"ok": False, "reason": "not_installed"}
        seed_mock.assert_not_called()
        connect_mock.assert_not_called()


class TestSuccessfulReload:
    @pytest.mark.asyncio
    async def test_reseeds_reconnects_and_reports_fresh_health(self) -> None:
        wiring = _make_wiring()
        endpoint = _endpoint()
        with (
            patch("hermes.shell_server.companions.get_companion", return_value=endpoint),
            patch.object(svc, "_import_seed_companion_servers") as seed_mock,
            patch.object(svc, "_mcp_connect", new_callable=AsyncMock) as connect_mock,
            patch.object(
                CompanionHealthChecker, "check", new_callable=AsyncMock,
                return_value=_healthy_report(),
            ),
        ):
            result = await wiring.reload_companion_presence(slug=_SLUG, sender_uid=_PROXY_UID)

        seed_mock.assert_called_once_with()
        connect_mock.assert_awaited_once()
        call = connect_mock.await_args
        assert call.args[0] is wiring._mcp_manager
        assert call.args[1] == _SLUG
        assert call.args[2] == endpoint.argv
        assert result == {"ok": True, "state": "ready", "reachable": True}

    @pytest.mark.asyncio
    async def test_no_mcp_manager_configured_skips_reconnect_but_still_reports_health(
        self,
    ) -> None:
        wiring = _make_wiring(mcp_manager=None)
        endpoint = _endpoint()
        with (
            patch("hermes.shell_server.companions.get_companion", return_value=endpoint),
            patch.object(svc, "_import_seed_companion_servers"),
            patch.object(svc, "_mcp_connect", new_callable=AsyncMock) as connect_mock,
            patch.object(
                CompanionHealthChecker, "check", new_callable=AsyncMock,
                return_value=_healthy_report(),
            ),
        ):
            result = await wiring.reload_companion_presence(slug=_SLUG, sender_uid=_PROXY_UID)

        connect_mock.assert_not_awaited()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_a_reconnect_failure_never_raises_and_still_reports_health(self) -> None:
        """GetCompanionHealth (called right after) is the source of truth
        for reachability — a transient MCP-side reconnect error must not
        turn into an exception the caller has to specially handle."""
        wiring = _make_wiring()
        endpoint = _endpoint()
        unreachable = CompanionHealthReport(state="unreachable", reachable=False)
        with (
            patch("hermes.shell_server.companions.get_companion", return_value=endpoint),
            patch.object(svc, "_import_seed_companion_servers"),
            patch.object(
                svc, "_mcp_connect", new_callable=AsyncMock, side_effect=RuntimeError("boom")
            ),
            patch.object(
                CompanionHealthChecker, "check", new_callable=AsyncMock,
                return_value=unreachable,
            ),
        ):
            result = await wiring.reload_companion_presence(slug=_SLUG, sender_uid=_PROXY_UID)

        assert result == {"ok": True, "state": "unreachable", "reachable": False}
