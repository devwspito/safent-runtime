"""T053 — DbusRuntimeClient + TaskStreamClient unit tests.

Tests the shell infrastructure layer client against:
- FakeDbusInterface (no real D-Bus bus required — CI-safe)
- FakeTaskStreamServer (no real AF_UNIX socket — CI-safe)

Validates:
- get_status() translates daemon states to health monitor vocabulary
- enqueue() calls the D-Bus Enqueue and returns (task_id, stream_path)
- enqueue() encodes conversation_id correctly
- request_pause() / request_resume() delegate to the D-Bus interface
- StreamFrame.from_json validates protocol schema (required fields, kinds)
- FakeTaskStreamServer yields pre-configured frames in order
- Frame kinds are dispatched correctly by consumers (no GTK required)
- SRP: no GTK import anywhere in the module under test

Tests that require a real D-Bus system bus or running daemon are marked
`requires_vm` and excluded from CI via pyproject.toml addopts.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from hermes.shell.infrastructure.dbus_fast_runtime_client import (
    DbusRuntimeClient,
    FakeDbusInterface,
    FakeTaskStreamServer,
    StreamFrame,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# StreamFrame deserialization
# ---------------------------------------------------------------------------


class TestStreamFrame:
    def test_delta_frame_round_trip(self) -> None:
        raw = json.dumps(
            {
                "kind": "delta",
                "task_id": "abc",
                "protocol_version": 1,
                "delta": "hello",
            }
        )
        frame = StreamFrame.from_json(raw)
        assert frame.kind == "delta"
        assert frame.task_id == "abc"
        assert frame.payload["delta"] == "hello"
        assert frame.protocol_version == 1

    def test_done_frame_with_error(self) -> None:
        raw = json.dumps(
            {
                "kind": "done",
                "task_id": "t1",
                "protocol_version": 1,
                "outcome": "failed",
                "error": "inference_not_configured",
            }
        )
        frame = StreamFrame.from_json(raw)
        assert frame.kind == "done"
        assert frame.payload["error"] == "inference_not_configured"
        assert frame.payload["outcome"] == "failed"

    def test_tool_call_frame(self) -> None:
        tc = {"name": "browser_click", "args": {"selector": "#go"}}
        raw = json.dumps(
            {
                "kind": "tool_call",
                "task_id": "t2",
                "protocol_version": 1,
                "tool_call": tc,
            }
        )
        frame = StreamFrame.from_json(raw)
        assert frame.kind == "tool_call"
        assert frame.payload["tool_call"] == tc

    def test_thinking_delta_frame(self) -> None:
        raw = json.dumps(
            {
                "kind": "thinking_delta",
                "task_id": "t3",
                "protocol_version": 1,
                "delta": "reasoning step",
            }
        )
        frame = StreamFrame.from_json(raw)
        assert frame.kind == "thinking_delta"
        assert frame.payload["delta"] == "reasoning step"

    def test_status_frame(self) -> None:
        raw = json.dumps(
            {
                "kind": "status",
                "task_id": "t4",
                "protocol_version": 1,
                "status": "in_progress",
            }
        )
        frame = StreamFrame.from_json(raw)
        assert frame.kind == "status"
        assert frame.payload["status"] == "in_progress"

    def test_error_frame(self) -> None:
        raw = json.dumps(
            {
                "kind": "error",
                "task_id": "t5",
                "protocol_version": 1,
                "error": "provider_5xx",
            }
        )
        frame = StreamFrame.from_json(raw)
        assert frame.kind == "error"
        assert frame.payload["error"] == "provider_5xx"

    def test_missing_kind_raises(self) -> None:
        raw = json.dumps({"task_id": "t1", "protocol_version": 1})
        with pytest.raises(ValueError, match="kind"):
            StreamFrame.from_json(raw)

    def test_missing_task_id_raises(self) -> None:
        raw = json.dumps({"kind": "delta", "protocol_version": 1, "delta": "x"})
        with pytest.raises(ValueError, match="task_id"):
            StreamFrame.from_json(raw)

    def test_missing_protocol_version_raises(self) -> None:
        raw = json.dumps({"kind": "delta", "task_id": "t1", "delta": "x"})
        with pytest.raises(ValueError, match="protocol_version"):
            StreamFrame.from_json(raw)

    def test_unknown_kind_raises(self) -> None:
        raw = json.dumps(
            {"kind": "garbage", "task_id": "t1", "protocol_version": 1}
        )
        with pytest.raises(ValueError, match="kind"):
            StreamFrame.from_json(raw)

    def test_invalid_json_raises(self) -> None:
        import json

        with pytest.raises(json.JSONDecodeError):
            StreamFrame.from_json("not-json{{{")

    def test_frame_is_frozen(self) -> None:
        raw = json.dumps(
            {"kind": "delta", "task_id": "t1", "protocol_version": 1, "delta": "x"}
        )
        frame = StreamFrame.from_json(raw)
        with pytest.raises((AttributeError, TypeError)):
            frame.kind = "done"  # type: ignore[misc]

    def test_payload_does_not_include_protocol_fields(self) -> None:
        raw = json.dumps(
            {"kind": "delta", "task_id": "t1", "protocol_version": 1, "delta": "hi"}
        )
        frame = StreamFrame.from_json(raw)
        assert "kind" not in frame.payload
        assert "task_id" not in frame.payload
        assert "protocol_version" not in frame.payload
        assert "delta" in frame.payload


# ---------------------------------------------------------------------------
# DbusRuntimeClient.get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    async def test_idle_state_returns_ok(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_status_response(state="idle")
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.get_status()
        assert result["status"] == "ok"
        assert result["raw_state"] == "idle"

    async def test_running_state_returns_ok(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_status_response(state="running", in_progress=1)
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.get_status()
        assert result["status"] == "ok"

    async def test_paused_state_returns_degraded(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_status_response(state="paused")
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.get_status()
        assert result["status"] == "degraded"
        assert result["raw_state"] == "paused"

    async def test_no_model_state_returns_degraded(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_status_response(state="no_model")
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.get_status()
        assert result["status"] == "degraded"

    async def test_bus_error_propagates(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_status_error(ConnectionRefusedError("bus down"))
        client = DbusRuntimeClient(dbus_interface=fake)
        with pytest.raises(ConnectionRefusedError):
            await client.get_status()

    async def test_default_when_queue_empty_returns_ok(self) -> None:
        fake = FakeDbusInterface()
        # No queued response — FakeDbusInterface defaults to idle
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.get_status()
        assert result["status"] == "ok"

    async def test_status_includes_queue_fields(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_status_response(state="running", pending=2, in_progress=1)
        client = DbusRuntimeClient(dbus_interface=fake)
        result = await client.get_status()
        assert result["pending"] == 2
        assert result["in_progress"] == 1


# ---------------------------------------------------------------------------
# DbusRuntimeClient.enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    async def test_enqueue_returns_task_id_and_stream_path(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_enqueue_result(task_id="tid1", stream_path="/ws/tasks/tid1")
        client = DbusRuntimeClient(dbus_interface=fake)
        task_id, stream_path = await client.enqueue(
            kind="chat_message", text="hello"
        )
        assert task_id == "tid1"
        assert stream_path == "/ws/tasks/tid1"

    async def test_enqueue_records_call(self) -> None:
        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.enqueue(kind="chat_message", text="test message")
        assert len(fake.enqueue_calls) == 1
        call = fake.enqueue_calls[0]
        assert call["trigger_kind"] == "chat_message"

    async def test_enqueue_with_conversation_id_passes_as_separate_arg(self) -> None:
        """conversation_id MUST be passed as its own 5th D-Bus arg, NOT JSON-in-text.

        The daemon Enqueue signature is (s, s, i, s, s) → (s, s).
        An old version encoded conversation_id in a JSON envelope inside text;
        that encoding has been removed because the daemon now has a dedicated
        parameter and enforces invariant I5 (chat_message must have conversation_id
        NOT NULL) via a CHECK constraint — NULL or absent conversation_id causes
        INSERT OR IGNORE to silently drop the task.
        """
        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.enqueue(
            kind="chat_message",
            text="pregunta",
            conversation_id="conv-123",
        )
        call = fake.enqueue_calls[0]
        # text is the plain user message — NOT a JSON envelope
        assert call["text"] == "pregunta"
        # conversation_id arrives as its own field
        assert call["conversation_id"] == "conv-123"

    async def test_enqueue_with_conversation_id_text_is_not_json(self) -> None:
        """Regression: text must not be a JSON string wrapping conversation_id."""
        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.enqueue(
            kind="chat_message",
            text="hello world",
            conversation_id="conv-456",
        )
        call = fake.enqueue_calls[0]
        # Verify that text is NOT a JSON-encoded envelope
        try:
            parsed = json.loads(call["text"])
            assert "conversation_id" not in parsed, (
                "conversation_id must NOT be encoded inside text"
            )
        except (json.JSONDecodeError, TypeError):
            pass  # plain text — this is the expected path

    async def test_enqueue_without_conversation_id_sends_empty_string(self) -> None:
        """No conversation_id → empty string passed to interface (not None)."""
        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.enqueue(kind="chat_message", text="hola")
        call = fake.enqueue_calls[0]
        assert call["text"] == "hola"
        assert call["conversation_id"] == ""

    async def test_enqueue_error_propagates(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_enqueue_error(RuntimeError("agent paused"))
        client = DbusRuntimeClient(dbus_interface=fake)
        with pytest.raises(RuntimeError, match="agent paused"):
            await client.enqueue(kind="chat_message", text="x")

    async def test_enqueue_default_auto_generates_task_id(self) -> None:
        fake = FakeDbusInterface()
        # No queued result — FakeDbusInterface auto-generates UUID
        client = DbusRuntimeClient(dbus_interface=fake)
        task_id, stream_path = await client.enqueue(
            kind="chat_message", text="auto"
        )
        assert task_id
        assert stream_path.startswith("/ws/tasks/")

    async def test_enqueue_passes_priority(self) -> None:
        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.enqueue(kind="chat_message", text="x", priority=10)
        assert fake.enqueue_calls[0]["priority"] == 10

    async def test_enqueue_passes_dedup_key(self) -> None:
        fake = FakeDbusInterface()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.enqueue(kind="chat_message", text="x", dedup_key="k1")
        assert fake.enqueue_calls[0]["dedup_key"] == "k1"


# ---------------------------------------------------------------------------
# DbusRuntimeClient.pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    async def test_pause_delegates_to_interface(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_pause_ok()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.request_pause(reason="manual")
        assert fake.pause_calls == ["manual"]

    async def test_resume_delegates_to_interface(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_resume_ok()
        client = DbusRuntimeClient(dbus_interface=fake)
        await client.request_resume()
        assert fake.resume_calls == 1

    async def test_pause_error_propagates(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_pause_error(RuntimeError("not authorized"))
        client = DbusRuntimeClient(dbus_interface=fake)
        with pytest.raises(RuntimeError, match="not authorized"):
            await client.request_pause(reason="force")

    async def test_resume_error_propagates(self) -> None:
        fake = FakeDbusInterface()
        fake.queue_resume_error(RuntimeError("not paused"))
        client = DbusRuntimeClient(dbus_interface=fake)
        with pytest.raises(RuntimeError, match="not paused"):
            await client.request_resume()


# ---------------------------------------------------------------------------
# FakeTaskStreamServer
# ---------------------------------------------------------------------------


class TestFakeTaskStreamServer:
    async def test_yields_queued_frames_in_order(self) -> None:
        server = FakeTaskStreamServer()
        server.queue_frame(kind="status", task_id="t1", status="in_progress")
        server.queue_frame(kind="delta", task_id="t1", delta="hello")
        server.queue_frame(kind="done", task_id="t1", outcome="completed")

        collected = []
        async for frame in server.frames():
            collected.append(frame)

        assert len(collected) == 3
        assert collected[0].kind == "status"
        assert collected[1].kind == "delta"
        assert collected[1].payload["delta"] == "hello"
        assert collected[2].kind == "done"

    async def test_error_raises_during_iteration(self) -> None:
        server = FakeTaskStreamServer()
        server.queue_frame(kind="delta", task_id="t1", delta="partial")
        server.queue_error(ConnectionAbortedError("stream cut"))

        frames = []
        with pytest.raises(ConnectionAbortedError, match="stream cut"):
            async for frame in server.frames():
                frames.append(frame)

        assert len(frames) == 1
        assert frames[0].payload["delta"] == "partial"

    async def test_empty_server_yields_nothing(self) -> None:
        server = FakeTaskStreamServer()
        frames = []
        async for frame in server.frames():
            frames.append(frame)
        assert frames == []

    async def test_different_servers_are_independent(self) -> None:
        """Two separate FakeTaskStreamServer instances are fully isolated."""
        server_a = FakeTaskStreamServer()
        server_b = FakeTaskStreamServer()
        server_a.queue_frame(kind="delta", task_id="t1", delta="a")
        server_b.queue_frame(kind="delta", task_id="t2", delta="b")
        server_b.queue_frame(kind="done", task_id="t2", outcome="completed")

        frames_a = [f async for f in server_a.frames()]
        frames_b = [f async for f in server_b.frames()]

        assert len(frames_a) == 1
        assert frames_a[0].payload["delta"] == "a"
        assert len(frames_b) == 2
        assert frames_b[1].kind == "done"


# ---------------------------------------------------------------------------
# SRP: no GTK import in the infrastructure module
# ---------------------------------------------------------------------------


def test_dbus_client_module_does_not_import_gtk() -> None:
    """SRP: dbus_fast_runtime_client must not pull in GTK / GLib / gi."""
    import sys

    import hermes.shell.infrastructure.dbus_fast_runtime_client as mod

    module_file = mod.__file__ or ""
    with open(module_file) as f:
        src = f.read()
    assert "gi.require_version" not in src, "module imports GTK"
    assert "from gi.repository" not in src, "module imports gi.repository"
    gtk_modules = [k for k in sys.modules if k.startswith("gi.repository")]
    # The check is on the source (the module may be loaded in a GTK env);
    # what matters is that the module itself does not reference gi.
    _ = gtk_modules  # collected for informational purposes


# ---------------------------------------------------------------------------
# Health monitor integration: get_status feeds RuntimeBackendHealthMonitor
# ---------------------------------------------------------------------------


async def test_health_monitor_uses_dbus_client_get_status() -> None:
    """Verify DbusRuntimeClient.get_status() satisfies the monitor's poll contract."""
    from hermes.shell.application.runtime_backend_health_monitor import (
        MonitorConfig,
        RuntimeBackendHealthMonitor,
    )
    from hermes.shell.domain.shell_session import RuntimeLinkState

    fake = FakeDbusInterface()
    client = DbusRuntimeClient(dbus_interface=fake)

    observed: list[RuntimeLinkState] = []

    class _NoopBus:
        def subscribe_name_owner_changed(self, _cb) -> None:
            pass

    monitor = RuntimeBackendHealthMonitor(
        runtime_port=client,
        event_bus=_NoopBus(),
        config=MonitorConfig(
            poll_interval_s=0.01,
            backoff_initial_s=0.01,
            backoff_cap_s=0.08,
            backoff_multiplier=2.0,
            grace_period_s=0.04,
            max_retries_before_offline=3,
        ),
        on_state_change=observed.append,
    )

    # Queue: ok → degraded (paused)
    fake.queue_status_response(state="idle")
    fake.queue_status_response(state="paused")

    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert RuntimeLinkState.CONNECTED in observed
    assert RuntimeLinkState.DEGRADED in observed


