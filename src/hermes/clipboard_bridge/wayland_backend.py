"""Wayland clipboard backend — thin wrapper around wl-copy / wl-paste.

Injected as a dependency so tests can substitute a fake without touching a
real Wayland session.  Production code uses WaylandClipboardBackend; tests
use FakeClipboardBackend from the testing module below.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Protocol

logger = logging.getLogger("hermes-clipboard-bridge")

# Max time to wait for wl-copy / wl-paste (seconds).
_WL_TIMEOUT = 5


class ClipboardBackend(Protocol):
    """Port: synchronous clipboard read/write primitives."""

    def write(self, text: str) -> None:
        """Write *text* to the clipboard.  Raises ClipboardError on failure."""
        ...

    def read(self) -> str:
        """Return clipboard text.  Returns empty string when clipboard is empty
        or the backend is unavailable."""
        ...


class ClipboardError(Exception):
    """Raised when a clipboard operation fails at the backend level."""


class WaylandClipboardBackend:
    """Real backend: delegates to wl-copy / wl-paste via subprocess.

    Security notes:
    - Text is passed on STDIN (wl-copy), never as an argv argument — no
      shell / argument injection vector.
    - shell=False is explicit.
    - Content is never emitted to logs; only byte lengths are logged.
    """

    def write(self, text: str) -> None:
        encoded = text.encode("utf-8")
        logger.debug(
            "hermes.clipboard_bridge.write",
            extra={"byte_len": len(encoded)},
        )
        try:
            # wl-copy FORKea un demonio que sirve la selección Wayland y queda
            # vivo; si capturáramos stdout/stderr como PIPES, ese demonio
            # heredaría los write-ends → run() bloquea esperando EOF → falso
            # timeout (la copia YA tuvo efecto). Por eso → DEVNULL (sin pipes):
            # run() solo espera al proceso padre, que forkea y sale al instante.
            # start_new_session desliga al demonio del ciclo de vida del bridge.
            result = subprocess.run(
                ["wl-copy"],
                input=encoded,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_WL_TIMEOUT,
                shell=False,  # explicit: no shell, no injection
                check=False,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ClipboardError("wl-copy not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClipboardError("wl-copy timed out") from exc

        if result.returncode != 0:
            raise ClipboardError(f"wl-copy exited {result.returncode}")

    def read(self) -> str:
        logger.debug("hermes.clipboard_bridge.read")
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                timeout=_WL_TIMEOUT,
                shell=False,
                check=False,
            )
        except FileNotFoundError:
            logger.warning("hermes.clipboard_bridge.wl_paste_not_found")
            return ""
        except subprocess.TimeoutExpired:
            logger.warning("hermes.clipboard_bridge.wl_paste_timeout")
            return ""

        if result.returncode != 0:
            # wl-paste exits 1 when clipboard is empty — treat as empty.
            return ""

        text = result.stdout.decode("utf-8", errors="replace")
        logger.debug(
            "hermes.clipboard_bridge.read_ok",
            extra={"byte_len": len(result.stdout)},
        )
        return text
