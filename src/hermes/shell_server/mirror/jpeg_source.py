"""JpegFrameSource — pipewiresrc(node) ! jpegenc ! appsink → últimos bytes JPEG.

Igual que GstFrameCapture pero comprime a JPEG en el pipeline (eficiente para
transporte por WebSocket). Guarda el último frame JPEG; el server lo emite al
ritmo que quiera. SIN videorate (estanca el preroll del source de mutter).
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402,F401

from ..screen_capture.domain import CaptureError

logger = logging.getLogger(__name__)

_PIPELINE = (
    "pipewiresrc path={node} ! "
    "videoconvert ! videoscale ! video/x-raw,format=I420 ! "
    "jpegenc quality={quality} ! "
    "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
)


def _ensure_gst() -> None:
    if not Gst.is_initialized():
        Gst.init(None)


class JpegFrameSource:
    """Pipeline GStreamer que guarda el último frame como JPEG."""

    def __init__(self, node_id: int, *, quality: int = 60) -> None:
        _ensure_gst()
        self._node_id = node_id
        self._quality = quality
        self._pipeline: Gst.Pipeline | None = None
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._size: tuple[int, int] = (0, 0)

    def start(self) -> None:
        desc = _PIPELINE.format(node=self._node_id, quality=self._quality)
        logger.info("mirror gst pipeline: %s", desc)
        self._pipeline = Gst.parse_launch(desc)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise CaptureError("mirror pipeline no arrancó")

    def _on_sample(self, sink: GstApp.AppSink) -> int:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        st = sample.get_caps().get_structure(0)
        w, h = st.get_value("width"), st.get_value("height")
        ok, minfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(minfo.data)
        finally:
            buf.unmap(minfo)
        with self._lock:
            self._latest = data
            self._size = (w, h)
        return Gst.FlowReturn.OK

    def latest(self) -> tuple[bytes | None, tuple[int, int]]:
        with self._lock:
            return self._latest, self._size

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
