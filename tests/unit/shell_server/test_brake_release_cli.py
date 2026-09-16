"""brake_release_cli (R17, spec 025 matriz item R17) — the sovereign,
host-only fallback that releases the emergency brake without MFA.

Before this existed, `safent brake release` did not exist at all: a brake
engaged on a fresh install (no TOTP enrolled, no device/OS password set)
had NO release path — `POST /api/v1/security/kill-switch` fails closed 403
`invalid_device_password` in that state (security_api.py), and the only
documented way out was enrolling MFA with the brake still engaged.

This module is invoked via `podman exec -u hermes-user $NAME python3 -m
hermes.shell_server.security.brake_release_cli release` by the `safent`
host CLI — never over HTTP, never with a REST bearer. Covers:
  - `_release_brake()` calls exactly `org.hermes.Runtime1.Resume()` on the
    well-known bus name/object path the rest of the shell-server uses
    (dbus_proxy.py) — never a different verb, never with an operator_token
    argument (Resume() takes none; direct-uid authorization is the point).
  - `cmd_release()` maps a truthy Resume() result to exit 0 + "[ok]", a
    falsy result and any raised exception to exit 1 + a clear stderr
    message — the CLI is fail-closed: it must never print success unless
    Resume() actually reported success.
"""

from __future__ import annotations

import asyncio

import dbus_fast.aio
import pytest

from hermes.shell_server.security import brake_release_cli

pytestmark = pytest.mark.unit


class _FakeInterface:
    def __init__(self, *, result: bool = True, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls = 0
        self.reasons: list[str] = []

    async def call_resume(self, reason: str) -> bool:
        self.calls += 1
        self.reasons.append(reason)
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeProxyObject:
    def __init__(self, iface: _FakeInterface) -> None:
        self._iface = iface

    def get_interface(self, name: str) -> _FakeInterface:
        assert name == "org.hermes.Runtime1"
        return self._iface


class _FakeBus:
    def __init__(self, iface: _FakeInterface) -> None:
        self._iface = iface
        self.connected = True
        self.disconnect_called = False
        self.introspected_with: tuple[str, str] | None = None

    async def connect(self) -> _FakeBus:
        return self

    async def introspect(self, well_known_name: str, object_path: str) -> object:
        self.introspected_with = (well_known_name, object_path)
        return object()

    def get_proxy_object(self, well_known_name: str, object_path: str, _introspection: object) -> _FakeProxyObject:
        assert (well_known_name, object_path) == self.introspected_with
        return _FakeProxyObject(self._iface)

    def disconnect(self) -> None:
        self.disconnect_called = True


def _install_fake_bus(monkeypatch: pytest.MonkeyPatch, iface: _FakeInterface) -> _FakeBus:
    """`_release_brake` does `from dbus_fast.aio import MessageBus` lazily —
    patching the real module's attribute is what that import sees."""
    bus_holder: dict[str, _FakeBus] = {}

    def _fake_message_bus(*, bus_type: object) -> _FakeBus:
        bus = _FakeBus(iface)
        bus_holder["bus"] = bus
        return bus

    monkeypatch.setattr(dbus_fast.aio, "MessageBus", _fake_message_bus)
    return bus_holder  # type: ignore[return-value]  # populated after connect()


class TestReleaseBrakeCallsTheExactDbusVerb:
    def test_calls_resume_on_the_runtime_interface_and_disconnects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        iface = _FakeInterface(result=True)
        holder = _install_fake_bus(monkeypatch, iface)

        released = asyncio.run(brake_release_cli._release_brake())

        assert released is True
        assert iface.calls == 1
        bus = holder["bus"]
        assert bus.introspected_with == ("org.hermes.Runtime", "/org/hermes/Runtime")
        assert bus.disconnect_called is True

    def test_calls_resume_with_the_host_cli_audit_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Security review 2026-09-10 (MEDIUM finding): this exact string is
        what makes the signed AGENT_RESUMED entry distinguishable from a
        TOTP/device-password UI release — see security_api.py's own
        "totp"/"device_password" reasons on the REST path."""
        iface = _FakeInterface(result=True)
        _install_fake_bus(monkeypatch, iface)

        asyncio.run(brake_release_cli._release_brake())

        assert iface.reasons == ["host_cli"]
        assert iface.reasons == [brake_release_cli._RELEASE_REASON]

    def test_a_false_resume_result_is_propagated_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        iface = _FakeInterface(result=False)
        _install_fake_bus(monkeypatch, iface)

        released = asyncio.run(brake_release_cli._release_brake())

        assert released is False

    def test_a_dbus_error_propagates_to_the_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        iface = _FakeInterface(exc=RuntimeError("UID 0 no autorizado"))
        _install_fake_bus(monkeypatch, iface)

        with pytest.raises(RuntimeError, match="no autorizado"):
            asyncio.run(brake_release_cli._release_brake())


class TestCmdReleaseExitCodesAndMessages:
    def test_success_prints_ok_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(brake_release_cli, "_release_brake", _async_returning(True))

        rc = brake_release_cli.cmd_release()

        assert rc == 0
        assert "[ok]" in capsys.readouterr().out

    def test_false_result_is_a_clear_failure_not_a_silent_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(brake_release_cli, "_release_brake", _async_returning(False))

        rc = brake_release_cli.cmd_release()

        captured = capsys.readouterr()
        assert rc == 1
        assert "[ok]" not in captured.out
        assert "brake may still be engaged" in captured.err

    def test_an_exception_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def _raise() -> bool:
            raise RuntimeError("UID 0 no autorizado para 'request_resume'")

        monkeypatch.setattr(brake_release_cli, "_release_brake", _raise)

        rc = brake_release_cli.cmd_release()

        captured = capsys.readouterr()
        assert rc == 1
        assert "Could not release the brake" in captured.err
        assert "no autorizado" in captured.err


class TestMainDispatch:
    def test_release_subcommand_invokes_cmd_release(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"n": 0}
        monkeypatch.setattr(brake_release_cli, "cmd_release", lambda: called.__setitem__("n", called["n"] + 1) or 0)

        rc = brake_release_cli.main(["release"])

        assert rc == 0
        assert called["n"] == 1

    def test_unknown_subcommand_is_rejected_by_argparse(self) -> None:
        with pytest.raises(SystemExit):
            brake_release_cli.main(["bogus"])


def _async_returning(value: bool):
    async def _inner() -> bool:
        return value

    return _inner
