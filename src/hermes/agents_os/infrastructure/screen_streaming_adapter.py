"""ScreenStreamingAdapter — SO-level remote control vía GStreamer/WebRTC.

Spec 003 FR-053..FR-056 (BLOQUEANTES). Sustituye a cualquier control
remoto embebido en Chromium — sobrevive a un crash del navegador.

Pipeline (research §7):
   PipeWire screen capture
     → videoconvert → x264enc (o hw enc) low-latency
     → rtph264pay → webrtcbin (DTLS-SRTP)
     → señalización fuera (mTLS al CP)

Esta clase es la fachada Python; el pipeline real GStreamer se carga
lazy con `gi`. En CI/base usamos `FakeScreenStreamingBackend`.

Eventos publicados:
  - StreamStarted (session_id, sdp_local)
  - StreamRunning (session_id)
  - StreamEnded (session_id, end_reason)
  - StreamBindingViolated (session_id, observed_dtls_fp)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import UUID

from hermes.agents_os.application.remote_control_orchestrator import (
    RemoteControlSession,
    RemoteControlState,
)

logger = logging.getLogger(__name__)


class StreamEvent(StrEnum):
    STARTED = "started"
    RUNNING = "running"
    ENDED = "ended"
    BINDING_VIOLATED = "binding_violated"
    PIPELINE_ERROR = "pipeline_error"


@dataclass(slots=True)
class StreamPublication:
    """Publicación SDP del lado nodo (offer)."""

    session_id: UUID
    sdp_local: str
    dtls_fingerprint_local: str
    started_at: datetime
    state: str = "starting"


@runtime_checkable
class ScreenStreamingBackend(Protocol):
    """Interfaz al pipeline GStreamer real."""

    def start_pipeline(
        self,
        *,
        session: RemoteControlSession,
        on_event: Callable[[StreamEvent, dict[str, Any]], None],
    ) -> StreamPublication: ...

    def stop_pipeline(self, *, session_id: UUID) -> None: ...

    def handle_remote_sdp_answer(
        self, *, session_id: UUID, sdp_answer: str
    ) -> None: ...


@dataclass(slots=True)
class FakeScreenStreamingBackend:
    """Backend determinístico para tests/CI sin GStreamer."""

    pipelines: dict[UUID, StreamPublication] = field(default_factory=dict)
    events: list[tuple[UUID, StreamEvent, dict[str, Any]]] = field(
        default_factory=list
    )

    def start_pipeline(
        self,
        *,
        session: RemoteControlSession,
        on_event: Callable[[StreamEvent, dict[str, Any]], None],
    ) -> StreamPublication:
        pub = StreamPublication(
            session_id=session.session_id,
            sdp_local="v=0\no=- 0 0 IN IP4 127.0.0.1\n...",
            dtls_fingerprint_local=session.dtls_fingerprint,
            started_at=datetime.now(tz=UTC),
            state="running",
        )
        self.pipelines[session.session_id] = pub
        ev = (session.session_id, StreamEvent.STARTED, {"sdp": pub.sdp_local})
        self.events.append(ev)
        on_event(StreamEvent.STARTED, {"sdp": pub.sdp_local})
        return pub

    def stop_pipeline(self, *, session_id: UUID) -> None:
        pub = self.pipelines.pop(session_id, None)
        if pub is not None:
            pub.state = "ended"
            self.events.append((session_id, StreamEvent.ENDED, {}))

    def handle_remote_sdp_answer(
        self, *, session_id: UUID, sdp_answer: str
    ) -> None:
        if session_id not in self.pipelines:
            raise RuntimeError(f"unknown session {session_id}")
        self.events.append(
            (session_id, StreamEvent.RUNNING, {"answer": sdp_answer})
        )


class ScreenStreamingAdapter:
    """Fachada — wraps backend con validación de sesión + auditoría hooks."""

    def __init__(
        self,
        *,
        backend: ScreenStreamingBackend,
        on_event: Callable[[UUID, StreamEvent, dict[str, Any]], None]
        | None = None,
    ) -> None:
        self._backend = backend
        self._on_event = on_event or (lambda *_: None)
        self._active: dict[UUID, StreamPublication] = {}

    def start(
        self, *, session: RemoteControlSession
    ) -> StreamPublication:
        if session.state not in (
            RemoteControlState.ACCEPTED,
            RemoteControlState.ACTIVE,
        ):
            raise RuntimeError(
                f"start requiere ACCEPTED/ACTIVE, está {session.state}"
            )
        pub = self._backend.start_pipeline(
            session=session,
            on_event=lambda evt, payload: self._on_event(
                session.session_id, evt, payload
            ),
        )
        self._active[session.session_id] = pub
        return pub

    def stop(self, *, session_id: UUID) -> None:
        self._backend.stop_pipeline(session_id=session_id)
        self._active.pop(session_id, None)
        self._on_event(session_id, StreamEvent.ENDED, {})

    def handle_answer(
        self, *, session_id: UUID, sdp_answer: str
    ) -> None:
        self._backend.handle_remote_sdp_answer(
            session_id=session_id, sdp_answer=sdp_answer
        )

    def active_sessions(self) -> tuple[UUID, ...]:
        return tuple(self._active.keys())


def _lazy_gst_backend():  # pragma: no cover — solo en nodo real
    """Construye el backend GStreamer real cuando el SO tiene gi+gst."""
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    from gi.repository import Gst, GstWebRTC  # type: ignore  # noqa

    raise NotImplementedError(
        "real GStreamer backend wires up in stable-channel build"
    )