# ---------------------------------------------------------------------------
# _resolve_operator_uid — priority chain (env > pwd > getuid)
# ---------------------------------------------------------------------------


class TestResolveOperatorUid:
    """Unit tests for _resolve_operator_uid() in hermes.runtime.__main__.

    Importing the module pulls in its top-level side-effects only through
    the lazy-import pattern, so we import the function directly.
    """

    def _fn(self):
        from hermes.runtime.__main__ import _resolve_operator_uid  # noqa: PLC0415
        return _resolve_operator_uid

    def test_env_var_takes_precedence(self, monkeypatch) -> None:
        """HERMES_OPERATOR_UID env → returned as int, no pwd lookup."""
        monkeypatch.setenv("HERMES_OPERATOR_UID", "1234")
        result = self._fn()()
        assert result == 1234

    def test_env_var_strips_whitespace(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_OPERATOR_UID", "  999  ")
        result = self._fn()()
        assert result == 999

    def test_no_env_falls_through_to_pwd_or_getuid(self, monkeypatch) -> None:
        """When env is absent the function returns an int (pwd or getuid)."""
        monkeypatch.delenv("HERMES_OPERATOR_UID", raising=False)
        result = self._fn()()
        assert isinstance(result, int)
        assert result >= 0

    def test_no_env_hermes_user_absent_falls_to_getuid(self, monkeypatch) -> None:
        """When hermes-user does not exist in passwd, falls back to os.getuid()."""
        import os  # noqa: PLC0415
        import pwd as _pwd  # noqa: PLC0415

        monkeypatch.delenv("HERMES_OPERATOR_UID", raising=False)

        original_getpwnam = _pwd.getpwnam

        def _raise_keyerror(name):
            if name == "hermes-user":
                raise KeyError("hermes-user not found")
            return original_getpwnam(name)

        monkeypatch.setattr(_pwd, "getpwnam", _raise_keyerror)
        result = self._fn()()
        assert result == os.getuid()

    def test_env_set_to_hermes_user_uid_returns_that_uid(self, monkeypatch) -> None:
        """Explicit override always wins, even if value matches getuid()."""
        monkeypatch.setenv("HERMES_OPERATOR_UID", "1000")
        assert self._fn()() == 1000


# ---------------------------------------------------------------------------
# RealDbusInterface — adapter surface (unit, no live bus)
# ---------------------------------------------------------------------------


class TestRealDbusInterfaceAdapter:
    """RealDbusInterface adapts lowercase dbus-fast proxy methods to the
    uppercase call_* surface expected by DbusRuntimeClient.

    We stub the proxy interface to verify the mapping without a real bus.
    """

    def _make_fake_proxy(self, **overrides):
        """Return a minimal object that looks like a dbus-fast proxy interface."""

        class _FakeProxy:
            async def call_get_queue_status(self):
                return {"state": "idle", "pending": 0, "in_progress": 0,
                        "pending_approval": 0, "last_audit_head": ""}

            async def call_enqueue(self, trigger_kind, text, priority, dedup_key, conversation_id, operator_token=""):
                return ("task-uuid-123", f"/ws/tasks/task-uuid-123")

            async def call_pause(self, reason):
                return True

            async def call_resume(self):
                return True

        proxy = _FakeProxy()
        for k, v in overrides.items():
            setattr(proxy, k, v)
        return proxy

    async def test_call_GetQueueStatus_maps_to_lowercase(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import RealDbusInterface

        proxy = self._make_fake_proxy()
        adapter = RealDbusInterface(proxy_interface=proxy)
        result = await adapter.call_GetQueueStatus()
        assert result["state"] == "idle"

    async def test_call_Enqueue_maps_to_lowercase_and_returns_EnqueueResult(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (
            RealDbusInterface,
            EnqueueResult,
        )

        captured: list[tuple] = []

        class _CapturingProxy:
            async def call_enqueue(self, trigger_kind, text, priority, dedup_key, conversation_id, operator_token=""):
                captured.append((trigger_kind, text, priority, dedup_key, conversation_id))
                return ("tid-abc", "/ws/tasks/tid-abc")

        adapter = RealDbusInterface(proxy_interface=_CapturingProxy())
        result = await adapter.call_Enqueue(
            "chat_message", "hello", 0, "", "conv-xyz"
        )
        assert isinstance(result, EnqueueResult)
        assert result.task_id == "tid-abc"
        assert result.stream_path == "/ws/tasks/tid-abc"
        assert captured[0] == ("chat_message", "hello", 0, "", "conv-xyz")

    async def test_call_Enqueue_conversation_id_forwarded(self) -> None:
        """conversation_id must NOT be dropped or JSON-encoded by the adapter."""
        from hermes.shell.infrastructure.dbus_fast_runtime_client import RealDbusInterface

        received_conv_id: list[str] = []

        class _Proxy:
            async def call_enqueue(self, trigger_kind, text, priority, dedup_key, conversation_id, operator_token=""):
                received_conv_id.append(conversation_id)
                return ("t", "/ws/tasks/t")

        adapter = RealDbusInterface(proxy_interface=_Proxy())
        await adapter.call_Enqueue("chat_message", "msg", 0, "", "my-conv-id")
        assert received_conv_id == ["my-conv-id"]

    async def test_call_Pause_maps_to_lowercase(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import RealDbusInterface

        proxy = self._make_fake_proxy()
        adapter = RealDbusInterface(proxy_interface=proxy)
        result = await adapter.call_Pause("manual")
        assert result is True

    async def test_call_Resume_maps_to_lowercase(self) -> None:
        from hermes.shell.infrastructure.dbus_fast_runtime_client import RealDbusInterface

        proxy = self._make_fake_proxy()
        adapter = RealDbusInterface(proxy_interface=proxy)
        result = await adapter.call_Resume()
        assert result is True

    async def test_DbusRuntimeClient_with_RealDbusInterface_adapter(self) -> None:
        """End-to-end: DbusRuntimeClient + RealDbusInterface + fake proxy."""
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (
            DbusRuntimeClient,
            RealDbusInterface,
        )

        captured: list[tuple] = []

        class _Proxy:
            async def call_get_queue_status(self):
                return {"state": "idle", "pending": 0, "in_progress": 0,
                        "pending_approval": 0, "last_audit_head": ""}

            async def call_enqueue(self, trigger_kind, text, priority, dedup_key, conversation_id, operator_token=""):
                captured.append((trigger_kind, text, priority, dedup_key, conversation_id))
                return ("t1", "/ws/tasks/t1")

        adapter = RealDbusInterface(proxy_interface=_Proxy())
        client = DbusRuntimeClient(dbus_interface=adapter)

        status = await client.get_status()
        assert status["status"] == "ok"

        task_id, stream_path = await client.enqueue(
            kind="chat_message",
            text="hola daemon",
            conversation_id="conv-999",
        )
        assert task_id == "t1"
        assert stream_path == "/ws/tasks/t1"
        # Verify conversation_id arrives as 5th positional, text is plain
        assert captured[0] == ("chat_message", "hola daemon", 0, "", "conv-999")
