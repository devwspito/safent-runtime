"""HermesTrainingPanel — pestaña Grabación del workspace.

UI del flow F6 / F6.1 (in-session capture):
  - Si se llega desde el flujo "+ Enseñar skill" de HermesSkillsView, la skill
    ya está creada (session_id minted, nombre/descripción conocidos): el panel
    NO vuelve a pedirlos y va directo al step "Abrir navegador" (change #3/#4).
  - Si se usa el panel de forma independiente (training tab legacy), el botón
    "Enseñar habilidad" sigue siendo el punto de entrada (retro-compat).

Máquina de estados (change #4):
  no_session → idle → browser_opening → browser_open
             → recording → paused → recording → finalized → review → signed
  En cualquier estado excepto signed/abandoned → abandoned.

  - "Abrir navegador"  : idle → browser_open   (lanza Chromium aislado)
  - "Iniciar grabación": browser_open → recording  (NUNCA automático)
  - "Pausar grabación" : recording → paused
  - "Reanudar"         : paused → recording
  - "Finalizar grabación": recording/paused → finalized → review
  - "Firmar y guardar" : review → signed
  - "Abandonar"        : cualquier estado activo → abandoned

  # TODO: mover controles de grabación a overlay flotante sobre el navegador
  #        via CDP (chrome.debugger / DevTools Protocol) una vez que el wiring
  #        CDP esté disponible en el Capability Broker.

Degradación:
  - Si gi/Gst/Mutter no están (headless, dev VM sin GPU): coordinator factory
    devuelve None. El panel entra en "capture_unavailable" y muestra mensaje.
  - Sin micrófono real: probe_mic_backend() → mic_backend=None, audio_available=False.
    El panel entra en "needs_microphone" y bloquea la grabación.
    El usuario puede decidir "Entrenar sin voz" (su elección, no degradación automática).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from uuid import UUID

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import (  # noqa: E402
    ShellBackendClient,
)

logger = logging.getLogger(__name__)

# Path to the shared SQLite DB (same as hermes-shell-server).
_DB_PATH = Path(os.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db"))


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Recording state machine
# ---------------------------------------------------------------------------

# Status page copy per state.
_STATE_COPY: dict[str, tuple[str, str]] = {
    "no_session": (
        "Enseñar una habilidad",
        "Pulsa 'Enseñar habilidad', graba la tarea (pantalla + voz + clicks)\n"
        "y firma la skill para que Hermes pueda reusarla.",
    ),
    "idle": (
        "Listo para empezar",
        "Pulsa 'Abrir navegador' para lanzar un espacio aislado.\n"
        "La grabación NO empieza hasta que pulses 'Iniciar grabación'.",
    ),
    "browser_opening": (
        "Abriendo navegador…",
        "Espera mientras se lanza Chromium en el espacio aislado.\n"
        "La grabación NO ha empezado.",
    ),
    "browser_open": (
        "Navegador abierto",
        "El navegador aislado está listo. Navega al sitio, prepara la tarea.\n"
        "Cuando estés listo, pulsa 'Iniciar grabación'.\n"
        "PipeWire pedirá consentimiento para capturar pantalla + audio.",
    ),
    "recording": (
        "Grabando…",
        "Realiza la tarea como lo harías normalmente. Habla en voz alta\n"
        "explicando los pasos. Pulsa 'Pausar' para descansar, o 'Finalizar' al acabar.",
    ),
    "paused": (
        "Grabación pausada",
        "La captura está en pausa. Pulsa 'Reanudar' para continuar o\n"
        "'Finalizar grabación' cuando hayas terminado.",
    ),
    "finalized": (
        "Grabación finalizada",
        "La captura ha terminado. Pulsa 'Firmar y guardar' para validar la skill;\n"
        "si algo salió mal, abandona y vuelve a enseñar.",
    ),
    "review": (
        "Revisión de la demostración",
        "Revisa los pasos capturados de la demostración antes de firmar.\n"
        "Si es correcta, pulsa 'Firmar y guardar' para validar la skill;\n"
        "si no, abandona y vuelve a enseñar.",
    ),
    "signed": (
        "Skill firmada ✓",
        "La skill ya está disponible. Empieza otra cuando quieras.",
    ),
    "abandoned": (
        "Sesión abandonada",
        "Empieza otra cuando quieras.",
    ),
    "capture_unavailable": (
        "Captura no disponible",
        "La captura de pantalla y audio requiere una sesión gráfica con\n"
        "Mutter y PipeWire. Este entorno no los tiene disponibles.\n"
        "Conéctate a la sesión de hermes-user para grabar skills.",
    ),
    "needs_microphone": (
        "Hermes necesita oírte",
        "El entrenamiento se basa en tu explicación hablada: Hermes\n"
        "aprende la intención de lo que narras mientras lo haces.\n"
        "No detecto un micrófono. Conecta uno y reintenta.\n"
        "Si prefieres entrenar sin voz (no recomendado), pulsa\n"
        "'Entrenar sin voz' — la skill aprenderá solo de las acciones.",
    ),
}


class HermesTrainingPanel(Gtk.Box):
    """Training capture panel driven in-session by TrainingCaptureCoordinator.

    Puede construirse de dos formas:

    1. Modo "inject" (flujo Enseñar skill, change #3):
       Se pasan ``prefilled_skill_name``, ``prefilled_session_id``, etc. desde
       HermesSkillsView.  El panel salta directo a estado ``idle`` (o el que
       indique ``prefilled_state``) mostrando el nombre como título de solo
       lectura.  No muestra el botón "Enseñar habilidad" ni abre ningún diálogo.

    2. Modo legacy (training tab standalone):
       Sin ``prefilled_session_id`` → ``no_session``.  El botón "Enseñar
       habilidad" es el punto de entrada.
    """

    def __init__(  # noqa: PLR0915 — rich button setup required by state machine
        self,
        *,
        client: ShellBackendClient,
        teaching_context: dict | None = None,
        # Campos pre-rellenos desde HermesSkillsView (change #3)
        prefilled_skill_name: str = "",
        prefilled_description: str = "",
        prefilled_site_url: str = "",
        prefilled_session_id: str | None = None,
        prefilled_state: str = "idle",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._client = client
        self._teaching_context: dict | None = teaching_context
        self._skill_name: str = prefilled_skill_name
        self._description: str = prefilled_description
        self._site_url: str = prefilled_site_url

        # Hydrate session_id when pre-filled (session already created upstream).
        if prefilled_session_id:
            try:
                self._session_id: UUID | None = UUID(prefilled_session_id)
            except ValueError:
                logger.warning("invalid prefilled_session_id: %s", prefilled_session_id)
                self._session_id = None
        else:
            self._session_id = None

        # When the panel is pre-loaded with a session, start at the provided
        # state (typically "idle" meaning browser not opened yet).
        self._state: str = prefilled_state if prefilled_session_id else "no_session"

        # Coordinator + orchestrator are built lazily on first use so that the
        # panel can be constructed headlessly (workspace fallback path).
        self._coordinator = None
        self._orchestrator = None
        self._capture_available: bool | None = None  # None = not yet probed
        # audio_available False → no hay micrófono real. No degradamos solos.
        self._audio_available: bool = False
        self._train_without_voice_ack: bool = False

        # Periodic screenshot timer (GLib source id).
        self._screenshot_timer_id: int | None = None

        # ------------------------------------------------------------------
        # Skill title header (read-only, shown when pre-filled — change #3)
        # ------------------------------------------------------------------
        self._skill_header: Gtk.Widget | None = None
        if prefilled_skill_name:
            self._skill_header = self._build_skill_header(
                prefilled_skill_name, prefilled_description
            )
            self.append(self._skill_header)

        # ------------------------------------------------------------------
        # Isolation banner (show when teaching_context is set — spec 004)
        # ------------------------------------------------------------------
        self._isolation_banner: Gtk.Widget | None = None
        if self._teaching_context:
            self._isolation_banner = self._build_isolation_banner(
                self._teaching_context
            )
            self.append(self._isolation_banner)

        # ------------------------------------------------------------------
        # Status page
        # ------------------------------------------------------------------
        self._status_page = Adw.StatusPage()
        self._status_page.set_icon_name("media-record-symbolic")
        self._status_page.set_vexpand(True)
        self.append(self._status_page)

        # ------------------------------------------------------------------
        # Controls row — ordered per the new state machine (change #4).
        # Visible/sensitive toggled per state in _update_view.
        # Layout: [Abandonar] [spacer] [secondary…] [primary]
        # ------------------------------------------------------------------
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.add_css_class("hermes-panel-actionbar")
        controls.set_halign(Gtk.Align.FILL)

        # Abandon — leftmost, destructive, available whenever a session is active.
        self._abandon_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self._abandon_btn.set_tooltip_text("Abandonar sesión")
        self._abandon_btn.add_css_class("flat")
        self._abandon_btn.add_css_class("destructive-action")
        self._abandon_btn.connect("clicked", lambda _b: self._on_abandon())
        controls.append(self._abandon_btn)

        # Spacer — pushes action buttons to the right.
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        controls.append(spacer)

        # Legacy entry-point: only shown in standalone (no pre-fill) mode.
        self._new_btn = Gtk.Button.new_with_label("Enseñar habilidad")
        self._new_btn.add_css_class("hermes-primary")
        self._new_btn.connect("clicked", lambda _b: self._open_new_dialog())
        controls.append(self._new_btn)

        # Step 1 (change #4-a): open isolated browser BEFORE recording.
        self._open_browser_btn = Gtk.Button.new_with_label("Abrir navegador")
        self._open_browser_btn.add_css_class("hermes-primary")
        self._open_browser_btn.set_tooltip_text(
            "Abre un Chromium aislado en el sitio objetivo. "
            "La grabación NO empieza hasta que pulses 'Iniciar grabación'."
        )
        self._open_browser_btn.connect("clicked", lambda _b: self._on_open_browser())
        controls.append(self._open_browser_btn)

        # Conscious user override: train without voice when no mic detected.
        self._without_voice_btn = Gtk.Button.new_with_label("Entrenar sin voz")
        self._without_voice_btn.add_css_class("flat")
        self._without_voice_btn.set_tooltip_text(
            "No recomendado: Hermes aprenderá solo de las acciones, sin tu "
            "explicación hablada."
        )
        self._without_voice_btn.connect(
            "clicked", lambda _b: self._on_train_without_voice()
        )
        controls.append(self._without_voice_btn)

        # Step 2 (change #4-b): start recording — ONLY after browser is open.
        self._start_btn = Gtk.Button.new_with_label("Iniciar grabación")
        self._start_btn.add_css_class("hermes-ghost")
        self._start_btn.connect("clicked", lambda _b: self._on_start())
        controls.append(self._start_btn)

        # Pause (change #4-b): reanudable.
        self._pause_btn = Gtk.Button.new_with_label("Pausar")
        self._pause_btn.add_css_class("hermes-ghost")
        self._pause_btn.set_tooltip_text("Pausar la grabación; podrás reanudar")
        self._pause_btn.connect("clicked", lambda _b: self._on_pause())
        controls.append(self._pause_btn)

        # Resume (change #4-b): from paused back to recording.
        self._resume_btn = Gtk.Button.new_with_label("Reanudar")
        self._resume_btn.add_css_class("hermes-ghost")
        self._resume_btn.connect("clicked", lambda _b: self._on_resume())
        controls.append(self._resume_btn)

        # Finalize recording (change #4-b): stops capture, enters review.
        self._finalize_btn = Gtk.Button.new_with_label("Finalizar grabación")
        self._finalize_btn.add_css_class("hermes-ghost")
        self._finalize_btn.connect("clicked", lambda _b: self._on_stop())
        controls.append(self._finalize_btn)

        # Sign — only enabled in review/finalized state.
        self._sign_btn = Gtk.Button.new_with_label("Firmar y guardar")
        self._sign_btn.add_css_class("hermes-primary")
        self._sign_btn.connect("clicked", lambda _b: self._on_sign())
        controls.append(self._sign_btn)

        self.append(controls)

        # Render initial state after all buttons are built (_update_view
        # touches every button — calling before appending crashes).
        self._update_view()

    # ------------------------------------------------------------------
    # Static builders for read-only headers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_skill_header(name: str, description: str) -> Gtk.Widget:
        """Read-only skill name banner (change #3 — no double form)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("hermes-teach-banner")
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(8)

        name_lbl = Gtk.Label()
        name_lbl.set_markup(f"<b>Enseñando:</b> {GLib.markup_escape_text(name)}")
        name_lbl.set_xalign(0)
        box.append(name_lbl)

        if description:
            desc_lbl = Gtk.Label(label=description)
            desc_lbl.add_css_class("dim-label")
            desc_lbl.set_xalign(0)
            desc_lbl.set_wrap(True)
            box.append(desc_lbl)

        return box

    @staticmethod
    def _build_isolation_banner(teaching_ctx: dict) -> Gtk.Widget:
        """Infobar shown when the panel is bound to a teaching context (spec 004)."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.add_css_class("hermes-teach-banner")

        icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        icon.set_pixel_size(16)
        box.append(icon)

        isolation_key = teaching_ctx.get("isolation_key", "—")
        surface = teaching_ctx.get("surface_kind", "browser")
        lbl = Gtk.Label()
        lbl.set_markup(
            "<b>Espacio aislado</b>  ·  "
            f"superficie: {surface}  ·  clave: {isolation_key}\n"
            "<small>Estás enseñando en un espacio propio; "
            "el agente sigue trabajando sin interrupción.</small>"
        )
        lbl.set_xalign(0)
        lbl.set_wrap(True)
        lbl.set_hexpand(True)
        box.append(lbl)

        return box

    # ------------------------------------------------------------------
    # Legacy dialog (standalone training tab, no pre-fill)
    # ------------------------------------------------------------------

    def _open_new_dialog(self) -> None:
        dlg = _NewSkillDialog(parent=self.get_root(), on_save=self._create_session)
        dlg.present()

    # ------------------------------------------------------------------
    # Step 0 (legacy): Create session in DB via REST (mints session_id)
    # ------------------------------------------------------------------

    def _create_session(self, *, skill_name: str, description: str) -> None:
        """POST /api/v1/training — single source of truth for session_id.

        Only used in legacy standalone mode.  In the Enseñar-skill flow the
        session is created upstream by start_teaching() before this panel opens.
        """
        self._train_without_voice_ack = False
        self._skill_name = skill_name
        self._description = description

        def runner() -> None:
            try:
                data = self._client._request(  # type: ignore[attr-defined]
                    path="/api/v1/training",
                    method="POST",
                    body={"skill_name": skill_name, "description": description},
                )
                self._session_id = UUID(data["session_id"])
                self._state = "idle"
            except Exception as exc:  # noqa: BLE001
                logger.warning("create training session: %s", exc)
                _msg = str(exc)
                GLib.idle_add(lambda: self._show_error(_msg))
                return
            GLib.idle_add(self._update_view)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Step 1 (change #4-a): Open isolated browser BEFORE recording
    # ------------------------------------------------------------------

    def _on_open_browser(self) -> None:
        """Open the isolated Chromium window.  Recording does NOT start yet."""
        if not self._session_id:
            return
        self._state = "browser_opening"
        self._update_view()

        # Determine the URL to open: prefer site_url from the teaching context
        # (set by HermesSkillsView._launch_teaching_session), then the
        # prefilled_site_url, then fall back to about:blank.
        ctx = self._teaching_context or {}
        url = ctx.get("site_url") or self._site_url or "about:blank"

        def runner() -> None:
            self._launch_isolated_browser(url)
            self._state = "browser_open"
            GLib.idle_add(self._update_view)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Step 2 (change #4-b): Start recording — ONLY on explicit user action
    # ------------------------------------------------------------------

    def _on_train_without_voice(self) -> None:
        """Conscious user override: train without mic (not an automatic fallback)."""
        self._train_without_voice_ack = True
        self._on_start()

    def _on_start(self) -> None:
        """Start capture — called only from button click, never automatically."""
        if not self._session_id:
            return

        def runner() -> None:
            if not self._ensure_coordinator():
                self._state = "capture_unavailable"
                GLib.idle_add(self._update_view)
                return

            if not self._audio_available and not self._train_without_voice_ack:
                self._state = "needs_microphone"
                GLib.idle_add(self._update_view)
                return

            try:
                self._client._request(  # type: ignore[attr-defined]
                    path=f"/api/v1/training/{self._session_id}/start",
                    method="POST",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("training start REST: %s", exc)
                _msg = str(exc)
                GLib.idle_add(lambda: self._show_error(_msg))
                return

            try:
                assert self._coordinator is not None
                self._coordinator.begin(
                    session_id=self._session_id,
                    skill_name=self._skill_name,
                    voice_opt_out=self._train_without_voice_ack,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("coordinator.begin failed: %s", exc)

            self._state = "recording"
            GLib.idle_add(self._update_view)
            GLib.idle_add(self._start_screenshot_timer)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Step 2b (change #4-b): Pause / Resume
    # ------------------------------------------------------------------

    def _on_pause(self) -> None:
        """Pause the in-flight recording.  Reanudable."""
        if self._state != "recording" or not self._session_id:
            return

        self._stop_screenshot_timer()

        def runner() -> None:
            coord = self._coordinator
            if coord is not None and hasattr(coord, "pause"):
                try:
                    coord.pause(session_id=self._session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("coordinator.pause: %s", exc)
            # TODO: call /api/v1/training/{id}/pause when the endpoint exists.
            self._state = "paused"
            GLib.idle_add(self._update_view)

        threading.Thread(target=runner, daemon=True).start()

    def _on_resume(self) -> None:
        """Resume a paused recording."""
        if self._state != "paused" or not self._session_id:
            return

        def runner() -> None:
            coord = self._coordinator
            if coord is not None and hasattr(coord, "resume"):
                try:
                    coord.resume(session_id=self._session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("coordinator.resume: %s", exc)
            # TODO: call /api/v1/training/{id}/resume when the endpoint exists.
            self._state = "recording"
            GLib.idle_add(self._update_view)
            GLib.idle_add(self._start_screenshot_timer)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Step 3: Finalize recording (was "Terminar")
    # ------------------------------------------------------------------

    def _on_stop(self) -> None:
        """Finalize the capture — transitions to review."""
        if not self._session_id:
            return

        self._stop_screenshot_timer()

        def runner() -> None:
            coord = self._coordinator
            if coord is not None:
                try:
                    coord.end(session_id=self._session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("coordinator.end: %s", exc)

            try:
                self._client._request(  # type: ignore[attr-defined]
                    path=f"/api/v1/training/{self._session_id}/stop",
                    method="POST",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("training stop REST: %s", exc)
                _msg = str(exc)
                GLib.idle_add(lambda: self._show_error(_msg))
                return

            self._state = "review"
            GLib.idle_add(self._update_view)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Step 4: Sign — compile + persist locally, then notify server
    # ------------------------------------------------------------------

    def _on_sign(self) -> None:
        if not self._session_id:
            return

        def runner() -> None:
            now = _now_iso()

            voice_captions: list[str] = []
            if self._coordinator is not None:
                try:
                    voice_captions = self._coordinator.collected_voice_captions(
                        session_id=self._session_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("collected_voice_captions: %s", exc)

            from hermes.agents_os.application.training_session_orchestrator import (  # noqa: PLC0415
                VoiceCaptureRequired,
            )
            from hermes.shell_server.training.persist import (  # noqa: PLC0415
                compile_and_persist,
            )

            skill_persisted = False
            if self._orchestrator is not None:
                try:
                    skill_persisted = compile_and_persist(
                        db_path=_DB_PATH,
                        orchestrator=self._orchestrator,
                        session_id=self._session_id,
                        skill_name=self._skill_name,
                        signed_at=now,
                        voice_captions=voice_captions,
                    )
                except VoiceCaptureRequired:
                    logger.warning(
                        "sign rechazado: sesión requería voz, transcript vacío "
                        "session=%s",
                        self._session_id,
                    )
                    self._state = "needs_microphone"
                    GLib.idle_add(self._update_view)
                    return
                except Exception:
                    logger.exception(
                        "compile_and_persist failed session=%s", self._session_id
                    )
                    GLib.idle_add(
                        lambda: self._show_error("No se pudo guardar la skill.")
                    )
                    return

            if skill_persisted:
                try:
                    self._client._request(  # type: ignore[attr-defined]
                        path=f"/api/v1/training/{self._session_id}/sign",
                        method="POST",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("training sign REST (mirror): %s", exc)

            logger.info(
                "training.signed session=%s skill=%s persisted=%s",
                self._session_id,
                self._skill_name,
                skill_persisted,
            )
            self._state = "signed" if skill_persisted else "review"
            GLib.idle_add(self._update_view)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Abandon
    # ------------------------------------------------------------------

    def _on_abandon(self) -> None:
        if not self._session_id:
            return

        self._stop_screenshot_timer()

        coord = self._coordinator
        session_id = self._session_id

        def runner() -> None:
            if coord is not None:
                try:
                    coord.end(session_id=session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("coordinator.end (abandon): %s", exc)

            try:
                self._client._request(  # type: ignore[attr-defined]
                    path=f"/api/v1/training/{session_id}/abandon",
                    method="POST",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("training abandon REST: %s", exc)
            self._state = "abandoned"
            GLib.idle_add(self._update_view)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Isolated browser launch (change #4-a)
    # ------------------------------------------------------------------

    def _launch_isolated_browser(self, url: str) -> None:
        """Abre un Chromium visible con perfil aislado para la demostración.

        La grabación NO se inicia aquí — el usuario decide cuándo pulsar
        'Iniciar grabación' (change #4).

        # TODO: mover los controles de grabación a un overlay flotante sobre
        #        el navegador via CDP (chrome.debugger / DevTools Protocol).
        """
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        binary = shutil.which("chromium-browser") or shutil.which("chromium")
        if binary is None:
            GLib.idle_add(
                lambda: self._show_error(
                    "No encuentro Chromium para abrir el espacio de enseñanza."
                )
            )
            return

        ctx = self._teaching_context or {}
        key = ctx.get("context_id") or str(self._session_id)
        profile = os.path.expanduser(f"~/.local/share/hermes/teach-{key}")
        os.makedirs(profile, exist_ok=True)
        try:
            subprocess.Popen(  # noqa: S603 — binary resolved by which, no shell
                [
                    binary,
                    "--ozone-platform=wayland",
                    f"--user-data-dir={profile}",
                    "--new-window",
                    "--no-first-run",
                    # Sin llavero GNOME: con autologin passwordless Chromium pediría
                    # la contraseña del login-keyring al abrir (el prompt que salía
                    # al enseñar una skill con navegador). store plano = sin prompt.
                    "--password-store=basic",
                    "--use-mock-keychain",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "teaching: isolated chromium launched profile=%s url=%s", profile, url
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("teaching chromium launch failed: %s", exc)
            _e = str(exc)
            GLib.idle_add(
                lambda: self._show_error(
                    f"No pude abrir el navegador de enseñanza: {_e}"
                )
            )

    # ------------------------------------------------------------------
    # Coordinator / orchestrator lifecycle
    # ------------------------------------------------------------------

    def _ensure_coordinator(self) -> bool:
        """Build the in-session coordinator if not already done.

        Returns True if capture is available, False if we should degrade.
        Called from a background thread.
        """
        if self._capture_available is not None:
            return self._capture_available

        try:
            from hermes.agents_os.application.training_session_orchestrator import (  # noqa: PLC0415
                TrainingSessionOrchestrator,
            )
            from hermes.shell_server.training.in_session_factory import (  # noqa: PLC0415
                build_in_session_coordinator,
            )

            self._orchestrator = TrainingSessionOrchestrator()
            self._coordinator, self._audio_available = (
                build_in_session_coordinator(orchestrator=self._orchestrator)
            )
            self._capture_available = self._coordinator is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("_ensure_coordinator failed: %s", exc)
            self._capture_available = False
            self._audio_available = False

        return bool(self._capture_available)

    # ------------------------------------------------------------------
    # Periodic screenshot while capturing
    # ------------------------------------------------------------------

    def _start_screenshot_timer(self) -> bool:
        """Schedule periodic screen steps (every 3 s) while in recording state."""
        if self._screenshot_timer_id is not None:
            return False
        self._screenshot_timer_id = GLib.timeout_add(3000, self._take_screenshot_step)
        return False

    def _stop_screenshot_timer(self) -> None:
        if self._screenshot_timer_id is not None:
            GLib.source_remove(self._screenshot_timer_id)
            self._screenshot_timer_id = None

    def _take_screenshot_step(self) -> bool:
        if self._state != "recording" or not self._session_id or not self._coordinator:
            self._screenshot_timer_id = None
            return False

        session_id = self._session_id
        coordinator = self._coordinator

        def _worker() -> None:
            try:
                from hermes.agents_os.domain.surface_kind import SurfaceKind  # noqa: PLC0415

                coordinator.capture_screen_step(
                    session_id=session_id,
                    surface_kind=SurfaceKind.DESKTOP_APP,
                    action_payload={"kind": "periodic_screenshot"},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("periodic screenshot failed: %s", exc)

        threading.Thread(target=_worker, daemon=True).start()
        return True

    # ------------------------------------------------------------------
    # View update — maps state → button sensitivity/visibility
    # ------------------------------------------------------------------

    def _show_error(self, msg: str) -> bool:
        self._status_page.set_title("Error")
        self._status_page.set_description(msg)
        return False

    def _update_view(self) -> bool:
        title, desc = _STATE_COPY.get(self._state, (self._state, ""))
        self._status_page.set_title(title)
        self._status_page.set_description(desc)

        s = self._state
        has_session = self._session_id is not None

        # Legacy "Enseñar habilidad" button: only in standalone no-session mode.
        self._new_btn.set_visible(not has_session and s == "no_session")
        self._new_btn.set_sensitive(not has_session and s == "no_session")

        # "Abrir navegador" — idle state (session exists, browser not open yet).
        self._open_browser_btn.set_visible(s in ("idle", "browser_opening"))
        self._open_browser_btn.set_sensitive(s == "idle" and has_session)

        # "Iniciar grabación" — only after browser is open (change #4).
        self._start_btn.set_visible(s in ("browser_open", "needs_microphone"))
        self._start_btn.set_sensitive(
            s in ("browser_open", "needs_microphone") and has_session
        )

        # "Pausar" — only while actively recording.
        self._pause_btn.set_visible(s == "recording")
        self._pause_btn.set_sensitive(s == "recording")

        # "Reanudar" — only while paused.
        self._resume_btn.set_visible(s == "paused")
        self._resume_btn.set_sensitive(s == "paused")

        # "Finalizar grabación" — during recording or paused.
        self._finalize_btn.set_visible(s in ("recording", "paused"))
        self._finalize_btn.set_sensitive(s in ("recording", "paused"))

        # "Firmar y guardar" — review / finalized.
        self._sign_btn.set_visible(s in ("review", "finalized"))
        self._sign_btn.set_sensitive(s in ("review", "finalized") and has_session)

        # "Entrenar sin voz" — only when mic absent.
        self._without_voice_btn.set_visible(s == "needs_microphone")

        # "Abandonar" — when a session is active (not idle/no-session/terminal).
        active = s in (
            "idle",
            "browser_opening",
            "browser_open",
            "recording",
            "paused",
            "finalized",
            "review",
            "needs_microphone",
        )
        self._abandon_btn.set_visible(active)
        self._abandon_btn.set_sensitive(active and has_session)

        # Auto-reset terminal states so the panel is ready for a next skill.
        if s in ("signed", "abandoned"):
            self._session_id = None
            self._skill_name = ""
            self._description = ""
            self._site_url = ""
            self._state = "no_session"
            self._train_without_voice_ack = False
            # Remove the skill header if it was pre-filled (the session is done).
            if self._skill_header is not None:
                self._skill_header.set_visible(False)

        return False


# ---------------------------------------------------------------------------
# Legacy dialog — used only in standalone training tab (no pre-fill path)
# ---------------------------------------------------------------------------


class _NewSkillDialog(Adw.Window):
    def __init__(self, *, parent: Gtk.Window, on_save: Callable[..., None]) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Enseñar habilidad")
        self.set_default_size(480, 280)
        self._on_save = on_save

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        cancel = Gtk.Button.new_with_label("Cancelar")
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)

        save = Gtk.Button.new_with_label("Crear")
        save.add_css_class("hermes-primary")
        save.connect("clicked", lambda _b: self._save())
        header.pack_end(save)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.set_title("Nueva habilidad")

        self._name = Adw.EntryRow()
        self._name.set_title("Nombre")
        group.add(self._name)

        self._desc = Adw.EntryRow()
        self._desc.set_title("Descripción (opcional)")
        group.add(self._desc)

        page.add(group)
        toolbar.set_content(page)
        self.set_content(toolbar)

    def _save(self) -> None:
        name = self._name.get_text().strip()
        if not name:
            return
        self._on_save(
            skill_name=name,
            description=self._desc.get_text().strip(),
        )
        self.close()
