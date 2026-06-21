"""Ejecutores de las OS-native skills sobre la capa nativa del SO.

Estos handlers corren EN LA SESIÓN gráfica (proceso shell GTK4 o runtime en
sesión) porque mutter ScreenCast vive en el bus de sesión. Hacen imports
perezosos de la capa nativa para no requerir gi/Gst en CI/headless.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_ARTIFACT_DIR = Path("/var/lib/hermes/os-skills")


def _artifact_dir() -> Path:
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    return _ARTIFACT_DIR


def execute_screenshot(args: dict, *, monitor_connector: str = "") -> dict:
    """Captura 1 frame del compositor y lo guarda como PNG. Devuelve la ruta.

    monitor_connector="" → MutterScreenCastSource resuelve el monitor primario.
    """
    from hermes.shell_server.screen_capture.domain import CaptureTarget
    from hermes.shell_server.screen_capture.gst_capture import GstFrameCapture
    from hermes.shell_server.screen_capture.mutter_source import (
        MutterScreenCastSource,
    )
    from hermes.shell_server.training.png_writer import encode_rgba_png

    source = MutterScreenCastSource()
    connector = monitor_connector or source.primary_connector()
    node = source.start(CaptureTarget.monitor(connector))
    # cap.start() is inside the try so that source.stop() runs in the finally
    # even if the GStreamer pipeline fails to reach PLAYING (finding #11).
    cap = GstFrameCapture(node, fps=5)
    try:
        cap.start()
        # Esperar un frame no-blanco (hasta ~3 s).
        import time  # noqa: PLC0415

        frame = None
        for _ in range(30):
            f = cap.latest_frame()
            if f is not None and not f.is_blank():
                frame = f
                break
            time.sleep(0.1)
        if frame is None:
            return {"ok": False, "error": "no se obtuvo frame de la pantalla"}
        # Unique filename per capture to avoid collisions (finding #22).
        out = _artifact_dir() / f"screenshot_{uuid.uuid4().hex}.png"
        out.write_bytes(encode_rgba_png(frame.width, frame.height, frame.data))
        return {
            "ok": True,
            "path": str(out),
            "width": frame.width,
            "height": frame.height,
        }
    finally:
        cap.stop()
        source.stop()


def execute_screen_record(args: dict, *, monitor_connector: str = "") -> dict:
    """Graba pantalla+audio durante duration_seconds. Devuelve la ruta .webm."""
    import time  # noqa: PLC0415

    duration = int(args.get("duration_seconds", 5))
    # Default deny for microphone — must be explicitly requested (finding #27).
    with_audio = bool(args.get("with_audio", False))

    from hermes.shell_server.screen_capture.domain import CaptureTarget
    from hermes.shell_server.screen_capture.mutter_source import (
        MutterScreenCastSource,
    )
    from hermes.shell_server.screen_capture.recorder import GstScreenRecorder

    source = MutterScreenCastSource()
    connector = monitor_connector or source.primary_connector()
    node = source.start(CaptureTarget.monitor(connector))
    out = _artifact_dir() / f"recording_{node}_{duration}s.webm"
    recorder = GstScreenRecorder(
        node_id=node, out_path=out, fps=15, with_audio=with_audio
    )
    # recorder.start() is inside the try so that source.stop() runs in the
    # finally even if the pipeline or mic check raises (finding #11).
    try:
        recorder.start()
        time.sleep(duration)
    finally:
        path = recorder.stop()
        source.stop()
    size = path.stat().st_size if path.exists() else 0
    return {
        "ok": size > 0,
        "path": str(path),
        "bytes": size,
        "has_audio": recorder.has_audio,
        "duration_seconds": duration,
    }


def execute_mouse_move(args: dict) -> dict:
    """Move pointer to (x, y) via SessionBridgeClient.

    This function is sync and runs inside asyncio.to_thread inside the
    OsNativeDispatcher._dispatch_input helper.  It creates a fresh asyncio
    event loop for the blocking-to-async bridge call.
    """
    import asyncio  # noqa: PLC0415

    from hermes.capabilities.infrastructure.session_bridge_client import (  # noqa: PLC0415
        SessionBridgeClient,
        SessionBridgeUnavailable,
        SessionBridgeError,
    )

    x = float(args.get("x", 0))
    y = float(args.get("y", 0))
    client = SessionBridgeClient()
    try:
        return asyncio.run(client.pointer_motion(x, y))
    except SessionBridgeUnavailable as exc:
        return {"ok": False, "error": f"bridge_unavailable: {exc}"}
    except SessionBridgeError as exc:
        return {"ok": False, "error": str(exc)}


def execute_mouse_click(args: dict) -> dict:
    """Press + release a mouse button via SessionBridgeClient."""
    import asyncio  # noqa: PLC0415

    from hermes.capabilities.infrastructure.session_bridge_client import (  # noqa: PLC0415
        SessionBridgeClient,
        SessionBridgeUnavailable,
        SessionBridgeError,
    )

    btn = int(args.get("btn", 0))
    client = SessionBridgeClient()
    try:
        asyncio.run(client.pointer_button(btn, True))
        asyncio.run(client.pointer_button(btn, False))
        return {"ok": True}
    except SessionBridgeUnavailable as exc:
        return {"ok": False, "error": f"bridge_unavailable: {exc}"}
    except SessionBridgeError as exc:
        return {"ok": False, "error": str(exc)}


def execute_type_text(args: dict) -> dict:
    """Type a text string via SessionBridgeClient."""
    import asyncio  # noqa: PLC0415

    from hermes.capabilities.infrastructure.session_bridge_client import (  # noqa: PLC0415
        SessionBridgeClient,
        SessionBridgeUnavailable,
        SessionBridgeError,
    )

    text = str(args.get("text", ""))
    client = SessionBridgeClient()
    try:
        return asyncio.run(client.type_text(text))
    except SessionBridgeUnavailable as exc:
        return {"ok": False, "error": f"bridge_unavailable: {exc}"}
    except SessionBridgeError as exc:
        return {"ok": False, "error": str(exc)}


# Screenshot via bridge (replaces direct mutter import in daemon — bridge owns session bus)
def execute_screenshot_via_bridge(args: dict) -> dict:
    """Capture screenshot via SessionBridgeClient.

    The daemon cannot import mutter (PrivateDevices, no session bus). The
    bridge runs in the session and does the actual capture.  This executor
    replaces execute_screenshot in daemon context.
    """
    import asyncio  # noqa: PLC0415

    from hermes.capabilities.infrastructure.session_bridge_client import (  # noqa: PLC0415
        SessionBridgeClient,
        SessionBridgeUnavailable,
        SessionBridgeError,
    )

    client = SessionBridgeClient()
    try:
        return asyncio.run(client.screenshot())
    except SessionBridgeUnavailable as exc:
        logger.warning("hermes.executors.screenshot_bridge_unavailable: %s", exc)
        # Fallback: attempt direct capture (works when running in session process)
        return execute_screenshot(args)
    except SessionBridgeError as exc:
        return {"ok": False, "error": str(exc)}


EXECUTORS = {
    "screenshot": execute_screenshot_via_bridge,
    "screen_record": execute_screen_record,
    "mouse_move": execute_mouse_move,
    "mouse_click": execute_mouse_click,
    "type_text": execute_type_text,
}
