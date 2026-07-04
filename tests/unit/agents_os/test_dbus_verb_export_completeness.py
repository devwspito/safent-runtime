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
