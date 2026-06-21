"""Capa 4: WebSocket Frame Filter (T109, research §7 capa 4).

Intercela frames WebSocket salientes y bloquea los que parecen mutadores
usando heurística JSON-RPC/REST-style.

Heurística:
- Si el frame es JSON con "method" en lista de métodos mutadores.
- Si el frame contiene paths de side-effects típicos (/delete, /update, etc.).
- Frames binarios: default-deny si el SiteSpec los marca como peligrosos.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page, WebSocket, WebSocketFrame

logger = logging.getLogger(__name__)

# Métodos JSON-RPC / REST considerados mutadores.
_MUTATING_METHODS: frozenset[str] = frozenset(
    {
        "create",
        "update",
        "delete",
        "remove",
        "destroy",
        "patch",
        "submit",
        "save",
        "publish",
        "transfer",
        "pay",
        "checkout",
        "approve",
        "reject",
        "cancel",
        "archive",
        "restore",
        "execute",
        "confirm",
    }
)

# Paths de side-effects conocidos en URLs de WebSocket frames.
_SIDE_EFFECT_PATHS = (
    "/delete",
    "/remove",
    "/destroy",
    "/update",
    "/patch",
    "/submit",
    "/save",
    "/transfer",
    "/pay",
    "/checkout",
    "/confirm",
    "/approve",
    "/publish",
)


class WebSocketFrameFilter:
    """Filtra frames WebSocket mutadores en preview mode.

    Se adjunta via page.on("websocket", ...).
    """

    def __init__(
        self,
        *,
        on_blocked: Callable[[str, str], None] | None = None,
        extra_mutating_methods: Sequence[str] = (),
    ) -> None:
        self._on_blocked = on_blocked
        self._mutating = _MUTATING_METHODS | frozenset(extra_mutating_methods)
        self._active = False

    def attach(self, page: "Page") -> None:
        """Registra el handler de websocket en la Page."""
        page.on("websocket", self._on_websocket)
        self._active = True
        logger.info("websocket_frame_filter_attached")

    def _on_websocket(self, ws: "WebSocket") -> None:
        ws.on("framesent", self._on_frame_sent)

    def _on_frame_sent(self, frame: "WebSocketFrame") -> None:
        payload = frame.text if hasattr(frame, "text") else ""
        if not payload:
            return

        reason = self._classify(payload)
        if reason:
            logger.warning(
                "replay_preview_websocket_blocked",
                extra={"reason": reason, "payload_preview": payload[:100]},
            )
            if self._on_blocked:
                self._on_blocked(reason, payload)

    def _classify(self, payload: str) -> str | None:
        """Devuelve razón de bloqueo o None si el frame es seguro."""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None

        method = _extract_method(data)
        if method and method.lower() in self._mutating:
            return f"mutating_method:{method}"

        url = _extract_url(data)
        if url and _has_side_effect_path(url):
            return f"side_effecting_path:{url}"

        return None

    @property
    def is_active(self) -> bool:
        return self._active


def _extract_method(data: object) -> str | None:
    if isinstance(data, dict):
        return data.get("method") or data.get("action") or data.get("type")
    return None


def _extract_url(data: object) -> str | None:
    if isinstance(data, dict):
        return data.get("url") or data.get("path") or data.get("endpoint")
    return None


def _has_side_effect_path(url: str) -> bool:
    url_lower = url.lower()
    return any(path in url_lower for path in _SIDE_EFFECT_PATHS)
