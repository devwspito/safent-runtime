"""hermes-shell — entrypoint GTK4 + libadwaita de la Hermes Shell.

Esta es la UI del SO. Reemplaza gnome-shell.

Invocación:
    hermes-shell                 # arranca pantalla completa kiosk
    hermes-shell --windowed      # debug local (no fullscreen)
    hermes-shell --no-runtime    # mocked runtime para iteración UI offline
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import urllib.error

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk

    _GTK_AVAILABLE = True
except (ImportError, ValueError) as exc:  # pragma: no cover
    Adw = None  # type: ignore[assignment]
    Gtk = None  # type: ignore[assignment]
    _GTK_AVAILABLE = False
    _import_error = exc

import asyncio
import threading

from hermes.shell.application.runtime_backend_health_monitor import (
    MonitorConfig,
    RuntimeBackendHealthMonitor,
)
from hermes.shell.domain.shell_session import (
    RuntimeLinkState,
    start_session,
)
from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient
from hermes.shell.presentation.gtk4.window import HermesShellWindow

logger = logging.getLogger("hermes-shell")

_APP_ID = "ai.hermes.Shell"

# Boot gate. The graphical shell (kiosk, launched by GDM autologin) can come up
# before the shell-server (a system service that first needs master.key + a
# FastAPI boot). The first-boot decision must therefore WAIT for a definitive
# answer from the backend instead of treating "not ready yet" as "already
# onboarded" — otherwise a startup race permanently skips the onboarding wizard
# on a fresh install. We poll wizard_status() until it answers, bounded so boot
# never hangs if the backend is genuinely down.
_BOOT_GATE_TIMEOUT_S = 30.0
_BOOT_GATE_POLL_S = 0.5


def _bootstrap_theme(display) -> "ThemeManager":
    """Inicializa el sistema de temas Sereno (ThemeManager).

    Reemplaza la carga monolítica de hermes.css por 3 providers:
      1. tokens-light.css o tokens-dark.css (según modo persistido o auto)
      2. components.css (todo el estilado de componentes, sin hex)
      3. CSS de acento generado en memoria (acento persistido o Azul por defecto)

    hermes.css se conserva en disco como respaldo pero ya no se carga aquí.

    Retorna el ThemeManager para que la aplicación lo pase a la ventana
    principal y ésta lo exponga al panel de Ajustes → Apariencia.
    """
    from hermes.shell.presentation.gtk4.theme_manager import ThemeManager  # noqa: PLC0415

    mgr = ThemeManager(display)
    mgr.bootstrap()
    logger.info("ThemeManager bootstrapped")
    return mgr




class HermesShellApplication(Adw.Application):
    """Adw.Application que arranca la ventana shell en modo kiosk.

    Boot decision:
      1. Present a blank window immediately so the display isn't frozen.
      2. In a background thread, call wizard_status().
      3a. If first_boot_complete is False → show FirstBootWizardView.
      3b. If first_boot_complete is True (or the call fails gracefully) →
          show the normal HermesShellWindow content.
    """

    def __init__(self, *, windowed: bool, mock_runtime: bool) -> None:
        super().__init__(
            application_id=_APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._windowed = windowed
        self._mock_runtime = mock_runtime
        self._root_window: Adw.ApplicationWindow | None = None
        self._client = ShellBackendClient()
        self._monitor: RuntimeBackendHealthMonitor | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_loop: asyncio.AbstractEventLoop | None = None
        # ThemeManager singleton del proceso — asignado en do_activate y pasado
        # a HermesShellWindow para que el panel Ajustes → Apariencia lo reutilice.
        self.theme_manager = None

    def do_activate(self) -> None:  # type: ignore[override]
        display = Gdk.Display.get_default()
        if display is not None:
            self.theme_manager = _bootstrap_theme(display)
        else:
            logger.warning("no hay display disponible — CSS no cargado")

        # Present a minimal holding window immediately — the display must not
        # stay blank while we wait for the status call.
        self._root_window = self._build_holding_window()
        if not self._windowed:
            self._root_window.maximize()
            self._root_window.present()
            self._root_window.fullscreen()
        else:
            self._root_window.present()

        signal.signal(signal.SIGTERM, lambda *_: self.quit())
        signal.signal(signal.SIGINT, lambda *_: self.quit())

        # Probe wizard status off the main thread.
        threading.Thread(
            target=self._thread_check_wizard_status,
            daemon=True,
            name="hermes-boot-gate",
        ).start()

    # ------------------------------------------------------------------
    # Boot gate — background check
    # ------------------------------------------------------------------

    def _thread_check_wizard_status(self) -> None:
        # Retry until the shell-server gives a definitive answer. A connection
        # or HTTP error means "backend not ready yet" (startup race) — keep
        # waiting rather than falling back to first_boot_complete=True, which
        # would skip onboarding forever on a fresh install. Only after the
        # bounded deadline (or a non-network error) do we degrade to the shell.
        deadline = time.monotonic() + _BOOT_GATE_TIMEOUT_S
        first_boot_complete = True  # only used if the backend never answers
        attempt = 0
        while True:
            attempt += 1
            try:
                status = self._client.wizard_status()
                first_boot_complete = bool(status.get("first_boot_complete", True))
                logger.info(
                    "wizard_status() -> first_boot_complete=%s (attempt %d)",
                    first_boot_complete,
                    attempt,
                )
                break
            except urllib.error.URLError as exc:
                # Includes connection-refused (backend down) and HTTPError
                # (e.g. transient 503 while master.key/DB warm up).
                if time.monotonic() >= deadline:
                    logger.warning(
                        "shell-server not ready after %.0fs (%s) — showing shell",
                        _BOOT_GATE_TIMEOUT_S,
                        exc,
                    )
                    break
                logger.info("shell-server not ready (%s) — retry %d", exc, attempt)
                time.sleep(_BOOT_GATE_POLL_S)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wizard_status() failed (%s) — showing shell", exc
                )
                break
        GLib.idle_add(self._on_boot_decision, first_boot_complete)

    def _on_boot_decision(self, first_boot_complete: bool) -> bool:
        if first_boot_complete:
            self._show_normal_shell()
        else:
            self._show_wizard()
        return False

    # ------------------------------------------------------------------
    # Window content builders
    # ------------------------------------------------------------------

    def _build_holding_window(self) -> Adw.ApplicationWindow:
        win = Adw.ApplicationWindow(application=self)
        win.set_title("Hermes")
        win.set_default_size(1440, 900)
        win.add_css_class("hermes-shell")
        # Branded holding screen while the boot gate waits for the shell-server.
        # Usually <1 s, but can be a few seconds on a cold first boot — a label
        # avoids a frozen-looking blank canvas.
        placeholder = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.CENTER,
        )
        placeholder.add_css_class("hermes-shell")
        starting = Gtk.Label(label="Iniciando Hermes…")
        starting.add_css_class("title-2")
        placeholder.append(starting)
        win.set_content(placeholder)
        return win

    def _show_wizard(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.setup_wizard_view import (  # noqa: PLC0415
            HermesSetupWizardView,
        )

        wizard = HermesSetupWizardView(client=self._client)
        wizard.connect("wizard-finished", self._on_wizard_finished)
        assert self._root_window is not None
        self._root_window.set_content(wizard)
        logger.info("setup wizard (form stepper) presented")

    def _show_normal_shell(self) -> None:
        session = start_session(
            human_user_id=os.environ.get("USER", "hermes-user"),
            tenant_id=None,
        )
        # Initial state is OFFLINE; the monitor will drive transitions via
        # GLib.idle_add on the GTK main thread (T029, US1).
        session.mark_runtime_link(RuntimeLinkState.OFFLINE)

        shell_window = HermesShellWindow(
            application=self,
            session=session,
            mock_runtime=self._mock_runtime,
            theme_manager=self.theme_manager,
        )

        if not self._mock_runtime:
            self._start_health_monitor(session, shell_window)
        else:
            # Mock mode: simulate RECONNECTING → CONNECTED after a short delay.
            GLib.timeout_add(800, lambda: self._mock_connect(shell_window))

        # Replace the holding window with the real shell window.
        assert self._root_window is not None
        self._root_window.close()
        self._root_window = None

        if not self._windowed:
            self._present_fullscreen(shell_window)
        else:
            shell_window.set_default_size(1280, 800)
            shell_window.present()
        logger.info("normal shell presented")

    def _present_fullscreen(self, win: HermesShellWindow) -> None:
        """Belt-and-suspenders fullscreen for Mutter headless/Wayland.

        Mutter headless is flaky: fullscreen() on an unmapped window is often
        ignored (the compositor hasn't allocated the surface yet). Strategy:
        1. Query the monitor geometry and set_default_size to the full extent —
           this works even before mapping and covers compositors that ignore
           fullscreen hints.
        2. maximize() as a fallback in case fullscreen is ignored.
        3. present() to map the surface.
        4. fullscreen() after present() — the surface now exists.
        5. Connect to the 'map' signal to re-apply fullscreen once the window
           is actually realized by the compositor (handles the race on slow
           compositors or remote desktops).
        """
        def _apply_geometry() -> None:
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors.get_n_items() > 0:
                    geom = monitors.get_item(0).get_geometry()
                    # set_default_size only bites pre-map; harmless after.
                    win.set_default_size(geom.width, geom.height)
                else:
                    win.set_default_size(1920, 1080)
            else:
                win.set_default_size(1920, 1080)
            win.maximize()
            # fullscreen() is what actually sticks post-map; it supersedes
            # maximize and covers the whole monitor on the remote display.
            win.fullscreen()

        _apply_geometry()
        win.present()
        win.fullscreen()

        # Mutter headless + remote desktop (GRD / noVNC with resize=scale) often
        # maps the surface or resizes the virtual monitor AFTER present(); a
        # one-shot fullscreen() loses that race and the window ends up at a
        # partial height. Re-apply on map, on a few timed retries, and whenever
        # the monitor geometry changes (remote viewport resize).
        win.connect("map", lambda _w: _apply_geometry())

        def _retry() -> bool:
            _apply_geometry()
            return GLib.SOURCE_REMOVE

        for delay_ms in (150, 500, 1200, 2500):
            GLib.timeout_add(delay_ms, _retry)

        display = Gdk.Display.get_default()
        if display is not None:
            monitors = display.get_monitors()
            monitors.connect("items-changed", lambda *_a: _apply_geometry())
            if monitors.get_n_items() > 0:
                monitors.get_item(0).connect(
                    "notify::geometry", lambda *_a: _apply_geometry()
                )

    # ------------------------------------------------------------------
    # Health monitor — drives RuntimeLinkState from background thread
    # ------------------------------------------------------------------

    def _start_health_monitor(
        self,
        session,
        shell_window: HermesShellWindow,
    ) -> None:
        """Launch RuntimeBackendHealthMonitor in a dedicated asyncio thread.

        State changes are bridged to the GTK main thread via GLib.idle_add
        (the only safe way to touch GTK widgets from a background thread).
        """
        from hermes.shell.infrastructure.dbus_fast_runtime_client import (
            DbusRuntimeClient,
            FakeDbusInterface,
        )

        # In production: replace FakeDbusInterface with the real dbus-fast proxy.
        # FakeDbusInterface is used here so the shell starts up without a running
        # daemon (the monitor will emit OFFLINE immediately, which is correct).
        dbus_iface = FakeDbusInterface()
        runtime_client = DbusRuntimeClient(dbus_interface=dbus_iface)

        class _NoopBus:
            def subscribe_name_owner_changed(self, _cb):
                pass

        def _on_state_change(new_state: RuntimeLinkState) -> None:
            # Called from the monitor's asyncio thread — must not touch GTK.
            def _apply() -> bool:
                session.mark_runtime_link(new_state)
                shell_window.on_runtime_state_changed(new_state)
                return False

            GLib.idle_add(_apply)

        monitor = RuntimeBackendHealthMonitor(
            runtime_port=runtime_client,
            event_bus=_NoopBus(),
            config=MonitorConfig(),
            on_state_change=_on_state_change,
        )
        self._monitor = monitor

        loop = asyncio.new_event_loop()
        self._monitor_loop = loop

        def _run() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(monitor.run())
            except Exception as exc:  # noqa: BLE001
                logger.warning("health monitor exited: %s", exc)
            finally:
                loop.close()

        thread = threading.Thread(
            target=_run,
            daemon=True,
            name="hermes-health-monitor",
        )
        self._monitor_thread = thread
        thread.start()
        logger.info("health monitor thread started")

    def _mock_connect(self, shell_window: HermesShellWindow) -> bool:
        shell_window.on_runtime_state_changed(RuntimeLinkState.CONNECTED)
        return False

    # ------------------------------------------------------------------
    # Wizard → shell transition
    # ------------------------------------------------------------------

    def _on_wizard_finished(self, _wizard_view) -> None:
        """Called on the GTK main thread when wizard-finished fires."""
        logger.info("wizard finished — transitioning to normal shell")
        self._show_normal_shell()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes-shell")
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="No fullscreen kiosk (debug local)",
    )
    parser.add_argument(
        "--no-runtime",
        action="store_true",
        help="No conectar al runtime real, usar mock",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    if not _GTK_AVAILABLE:
        logger.error(
            "GTK4 + libadwaita no disponibles: %s. "
            "Instala gtk4 libadwaita python3-gobject.",
            _import_error,
        )
        return 1

    app = HermesShellApplication(
        windowed=args.windowed,
        mock_runtime=args.no_runtime,
    )
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
