"""DesktopAppSurfaceAdapter — opera apps GNOME via AT-SPI + libwnck.

Spec 003 FR-038 — SurfaceKind.DESKTOP_APP. El agente puede pulsar el
botón "Imprimir" de gedit, "Guardar" de LibreOffice, copiar texto del
clipboard, etc. Sin DOM — todo vía accessibility tree.

Captura:
  - Snapshot del árbol AT-SPI con foco actual + path Accessible.
  - Acción ejecutada (click, type, focus, copy, paste).

Reproducción:
  - Localiza el Accessible por el path firmado.
  - Si no se encuentra, busca por nombre de role + nombre visible
    + parent application name (re-discovery suave; el LLM puede
    re-firmar el path nuevo si el layout cambió).

Esta clase es STUB ejecutable en CI base (sin AT-SPI real) — todas
las invocaciones reales viven detrás del marcador `requires_at_spi`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger(__name__)


class DesktopActionKind(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    TYPE = "type"
    FOCUS = "focus"
    COPY = "copy"
    PASTE = "paste"
    SELECT_ALL = "select_all"
    KEYBOARD_SHORTCUT = "keyboard_shortcut"


@dataclass(frozen=True, slots=True)
class AtSpiPath:
    """Path canónico al Accessible (sin estado de runtime).

    Ej: app=org.gnome.Nautilus / window=Files / role=push_button /
        name=Open / index=2

    Sirve como selector firmable.
    """

    application: str
    window: str
    role: str
    name: str
    index: int = 0

    def to_canonical_string(self) -> str:
        return (
            f"app={self.application}|win={self.window}|role={self.role}"
            f"|name={self.name}|idx={self.index}"
        )


@dataclass(frozen=True, slots=True)
class DesktopCapturedAction:
    """Acción capturada — alineada con SurfaceAdapterPort."""

    surface_kind: SurfaceKind
    action_kind: DesktopActionKind
    accessible_path: AtSpiPath
    payload: dict[str, Any]
    captured_at: datetime
    pre_focus_app: str | None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class DesktopReplayOutcome:
    """Resultado de reproducir la acción."""

    success: bool
    final_accessible_path: AtSpiPath | None
    fallback_used: bool
    error: str | None = None
    duration_ms: int = 0


class DesktopAppSurfaceAdapter:
    """Adapter SurfaceAdapterPort para DesktopApps.

    Args:
        atspi_client: cliente AT-SPI inyectable (en CI = FakeAtSpiClient).
        allow_apps: lista blanca de aplicaciones permitidas. Si no
            aparece la app, capture/replay rechaza. Patrón allowlist
            consistente con browser host_allowlist.
    """

    surface_kind = SurfaceKind.DESKTOP_APP

    def __init__(
        self,
        *,
        atspi_client: "AtSpiClient",
        allow_apps: frozenset[str],
    ) -> None:
        self._client = atspi_client
        self._allow_apps = allow_apps

    def capture(
        self,
        *,
        action_kind: DesktopActionKind,
        accessible_path: AtSpiPath,
        payload: dict[str, Any],
    ) -> DesktopCapturedAction:
        self._assert_app_allowed(accessible_path.application)
        pre_focus = self._client.focused_application_name()
        return DesktopCapturedAction(
            surface_kind=self.surface_kind,
            action_kind=action_kind,
            accessible_path=accessible_path,
            payload=dict(payload),
            captured_at=datetime.now(tz=UTC),
            pre_focus_app=pre_focus,
        )

    def replay(self, action: DesktopCapturedAction) -> DesktopReplayOutcome:
        self._assert_app_allowed(action.accessible_path.application)
        start = time.monotonic()

        located = self._client.find_by_path(action.accessible_path)
        fallback_used = False
        if located is None:
            located = self._client.find_by_fuzzy(action.accessible_path)
            fallback_used = located is not None
        if located is None:
            return DesktopReplayOutcome(
                success=False,
                final_accessible_path=None,
                fallback_used=False,
                error="accessible_not_found",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            self._dispatch(action.action_kind, located, action.payload)
        except Exception as exc:  # noqa: BLE001
            return DesktopReplayOutcome(
                success=False,
                final_accessible_path=located,
                fallback_used=fallback_used,
                error=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        return DesktopReplayOutcome(
            success=True,
            final_accessible_path=located,
            fallback_used=fallback_used,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _assert_app_allowed(self, application: str) -> None:
        if application not in self._allow_apps:
            raise PermissionError(
                f"desktop app {application!r} no está en allowlist"
            )

    def _dispatch(
        self,
        action_kind: DesktopActionKind,
        located: AtSpiPath,
        payload: dict[str, Any],
    ) -> None:
        client = self._client
        if action_kind in (DesktopActionKind.CLICK, DesktopActionKind.DOUBLE_CLICK):
            client.click(located, double=action_kind == DesktopActionKind.DOUBLE_CLICK)
        elif action_kind == DesktopActionKind.TYPE:
            client.type_text(located, payload.get("text", ""))
        elif action_kind == DesktopActionKind.FOCUS:
            client.focus(located)
        elif action_kind == DesktopActionKind.COPY:
            client.send_shortcut(located, "ctrl+c")
        elif action_kind == DesktopActionKind.PASTE:
            client.send_shortcut(located, "ctrl+v")
        elif action_kind == DesktopActionKind.SELECT_ALL:
            client.send_shortcut(located, "ctrl+a")
        elif action_kind == DesktopActionKind.KEYBOARD_SHORTCUT:
            shortcut = payload.get("shortcut")
            if not shortcut:
                raise ValueError("payload.shortcut requerido")
            client.send_shortcut(located, shortcut)
        else:
            raise ValueError(f"action_kind no soportada: {action_kind}")


# ---------------------------------------------------------------------------
# Cliente AT-SPI (interfaz mínima + Fake para CI).
# ---------------------------------------------------------------------------


class AtSpiClient:
    """Interfaz mínima — definida como clase base para mypy. La impl
    real `LibAtSpiClient` viene en infra siguiente capa y usa pyatspi.
    """

    def focused_application_name(self) -> str | None:
        raise NotImplementedError

    def find_by_path(self, path: AtSpiPath) -> AtSpiPath | None:
        raise NotImplementedError

    def find_by_fuzzy(self, path: AtSpiPath) -> AtSpiPath | None:
        raise NotImplementedError

    def click(self, path: AtSpiPath, *, double: bool = False) -> None:
        raise NotImplementedError

    def type_text(self, path: AtSpiPath, text: str) -> None:
        raise NotImplementedError

    def focus(self, path: AtSpiPath) -> None:
        raise NotImplementedError

    def send_shortcut(self, path: AtSpiPath, shortcut: str) -> None:
        raise NotImplementedError


@dataclass(slots=True)
class FakeAtSpiClient(AtSpiClient):
    """Cliente fake para CI — registra invocaciones."""

    available_paths: set[str] = field(default_factory=set)
    focused_app: str | None = None
    invocations: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def focused_application_name(self) -> str | None:
        return self.focused_app

    def find_by_path(self, path: AtSpiPath) -> AtSpiPath | None:
        if path.to_canonical_string() in self.available_paths:
            return path
        return None

    def find_by_fuzzy(self, path: AtSpiPath) -> AtSpiPath | None:
        # Coincide por role+name si la app ya está y el resto difiere.
        for candidate in self.available_paths:
            if (
                f"app={path.application}" in candidate
                and f"role={path.role}" in candidate
                and f"name={path.name}" in candidate
            ):
                # Devolver el path original con index=99 para marcar fallback.
                return AtSpiPath(
                    application=path.application,
                    window=path.window,
                    role=path.role,
                    name=path.name,
                    index=99,
                )
        return None

    def click(self, path: AtSpiPath, *, double: bool = False) -> None:
        self.invocations.append(
            ("click", {"path": path.to_canonical_string(), "double": double})
        )

    def type_text(self, path: AtSpiPath, text: str) -> None:
        self.invocations.append(
            ("type", {"path": path.to_canonical_string(), "text": text})
        )

    def focus(self, path: AtSpiPath) -> None:
        self.invocations.append(
            ("focus", {"path": path.to_canonical_string()})
        )

    def send_shortcut(self, path: AtSpiPath, shortcut: str) -> None:
        self.invocations.append(
            ("shortcut", {"path": path.to_canonical_string(), "key": shortcut})
        )
