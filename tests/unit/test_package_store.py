"""Unit tests for the Package Store module.

Coverage:
- Domain PackageRef invariants (forbidden chars, blank id).
- PackageStoreService: list, search, install, uninstall, op_status.
- DbusRuntimeServiceWiring: list/search read-only (no authZ), install/uninstall authZ.
- FakeDbusInterface stubs for Package Store.
- DbusRuntimeClient high-level methods parse JSON.

All subprocess calls are mocked — no real flatpak/dnf required.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from hermes.package_store.domain.package import (
    PackageInfo,
    PackageOpResult,
    PackageOpStatus,
    PackageOpStatusSnapshot,
    PackageRef,
    PackageSource,
)

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000


# ---------------------------------------------------------------------------
# Domain: PackageRef invariants
# ---------------------------------------------------------------------------


class TestPackageRefInvariants:
    def test_valid_flatpak_ref(self) -> None:
        ref = PackageRef(source=PackageSource.FLATPAK, package_id="org.inkscape.Inkscape")
        assert ref.package_id == "org.inkscape.Inkscape"

    def test_valid_rpm_ref(self) -> None:
        ref = PackageRef(source=PackageSource.RPM, package_id="gimp")
        assert ref.source == PackageSource.RPM

    def test_blank_package_id_raises(self) -> None:
        with pytest.raises(ValueError, match="blank"):
            PackageRef(source=PackageSource.FLATPAK, package_id="   ")

    def test_semicolon_in_id_raises(self) -> None:
        with pytest.raises(ValueError, match="forbidden"):
            PackageRef(source=PackageSource.FLATPAK, package_id="foo;bar")

    def test_backtick_in_id_raises(self) -> None:
        with pytest.raises(ValueError, match="forbidden"):
            PackageRef(source=PackageSource.RPM, package_id="gimp`whoami")


# ---------------------------------------------------------------------------
# Application: PackageStoreService
# ---------------------------------------------------------------------------


def _fake_catalog(
    installed: list[PackageInfo] | None = None,
    search_results: list[PackageInfo] | None = None,
) -> MagicMock:
    catalog = MagicMock()
    catalog.list_installed.return_value = installed or []
    catalog.search.return_value = search_results or []
    return catalog


def _fake_manager(
    op_id: str = "abc123",
    status: PackageOpStatus = PackageOpStatus.SUCCESS,
) -> MagicMock:
    manager = MagicMock()
    ref = PackageRef(source=PackageSource.FLATPAK, package_id="org.inkscape.Inkscape")
    manager.start_install.return_value = PackageOpResult(op_id=op_id, ref=ref)
    manager.start_uninstall.return_value = PackageOpResult(op_id=op_id, ref=ref)
    manager.get_op_status.return_value = PackageOpStatusSnapshot(
        op_id=op_id, status=status
    )
    return manager


def _sample_package(installed: bool = False) -> PackageInfo:
    return PackageInfo(
        ref=PackageRef(source=PackageSource.FLATPAK, package_id="org.inkscape.Inkscape"),
        name="Inkscape",
        description="Vector graphics editor",
        version_available="1.3.2",
        version_installed="1.3.2" if installed else None,
    )


class TestPackageStoreServiceListInstalled:
    def test_returns_installed_packages(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(
            catalog=_fake_catalog(installed=[_sample_package(installed=True)]),
            manager=_fake_manager(),
        )
        result = svc.list_installed(source="flatpak")
        assert len(result) == 1
        assert result[0]["installed"] is True
        assert result[0]["package_id"] == "org.inkscape.Inkscape"

    def test_invalid_source_raises(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(catalog=_fake_catalog(), manager=_fake_manager())
        with pytest.raises(ValueError):
            svc.list_installed(source="pip")


class TestPackageStoreServiceSearch:
    def test_returns_search_results(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(
            catalog=_fake_catalog(search_results=[_sample_package()]),
            manager=_fake_manager(),
        )
        result = svc.search(query="inkscape", source="flatpak")
        assert len(result) == 1
        assert result[0]["name"] == "Inkscape"
        assert result[0]["installed"] is False

    def test_source_all_passes_none_to_catalog(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        catalog = _fake_catalog()
        svc = PackageStoreService(catalog=catalog, manager=_fake_manager())
        svc.search(query="gimp", source="all")
        catalog.search.assert_called_once_with(query="gimp", source=None)

    def test_empty_source_passes_none_to_catalog(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        catalog = _fake_catalog()
        svc = PackageStoreService(catalog=catalog, manager=_fake_manager())
        svc.search(query="gimp", source="")
        catalog.search.assert_called_once_with(query="gimp", source=None)

    def test_query_truncated_at_200_chars(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        catalog = _fake_catalog()
        svc = PackageStoreService(catalog=catalog, manager=_fake_manager())
        long_query = "x" * 500
        svc.search(query=long_query, source="all")
        _, kwargs = catalog.search.call_args
        assert len(kwargs["query"]) == 200


class TestPackageStoreServiceInstallUninstall:
    def test_start_install_returns_op_id(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(catalog=_fake_catalog(), manager=_fake_manager(op_id="op1"))
        result = svc.start_install(source="flatpak", package_id="org.inkscape.Inkscape")
        assert result["op_id"] == "op1"

    def test_start_uninstall_returns_op_id(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(catalog=_fake_catalog(), manager=_fake_manager(op_id="op2"))
        result = svc.start_uninstall(source="rpm", package_id="gimp")
        assert result["op_id"] == "op2"

    def test_invalid_source_raises(self) -> None:
        """The app service validates and RAISES; the D-Bus adapter translates the
        ValueError into a {"error": ...} dict (see TestWiringPackageStoreAuthZ)."""
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(catalog=_fake_catalog(), manager=_fake_manager())
        with pytest.raises(ValueError):
            svc.start_install(source="npm", package_id="react")

    def test_blank_package_id_raises(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(catalog=_fake_catalog(), manager=_fake_manager())
        with pytest.raises(ValueError, match="blank"):
            svc.start_install(source="flatpak", package_id="   ")


class TestPackageStoreServiceGetOpStatus:
    def test_returns_status_dict(self) -> None:
        from hermes.package_store.application.package_store_service import PackageStoreService

        svc = PackageStoreService(
            catalog=_fake_catalog(),
            manager=_fake_manager(op_id="xyz", status=PackageOpStatus.RUNNING),
        )
        result = svc.get_op_status(op_id="xyz")
        assert result["status"] == "running"
        assert result["op_id"] == "xyz"


# ---------------------------------------------------------------------------
# DbusRuntimeServiceWiring: Package Store verbs
# ---------------------------------------------------------------------------


def _wiring() -> "DbusRuntimeServiceWiring":
    from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring  # noqa: PLC0415

    return DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=None,
        authorized_uids=frozenset({_OPERATOR_UID}),
    )


class TestWiringPackageStoreReadOnly:
    def test_list_installed_no_authz_required(self) -> None:
        """list_installed_packages is read-only — no UID needed."""
        wiring = _wiring()
        with patch.object(
            wiring._package_store_service(),
            "list_installed",
            return_value=[],
        ):
            result = wiring.list_installed_packages(source="flatpak")
        assert result == []

    def test_search_no_authz_required(self) -> None:
        wiring = _wiring()
        with patch.object(
            wiring._package_store_service(),
            "search",
            return_value=[],
        ):
            result = wiring.search_packages(query="inkscape", source="flatpak")
        assert result == []


class TestWiringPackageStoreAuthZ:
    def test_install_requires_authorized_uid(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import DbusAuthorizationError  # noqa: PLC0415

        wiring = _wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.install_package(source="flatpak", package_id="org.inkscape.Inkscape", sender_uid=999)

    def test_uninstall_requires_authorized_uid(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import DbusAuthorizationError  # noqa: PLC0415

        wiring = _wiring()
        with pytest.raises(DbusAuthorizationError):
            wiring.uninstall_package(source="flatpak", package_id="org.inkscape.Inkscape", sender_uid=999)

    def test_install_authorized_uid_returns_op_id(self) -> None:
        wiring = _wiring()
        svc = wiring._package_store_service()
        # install_package runs a pre-install Security Center scan before delegating.
        # Mock it (fail-open None → proceed) to isolate the wiring's authZ+delegation
        # from the security-center infrastructure (which writes to /var/lib/hermes).
        with patch.object(wiring, "_scan_install_target", return_value=None), \
                patch.object(svc, "start_install", return_value={"op_id": "op99"}):
            result = wiring.install_package(
                source="flatpak", package_id="org.inkscape.Inkscape", sender_uid=_OPERATOR_UID
            )
        assert result["op_id"] == "op99"

    def test_install_invalid_source_returns_error_key(self) -> None:
        """The D-Bus boundary must translate the service's ValueError into an
        {"error": ...} dict rather than propagate an exception to the caller."""
        wiring = _wiring()
        with patch.object(wiring, "_scan_install_target", return_value=None):
            result = wiring.install_package(
                source="npm", package_id="react", sender_uid=_OPERATOR_UID
            )
        assert "error" in result

    def test_install_blank_package_id_returns_error_key(self) -> None:
        wiring = _wiring()
        with patch.object(wiring, "_scan_install_target", return_value=None):
            result = wiring.install_package(
                source="flatpak", package_id="   ", sender_uid=_OPERATOR_UID
            )
        assert "error" in result

    def test_uninstall_authorized_uid_returns_op_id(self) -> None:
        wiring = _wiring()
        svc = wiring._package_store_service()
        with patch.object(svc, "start_uninstall", return_value={"op_id": "op88"}):
            result = wiring.uninstall_package(
                source="rpm", package_id="gimp", sender_uid=_OPERATOR_UID
            )
        assert result["op_id"] == "op88"


class TestWiringGetPkgOpStatus:
    def test_returns_unknown_for_bogus_op_id(self) -> None:
        wiring = _wiring()
        result = wiring.get_pkg_op_status(op_id="nonexistent")
        assert result["status"] == "unknown"


# ---------------------------------------------------------------------------
# FakeDbusInterface stubs
# ---------------------------------------------------------------------------


class TestFakeDbusInterfacePackageStoreStubs:
    @pytest.mark.asyncio
    async def test_list_installed_returns_empty(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import FakeDbusInterface  # noqa: PLC0415

        fake = FakeDbusInterface()
        result = await fake.call_ListInstalledPackages("flatpak")
        assert json.loads(result) == []

    @pytest.mark.asyncio
    async def test_search_returns_empty(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import FakeDbusInterface  # noqa: PLC0415

        fake = FakeDbusInterface()
        result = await fake.call_SearchPackages("inkscape", "all")
        assert json.loads(result) == []

    @pytest.mark.asyncio
    async def test_install_returns_empty_object(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import FakeDbusInterface  # noqa: PLC0415

        fake = FakeDbusInterface()
        result = await fake.call_InstallPackage("flatpak", "org.inkscape.Inkscape")
        assert json.loads(result) == {}

    @pytest.mark.asyncio
    async def test_get_pkg_op_status_returns_unknown(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import FakeDbusInterface  # noqa: PLC0415

        fake = FakeDbusInterface()
        result = await fake.call_GetPkgOpStatus("some-op-id")
        parsed = json.loads(result)
        assert parsed["status"] == "unknown"
        assert parsed["op_id"] == "some-op-id"


# ---------------------------------------------------------------------------
# DbusRuntimeClient high-level methods
# ---------------------------------------------------------------------------


class TestDbusRuntimeClientPackageStoreMethods:
    @pytest.mark.asyncio
    async def test_list_installed_parses_json(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import DbusRuntimeClient, FakeDbusInterface  # noqa: PLC0415

        client = DbusRuntimeClient(dbus_interface=FakeDbusInterface())
        result = await client.list_installed_packages("flatpak")
        assert result == []

    @pytest.mark.asyncio
    async def test_search_parses_json(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import DbusRuntimeClient, FakeDbusInterface  # noqa: PLC0415

        client = DbusRuntimeClient(dbus_interface=FakeDbusInterface())
        result = await client.search_packages("inkscape")
        assert result == []

    @pytest.mark.asyncio
    async def test_install_parses_json(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import DbusRuntimeClient, FakeDbusInterface  # noqa: PLC0415

        client = DbusRuntimeClient(dbus_interface=FakeDbusInterface())
        result = await client.install_package("flatpak", "org.inkscape.Inkscape")
        assert result == {}

    @pytest.mark.asyncio
    async def test_get_pkg_op_status_parses_json(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import DbusRuntimeClient, FakeDbusInterface  # noqa: PLC0415

        client = DbusRuntimeClient(dbus_interface=FakeDbusInterface())
        result = await client.get_pkg_op_status("op-xyz")
        assert result["status"] == "unknown"
