"""Guard test — every config-sync allow-listed D-Bus verb must be EXPORTED.

F2 review fix: DbusRuntimeServiceWiring.set_agent_access_scope existed (wired,
tested at the wiring layer) but had NO matching @method() on the dbus-fast
ServiceInterface adapter. A verb "wired but not exported" resolves to None on
the client proxy (`getattr(iface, f"call_{member}", None)` in
hermes.shell_server.cowork.dbus_proxy._call) -> AgentUnavailable -> every
config-sync bundle carrying that verb call reports ok=False -> the sync loop
never advances last_applied_version and retries forever.

This test catches the WHOLE CLASS of bug in CI: for every verb the applier is
allowed to call, the exported Runtime1ServiceInterface must expose a matching
D-Bus method. It does not require a real bus — introspects the ServiceInterface
directly (same technique as tests/security/test_dbus_runtime1_contract.py).

MEDIUM-1 review fix (adversarial review of the A2A inbound consumer): the SAME
bug class hit the 3 inbound-delegation verbs (submit_inbound_delegation/
resolve_inbound_delegation/list_pending_delegations) — allow-listed on
org.hermes.Runtime1.conf and present on DbusRuntimeServiceWiring, but with no
@method() on Runtime1ServiceInterface, making the entire inbound-delegation
path unreachable (AgentUnavailable -> 'daemon_unavailable' forever).
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbus_fast")

from dbus_fast.proxy_object import BaseProxyInterface  # noqa: E402
from dbus_fast.service import ServiceInterface  # noqa: E402

from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (  # noqa: E402
    Runtime1ServiceInterface,
)
from hermes.config_sync.applier import _ALLOWED_VERBS  # noqa: E402

pytestmark = pytest.mark.unit


class _StubWiring:
    """Minimal wiring stub — only used for interface introspection, never called."""


@pytest.fixture()
def iface() -> Runtime1ServiceInterface:
    return Runtime1ServiceInterface(wiring=_StubWiring())  # type: ignore[arg-type]


def _exported_verbs(iface: Runtime1ServiceInterface) -> set[str]:
    """Snake-case verb names for every D-Bus method the interface exports.

    Mirrors exactly how a real client proxy resolves a verb name to a member:
    dbus-fast's BaseProxyInterface._to_snake_case(PascalCaseMethodName).
    """
    methods = ServiceInterface._get_methods(iface)
    return {BaseProxyInterface._to_snake_case(m.name) for m in methods}


class TestEveryAllowedVerbIsExported:
    def test_all_allowed_verbs_are_reachable_on_the_interface(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        exported = _exported_verbs(iface)
        missing = sorted(_ALLOWED_VERBS - exported)
        assert not missing, (
            f"Verb(s) allow-listed for config-sync but NOT exported on "
            f"Runtime1ServiceInterface (wired-but-unreachable): {missing}"
        )

    def test_set_agent_access_scope_specifically_exported(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        """Regression pin for the exact bug the review flagged (F2)."""
        assert "set_agent_access_scope" in _exported_verbs(iface)

    def test_clear_agent_access_scope_absent_verb_and_allowlist_agree(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        """clear_agent_access_scope is neither exported nor allow-listed —
        both sides agree it does not exist yet (no wired-but-unreachable gap)."""
        assert "clear_agent_access_scope" not in _ALLOWED_VERBS
        assert "clear_agent_access_scope" not in _exported_verbs(iface)


# Inbound-delegation verbs (FASE 3, A2A cross-human) — allow-listed on
# org.hermes.Runtime1.conf (both the `user="hermes"` block for
# SubmitInboundDelegation and the operator `context="default"` block for
# ResolveInboundDelegation/ListPendingDelegations) and present on
# DbusRuntimeServiceWiring. NOT part of config-sync's `_ALLOWED_VERBS`
# (a different allow-list, for cloud-pushed bundles) — tracked separately here
# per the review's explicit ask.
_DELEGATION_VERBS: frozenset[str] = frozenset(
    {
        "submit_inbound_delegation",
        "resolve_inbound_delegation",
        "list_pending_delegations",
    }
)


class TestInboundDelegationVerbsAreExported:
    """MEDIUM-1 review fix: same 'wired but not exported' bug class as F2
    (set_agent_access_scope) — the 3 inbound-delegation verbs existed on the
    wiring and were D-Bus-policy allow-listed, but had no @method() on
    Runtime1ServiceInterface, making the whole inbound path unreachable."""

    def test_all_delegation_verbs_are_reachable_on_the_interface(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        exported = _exported_verbs(iface)
        missing = sorted(_DELEGATION_VERBS - exported)
        assert not missing, (
            f"Inbound delegation verb(s) allow-listed on org.hermes.Runtime1.conf "
            f"but NOT exported on Runtime1ServiceInterface (wired-but-unreachable): "
            f"{missing}"
        )

    def test_submit_inbound_delegation_specifically_exported(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        """Regression pin for the exact bug the review flagged (MEDIUM-1)."""
        assert "submit_inbound_delegation" in _exported_verbs(iface)

    def test_resolve_inbound_delegation_specifically_exported(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        assert "resolve_inbound_delegation" in _exported_verbs(iface)

    def test_list_pending_delegations_specifically_exported(
        self, iface: Runtime1ServiceInterface
    ) -> None:
        assert "list_pending_delegations" in _exported_verbs(iface)
