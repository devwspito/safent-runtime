"""Tests ScreenStreamingAdapter (FR-053..FR-056)."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from hermes.agents_os.application.remote_control_orchestrator import (
    RemoteControlOrchestrator,
    RemoteControlScope,
    TokenCipher,
)
from hermes.agents_os.infrastructure.screen_streaming_adapter import (
    FakeScreenStreamingBackend,
    ScreenStreamingAdapter,
    StreamEvent,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(key=os.urandom(32), kid="kid-test")


@pytest.fixture
def session(cipher: TokenCipher):
    orch = RemoteControlOrchestrator(cipher=cipher)
    return orch.issue(
        node_installation_id=uuid4(),
        tenant_id=uuid4(),
        operator_id=uuid4(),
        scope=RemoteControlScope.OS_FULL_DESKTOP,
        dtls_fingerprint="ab:cd:ef",
        consent_id=uuid4(),
        local_operator_approved=True,
        ttl_seconds=600,
    )


@pytest.fixture
def backend() -> FakeScreenStreamingBackend:
    return FakeScreenStreamingBackend()


class TestLifecycle:
    def test_start_session_not_accepted_blocks(
        self, backend: FakeScreenStreamingBackend, session
    ) -> None:
        adapter = ScreenStreamingAdapter(backend=backend)
        # session está ISSUED — debe rechazar.
        with pytest.raises(RuntimeError):
            adapter.start(session=session)

    def test_start_active_publishes(
        self,
        backend: FakeScreenStreamingBackend,
        cipher: TokenCipher,
        session,
    ) -> None:
        orch = RemoteControlOrchestrator(cipher=cipher)
        accepted = orch.accept(session)
        active = orch.activate(accepted)

        events: list[tuple] = []
        adapter = ScreenStreamingAdapter(
            backend=backend,
            on_event=lambda sid, ev, payload: events.append(
                (sid, ev, payload)
            ),
        )
        pub = adapter.start(session=active)
        assert pub.state == "running"
        assert pub.dtls_fingerprint_local == active.dtls_fingerprint
        assert events[0][1] == StreamEvent.STARTED

    def test_stop_clears_active(
        self,
        backend: FakeScreenStreamingBackend,
        cipher: TokenCipher,
        session,
    ) -> None:
        orch = RemoteControlOrchestrator(cipher=cipher)
        active = orch.activate(orch.accept(session))
        adapter = ScreenStreamingAdapter(backend=backend)
        adapter.start(session=active)
        adapter.stop(session_id=active.session_id)
        assert active.session_id not in adapter.active_sessions()

    def test_handle_answer(
        self,
        backend: FakeScreenStreamingBackend,
        cipher: TokenCipher,
        session,
    ) -> None:
        orch = RemoteControlOrchestrator(cipher=cipher)
        active = orch.activate(orch.accept(session))
        adapter = ScreenStreamingAdapter(backend=backend)
        adapter.start(session=active)
        adapter.handle_answer(
            session_id=active.session_id, sdp_answer="v=0\no=- 0 0 IN IP4..."
        )
        # Backend registró RUNNING.
        kinds = [e[1] for e in backend.events]
        assert StreamEvent.RUNNING in kinds


class TestEvents:
    def test_on_event_callback_called_on_start(
        self,
        backend: FakeScreenStreamingBackend,
        cipher: TokenCipher,
        session,
    ) -> None:
        orch = RemoteControlOrchestrator(cipher=cipher)
        active = orch.activate(orch.accept(session))
        seen: list[StreamEvent] = []
        adapter = ScreenStreamingAdapter(
            backend=backend,
            on_event=lambda sid, ev, payload: seen.append(ev),
        )
        adapter.start(session=active)
        adapter.stop(session_id=active.session_id)
        assert StreamEvent.STARTED in seen
        assert StreamEvent.ENDED in seen
