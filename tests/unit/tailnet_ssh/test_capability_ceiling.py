"""resolve_execution_identity — the shared capability-ceiling decision for
governed-SSH hosts (spec 002 US3, D-4)."""

from __future__ import annotations

import pytest

from hermes.tailnet_ssh.application.capability_ceiling import resolve_execution_identity
from hermes.tailnet_ssh.application.ports import HostGrant
from hermes.tailnet_ssh.domain.errors import InvalidRemoteIdentityError, SshCapabilityDeniedError

pytestmark = pytest.mark.unit


class TestNoCeilingForNonCloudHosts:
    def test_none_grant_returns_none_identity(self) -> None:
        assert resolve_execution_identity(None, host="db1", capability="exec") is None

    def test_local_grant_returns_none_identity(self) -> None:
        grant = HostGrant(managed_by=None, identity=None, capabilities=None)
        assert resolve_execution_identity(grant, host="db1", capability="exec") is None

    def test_local_grant_never_denies_any_capability(self) -> None:
        """Today's behaviour, unchanged: a local host has no ceiling at
        all — every capability is implicitly allowed."""
        grant = HostGrant(managed_by=None, identity=None, capabilities=None)
        for capability in ("exec", "file_read", "file_write", "anything"):
            assert resolve_execution_identity(grant, host="db1", capability=capability) is None


class TestCloudCeilingAllows:
    def test_capability_within_ceiling_returns_declared_identity(self) -> None:
        grant = HostGrant(
            managed_by="cloud", identity="deploy", capabilities=frozenset({"exec"})
        )
        assert resolve_execution_identity(grant, host="db1", capability="exec") == "deploy"

    def test_multi_capability_grant_allows_each_declared_capability(self) -> None:
        grant = HostGrant(
            managed_by="cloud",
            identity="auditor",
            capabilities=frozenset({"file_read", "file_write"}),
        )
        assert resolve_execution_identity(grant, host="db1", capability="file_read") == "auditor"
        assert resolve_execution_identity(grant, host="db1", capability="file_write") == "auditor"


class TestCloudCeilingDenies:
    def test_capability_outside_ceiling_raises(self) -> None:
        grant = HostGrant(
            managed_by="cloud", identity="auditor", capabilities=frozenset({"file_read"})
        )
        with pytest.raises(SshCapabilityDeniedError):
            resolve_execution_identity(grant, host="db1", capability="exec")

    def test_empty_capabilities_denies_everything(self) -> None:
        """Fail-closed: a corrupted/empty ceiling denies, never allows."""
        grant = HostGrant(managed_by="cloud", identity="deploy", capabilities=frozenset())
        with pytest.raises(SshCapabilityDeniedError):
            resolve_execution_identity(grant, host="db1", capability="exec")

    def test_none_capabilities_on_a_cloud_grant_denies_everything(self) -> None:
        grant = HostGrant(managed_by="cloud", identity="deploy", capabilities=None)
        with pytest.raises(SshCapabilityDeniedError):
            resolve_execution_identity(grant, host="db1", capability="exec")

    def test_denial_message_names_the_host_and_capability(self) -> None:
        grant = HostGrant(
            managed_by="cloud", identity="auditor", capabilities=frozenset({"file_read"})
        )
        with pytest.raises(SshCapabilityDeniedError, match="exec"):
            resolve_execution_identity(grant, host="db1.tailxxxx.ts.net", capability="exec")


class TestMalformedStoredIdentity:
    def test_invalid_identity_on_an_otherwise_allowed_capability_raises(self) -> None:
        """Defense-in-depth: the allow-list file is its own trust boundary —
        a corrupted identity is refused rather than passed to ssh verbatim."""
        grant = HostGrant(
            managed_by="cloud", identity="Not Valid!", capabilities=frozenset({"exec"})
        )
        with pytest.raises(InvalidRemoteIdentityError):
            resolve_execution_identity(grant, host="db1", capability="exec")
