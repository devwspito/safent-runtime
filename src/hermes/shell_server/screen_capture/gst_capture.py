"""GstFrameCapture — pipewiresrc(node) ! videoconvert ! appsink(RGBA).

Lee frames RGBA del PipeWire node que entrega mutter y los entrega por
callback como `Frame` del dominio. Sin acoplar a la UI: el consumidor
decide qué hacer (render en Gtk.Picture, escribir PNG de un step, etc.).
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402,F401

from .domain import CaptureError, Frame, FrameCallback

logger = logging.getLogger(__name__)

# SIN `videorate ! caps(framerate)`: videorate forzando un framerate fijo
# estanca el preroll sobre el source de mutter ScreenCast (ritmo variable) y
# el appsink no entrega frames (validado: "no frame" en el SO). Entregamos al
# ritmo del compositor; el consumidor (live view / training) se queda con el
# último frame que necesita. drop=true + max-buffers acotan memoria.
_PIPELINE = (
    "pipewiresrc path={node} ! "
    "videoconvert ! video/x-raw,format=RGBA ! "
    "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
)


def _ensure_gst() -> None:
    if not Gst.is_initialized():
        Gst.init(None)


class GstFrameCapture:
    """Pipeline GStreamer que emite Frames RGBA por callback."""

    def __init__(self, node_id: int, *, fps: int = 15) -> None:
        _ensure_gst()
        self._node_id = node_id
        self._fps = fps
        self._pipeline: Gst.Pipeline | None = None
        self._callback: FrameCallback | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._latest: Frame | None = None

    # ------------------------------------------------------------------
    def start(self, callback: FrameCallback | None = None) -> None:
        self._callback = callback
        desc = _PIPELINE.format(node=self._node_id, fps=self._fps)
        logger.info("gst pipeline: %s", desc)
        self._pipeline = Gst.parse_launch(desc)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        rv = self._pipeline.set_state(Gst.State.PLAYING)
        if rv == Gst.StateChangeReturn.FAILURE:
            raise CaptureError("gst pipeline no arrancó (PLAYING failed)")

    def _on_sample(self, sink: GstApp.AppSink) -> int:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w = caps.get_value("width")
        h = caps.get_value("height")
        ok, minfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            self._seq += 1
            frame = Frame(
                width=w,
                height=h,
                data=bytes(minfo.data),
                sequence=self._seq,
            )
        finally:
            buf.unmap(minfo)
        with self._lock:
            self._latest = frame
        if self._callback is not None:
            try:
                self._callback(frame)
            except Exception:  # noqa: BLE001 - un callback malo no mata captura
                logger.exception("frame callback raised")
        return Gst.FlowReturn.OK

    def latest_frame(self) -> Frame | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
