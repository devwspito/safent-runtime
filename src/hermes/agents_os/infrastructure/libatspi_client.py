"""LibAtSpiClient — adapter AT-SPI real (FR-038).

Cumple `AtSpiClient`. Import lazy de `pyatspi` para tests/CI base.
En personal-desktop el paquete `at-spi2-core` viene de Fedora bootc.

v2 additions:
  list_windows()         — enumerate top-level windows via Registry.getDesktop(0)
  get_window_tree()      — accessible-element tree (index-stable, capped at 200)
  click_element()        — activate by index; prefers doAction, fallback → bounds
  set_text_element()     — queryEditableText().setTextContents(); fallback → None
  element_bounds()       — queryComponent().getExtents() for coord-fallback callers

Requires:
  * python3-pyatspi + at-spi2-core in the image (RPM).
  * The caller must run inside the GRAPHICAL SESSION (hermes-user) that owns the
    a11y bus.  The hardened daemon (hermes) does NOT have access to the session
    D-Bus; all AT-SPI work MUST be invoked from SessionInputBridge.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from hermes.agents_os.infrastructure.desktop_app_surface_adapter import (
    AtSpiClient,
    AtSpiPath,
)

logger = logging.getLogger(__name__)

# Roles considered "actionable/relevant" for the window tree.
# Kept as a frozenset for O(1) membership test.
_RELEVANT_ROLES: frozenset[str] = frozenset({
    "push button",
    "button",
    "toggle button",
    "check box",
    "radio button",
    "text",
    "entry",
    "password text",
    "editable text",
    "combo box",
    "list item",
    "menu item",
    "menu",
    "link",
    "label",
    "tree item",
    "table cell",
    "spin button",
    "slider",
    "scroll bar",
    "page tab",
    "separator",
    "tool bar",
    "status bar",
    "tool tip",
    "dialog",
    "frame",
    "window",
    "panel",
    "filler",
    "section",
})

# AT-SPI STATE_ACTIVE constant — resolved at runtime from pyatspi.
_MAX_TREE_ELEMENTS: int = 200
_MAX_TREE_DEPTH: int = 30
# TTL for the element cache (seconds).  Re-enumerated after this window is
# touched or the TTL expires.  5 seconds is short enough to track UI changes
# between agent steps but long enough to avoid redundant walks.
_CACHE_TTL_S: float = 5.0


class LibAtSpiClient(AtSpiClient):  # pragma: no cover — depende del SO
    """Adapter real sobre pyatspi.

    NO se importa en CI base — solo en personal-desktop image.

    Thread-safety: pyatspi is NOT thread-safe.  All public methods MUST be
    called from a single thread (asyncio.to_thread creates a new OS thread per
    call but each call is serialised by the GIL and pyatspi's own GLib main
    loop dependency).  For production use: always call via asyncio.to_thread
    from the session bridge; never share an instance across coroutines.
    """

    def __init__(self) -> None:
        try:
            import pyatspi  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "pyatspi no disponible — instala at-spi2-core + python3-pyatspi"
            ) from exc
        self._pyatspi = pyatspi
        # {(window_id, rebuild_key): (timestamp, [accessible_objects])}
        # window_id is the int index into the desktop child list (stable per session).
        self._element_cache: dict[int, tuple[float, list[Any]]] = {}

    def focused_application_name(self) -> str | None:
        desktop = self._pyatspi.Registry.getDesktop(0)
        for child in desktop:
            if child is None:
                continue
            for window in child:
                if window is None:
                    continue
                state_set = window.getState()
                if self._pyatspi.STATE_ACTIVE in state_set.getStates():
                    return child.name
        return None

    def find_by_path(self, path: AtSpiPath) -> AtSpiPath | None:
        desktop = self._pyatspi.Registry.getDesktop(0)
        for app in desktop:
            if app is None or app.name != path.application:
                continue
            for window in app:
                if window is None or window.name != path.window:
                    continue
                idx = 0
                for descendant in self._walk(window):
                    if (
                        descendant.getRoleName().replace(" ", "_") == path.role
                        and descendant.name == path.name
                    ):
                        if idx == path.index:
                            return path
                        idx += 1
        return None

    def find_by_fuzzy(self, path: AtSpiPath) -> AtSpiPath | None:
        desktop = self._pyatspi.Registry.getDesktop(0)
        for app in desktop:
            if app is None or app.name != path.application:
                continue
            for window in app:
                if window is None:
                    continue
                for descendant in self._walk(window):
                    if (
                        descendant.getRoleName().replace(" ", "_") == path.role
                        and descendant.name == path.name
                    ):
                        return AtSpiPath(
                            application=path.application,
                            window=window.name,
                            role=path.role,
                            name=path.name,
                            index=0,
                        )
        return None

    def click(self, path: AtSpiPath, *, double: bool = False) -> None:
        accessible = self._resolve(path)
        if accessible is None:
            raise RuntimeError(f"accessible not found for {path}")
        action_iface = accessible.queryAction()
        action_name = "click" if not double else "press"
        for i in range(action_iface.nActions):
            if action_iface.getName(i).lower() == action_name:
                action_iface.doAction(i)
                return
        raise RuntimeError(f"no '{action_name}' action available on {path}")

    def type_text(self, path: AtSpiPath, text: str) -> None:
        accessible = self._resolve(path)
        if accessible is None:
            raise RuntimeError(f"accessible not found for {path}")
        text_iface = accessible.queryEditableText()
        text_iface.insertText(0, text, len(text))

    def focus(self, path: AtSpiPath) -> None:
        accessible = self._resolve(path)
        if accessible is None:
            raise RuntimeError(f"accessible not found for {path}")
        component = accessible.queryComponent()
        component.grabFocus()

    def send_shortcut(self, path: AtSpiPath, shortcut: str) -> None:
        # Translates 'ctrl+c' into AT-SPI key events.
        self._pyatspi.Registry.generateKeyboardEvent(
            self._shortcut_to_keycode(shortcut),
            None,
            self._pyatspi.KEY_PRESSRELEASE,
        )

    def _resolve(self, path: AtSpiPath) -> Any | None:
        desktop = self._pyatspi.Registry.getDesktop(0)
        for app in desktop:
            if app is None or app.name != path.application:
                continue
            for window in app:
                if window is None or window.name != path.window:
                    continue
                idx = 0
                for descendant in self._walk(window):
                    if (
                        descendant.getRoleName().replace(" ", "_") == path.role
                        and descendant.name == path.name
                    ):
                        if idx == path.index:
                            return descendant
                        idx += 1
        return None

    def _walk(self, root):
        yield root
        for child in root:
            if child is None:
                continue
            yield from self._walk(child)

    @staticmethod
    def _shortcut_to_keycode(shortcut: str) -> int:
        mapping = {
            "ctrl+c": 0x60,
            "ctrl+v": 0x61,
            "ctrl+a": 0x62,
        }
        if shortcut not in mapping:
            raise ValueError(f"unknown shortcut {shortcut}")
        return mapping[shortcut]

    # ------------------------------------------------------------------
    # v2 — window enumeration and element-level interaction
    # ------------------------------------------------------------------

    def list_windows(self) -> list[dict[str, Any]]:
        """Return all top-level windows visible in the a11y tree.

        Each entry: {app_name, pid, window_id, title, is_active, bounds}.
        Sorted with the active (frontmost) window first.
        Returns [] if the desktop cannot be reached (no a11y bus).
        """
        try:
            desktop = self._pyatspi.Registry.getDesktop(0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("libatspi.list_windows.desktop_error: %s", exc)
            return []

        windows: list[dict[str, Any]] = []
        window_id = 0
        for app in desktop:
            if app is None:
                continue
            pid = self._get_pid(app)
            app_name = app.name or ""
            for child in app:
                if child is None:
                    continue
                role_name = child.getRoleName()
                if role_name not in ("frame", "window", "dialog"):
                    continue
                is_active = self._has_state_active(child)
                bounds = self._get_bounds(child)
                windows.append({
                    "app_name": app_name,
                    "pid": pid,
                    "window_id": window_id,
                    "title": child.name or "",
                    "is_active": is_active,
                    "bounds": bounds,
                })
                window_id += 1

        windows.sort(key=lambda w: (0 if w["is_active"] else 1))
        return windows

    def get_window_tree(self, window_id: int) -> dict[str, Any]:
        """Return an indexed element tree for the given window_id.

        window_id must match a value from list_windows().
        Elements are capped at _MAX_TREE_ELEMENTS (200) to avoid giant trees.
        Returns {"title": str, "elements": [{"index", "role", "name", "bounds"}]}.
        Returns {"title": "", "elements": []} if the window cannot be found.
        """
        accessible = self._resolve_window(window_id)
        if accessible is None:
            logger.warning("libatspi.get_window_tree.window_not_found id=%d", window_id)
            return {"title": "", "elements": []}

        title = accessible.name or ""
        elements: list[dict[str, Any]] = []
        accessibles: list[Any] = []

        for descendant in self._walk_bounded(accessible, _MAX_TREE_DEPTH):
            if len(elements) >= _MAX_TREE_ELEMENTS:
                break
            role = descendant.getRoleName()
            if role not in _RELEVANT_ROLES:
                continue
            bounds = self._get_bounds(descendant)
            idx = len(elements)
            elements.append({
                "index": idx,
                "role": role,
                "name": descendant.name or "",
                "bounds": bounds,
            })
            accessibles.append(descendant)

        self._element_cache[window_id] = (time.monotonic(), accessibles)
        return {"title": title, "elements": elements}

    def click_element(
        self,
        window_id: int,
        index: int,
        *,
        double: bool = False,
        button: str = "left",
    ) -> dict[str, Any] | None:
        """Activate element by index.

        Prefers queryAction().doAction(0) (default action).
        Falls back to returning bounds so the caller can use pointer click.
        Returns None on clean success, dict {"bounds": ...} when coord-fallback
        is needed, raises on unrecoverable error.
        """
        accessible = self._resolve_element(window_id, index)
        if accessible is None:
            logger.warning(
                "libatspi.click_element.not_found window=%d index=%d",
                window_id, index,
            )
            return None

        try:
            action_iface = accessible.queryAction()
            if action_iface is not None and action_iface.nActions > 0:
                action_name = "activate" if not double else "click"
                for i in range(action_iface.nActions):
                    if action_iface.getName(i).lower() in (action_name, "press", "click"):
                        action_iface.doAction(i)
                        logger.debug(
                            "libatspi.click_element.action_done window=%d index=%d action=%s",
                            window_id, index, action_iface.getName(i),
                        )
                        return None
                # No matching name — fall through to default action 0.
                action_iface.doAction(0)
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "libatspi.click_element.action_failed window=%d index=%d: %s — falling back to bounds",
                window_id, index, exc,
            )

        # Coord fallback: return bounds for the caller.
        bounds = self._get_bounds(accessible)
        if bounds:
            return {"bounds": bounds}
        return None

    def set_text_element(self, window_id: int, index: int, text: str) -> bool:
        """Set the text content of an editable element.

        Uses queryEditableText().setTextContents(text).
        Returns True on success, False when the interface is unavailable (caller
        should fall back to focus+type).
        """
        accessible = self._resolve_element(window_id, index)
        if accessible is None:
            logger.warning(
                "libatspi.set_text_element.not_found window=%d index=%d",
                window_id, index,
            )
            return False

        try:
            et_iface = accessible.queryEditableText()
            et_iface.setTextContents(text)
            logger.debug(
                "libatspi.set_text_element.ok window=%d index=%d len=%d",
                window_id, index, len(text),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "libatspi.set_text_element.failed window=%d index=%d: %s",
                window_id, index, exc,
            )
            return False

    def element_bounds(self, window_id: int, index: int) -> dict[str, Any] | None:
        """Return {x, y, w, h} for an element, or None if not found."""
        accessible = self._resolve_element(window_id, index)
        if accessible is None:
            return None
        return self._get_bounds(accessible)

    # ------------------------------------------------------------------
    # v2 private helpers
    # ------------------------------------------------------------------

    def _resolve_window(self, window_id: int) -> Any | None:
        """Return the Accessible for window_id (ordinal from list_windows)."""
        try:
            desktop = self._pyatspi.Registry.getDesktop(0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("libatspi._resolve_window.desktop_error: %s", exc)
            return None

        current_id = 0
        for app in desktop:
            if app is None:
                continue
            for child in app:
                if child is None:
                    continue
                role_name = child.getRoleName()
                if role_name not in ("frame", "window", "dialog"):
                    continue
                if current_id == window_id:
                    return child
                current_id += 1
        return None

    def _resolve_element(self, window_id: int, index: int) -> Any | None:
        """Return the cached Accessible for (window_id, index).

        Re-walks the window tree if the cache entry is missing or stale.
        """
        cached = self._element_cache.get(window_id)
        if cached is not None:
            ts, accessibles = cached
            if time.monotonic() - ts < _CACHE_TTL_S and index < len(accessibles):
                return accessibles[index]

        # Cache miss or stale — rebuild.
        self.get_window_tree(window_id)
        cached = self._element_cache.get(window_id)
        if cached is None:
            return None
        _, accessibles = cached
        if index < len(accessibles):
            return accessibles[index]
        return None

    def _has_state_active(self, accessible: Any) -> bool:
        try:
            state_set = accessible.getState()
            return self._pyatspi.STATE_ACTIVE in state_set.getStates()
        except Exception:  # noqa: BLE001
            return False

    def _get_pid(self, app: Any) -> int:
        try:
            return app.get_process_id()
        except AttributeError:
            pass
        try:
            return app.getApplication().get_process_id()
        except Exception:  # noqa: BLE001
            return 0

    def _get_bounds(self, accessible: Any) -> dict[str, int]:
        try:
            component = accessible.queryComponent()
            ext = component.getExtents(self._pyatspi.DESKTOP_COORDS)
            return {"x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height}
        except Exception:  # noqa: BLE001
            return {}

    def _walk_bounded(self, root: Any, max_depth: int):
        """Depth-bounded walk; yields root then children recursively."""
        yield root
        if max_depth <= 0:
            return
        try:
            child_count = root.childCount
        except Exception:  # noqa: BLE001
            return
        for i in range(child_count):
            try:
                child = root.getChildAtIndex(i)
            except Exception:  # noqa: BLE001
                continue
            if child is None:
                continue
            yield from self._walk_bounded(child, max_depth - 1)


def is_available() -> bool:
    """Verifica si pyatspi se puede importar."""
    try:
        import pyatspi  # noqa: F401
        return True
    except ImportError:
        return False
