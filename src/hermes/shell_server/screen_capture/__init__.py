"""SO-level screen capture: mutter ScreenCast → PipeWire → GStreamer appsink.

Captura el compositor entero (navegador Y apps de escritorio, todo lo que
mutter compone) en una sola tubería. Reutilizado por:
  - F7.1 live view (frames → Gdk.MemoryTexture → Gtk.Picture)
  - F6.1 training (frames como screenshots de los pasos capturados)

Validado en VM real: mutter Virtual-1 → node_id → pipewiresrc → appsink RGBA.

Piezas:
    domain.py            — Frame, CaptureTarget, CaptureError (sin framework).
    mutter_source.py     — MutterScreenCastSource (D-Bus, da el PipeWire node).
    gst_capture.py       — GstFrameCapture (pipewiresrc ! appsink → callbacks).
    service.py           — ScreenCaptureService (orquesta source + gst).
    fake.py              — FakeScreenCaptureBackend (tests sin compositor).
"""

from .domain import (
    CaptureError,
    CaptureTarget,
    CaptureTargetKind,
    Frame,
)

__all__ = [
    "CaptureError",
    "CaptureTarget",
    "CaptureTargetKind",
    "Frame",
]
