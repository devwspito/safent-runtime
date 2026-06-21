"""Lumen shell entrypoint — `python3 -m hermes.lumen`.

Launched by hermes-shell-session-wrapper as the single Wayland client under
mutter. Loads qml/Main.qml and exposes a Backend object to QML that bridges to
the agent backend (shell-server). Fails loud (exit 1) if QML can't load so the
session wrapper / GDM surfaces the error instead of a black screen.

Chat architecture (non-blocking):
  - POST /api/v1/chat is issued via QNetworkAccessManager (Qt async HTTP).
  - WS /ws/tasks/{task_id} is consumed by ChatWorker (QThread + stdlib socket).
  - Signals cross back to the QML thread via Qt's queued connection mechanism.
  - No blocking call ever runs on the GUI thread.

WS wire format (task_stream_socket_v1):
  Each WS message is a JSONL line with fields:
    kind             : "delta" | "thinking_delta" | "tool_call" | "status" | "done" | "error"
    task_id          : str (UUID)
    protocol_version : int (1)
    delta            : str  (kind=delta | thinking_delta)
    tool_call        : dict (kind=tool_call)
    status           : str  (kind=status)
    outcome          : str  (kind=done)
    error            : str  (kind=error | done with error)
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import socket
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QObject,
    Property,
    Signal,
    Slot,
    QThread,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtQml import QQmlApplicationEngine

_BACKEND_URL: str = os.environ.get("HERMES_SHELL_BACKEND_URL", "http://127.0.0.1:7517")
_HTTP_TIMEOUT_MS: int = 8_000   # POST /api/v1/chat timeout
_WS_RECV_TIMEOUT: float = 120.0  # seconds; keeps socket open during inference
# The task stream is served by the daemon over an AF_UNIX socket (NOT the
# shell-server HTTP port). Its peer-cred auth guard authorizes ONLY the operator
# uid (hermes-user) — exactly the uid Lumen runs as. The shell-server (uid hermes)
# is intentionally NOT authorized to read it, so we connect direct, not via proxy.
_TASKS_SOCK: str = os.environ.get("HERMES_TASKS_SOCK", "/run/hermes/tasks.sock")
# GATE 0 / M1 — control-plane OS-nativo: Lumen (operador uid 1000) habla D-Bus
# DIRECTO al daemon. Cero HTTP de negocio, cero operator_token (caller directo).
_DBUS_SERVICE: str = "org.hermes.Runtime"
_DBUS_PATH: str = "/org/hermes/Runtime"
_DBUS_IFACE: str = "org.hermes.Runtime1"

# Adaptive poll intervals (seconds).
# Fast-path: used for the first N settled cycles so state is established quickly.
# Settled-path: backed-off interval once state is stable to reduce idle wakeups.
_HEALTH_POLL_FAST_MS: int = 3_000     # healthz — fast (3 s) while not yet settled
_HEALTH_POLL_SETTLED_MS: int = 30_000 # healthz — settled (30 s, 10× reduction)
_HEALTH_SETTLE_CYCLES: int = 3        # consecutive unchanged results before backing off

_PROVIDER_POLL_FAST_MS: int = 5_000    # providers/active — fast
_PROVIDER_POLL_SETTLED_MS: int = 30_000  # providers/active — settled

_CLOCK_POLL_MS: int = 10_000           # clock — unchanged; only emits on minute-change

# Lumen UI preferences — persisted across reboots in XDG_STATE_HOME.
# Defaults to ~/.local/state/hermes/lumen/ui-prefs.json.
# hermes-user's HOME is writable (session user, no ProtectHome restriction).
_LUMEN_PREFS_DIR: Path = Path(
    os.environ.get("LUMEN_PREFS_DIR") or (
        Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
        / "hermes" / "lumen"
    )
)
_LUMEN_PREFS_FILE: Path = _LUMEN_PREFS_DIR / "ui-prefs.json"

# Maximum length for a setting key or value — prevents log-injection / DoS.
_SETTING_MAX_LEN: int = 2048


# ---------------------------------------------------------------------------
# Raw stdlib WebSocket client (zero extra deps, works in PySide6-Essentials)
# ---------------------------------------------------------------------------

def _ws_handshake_key() -> tuple[str, str]:
    """Return (Sec-WebSocket-Key, expected Sec-WebSocket-Accept)."""
    raw_key = base64.b64encode(os.urandom(16)).decode("ascii")
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = base64.b64encode(
        hashlib.sha1((raw_key + magic).encode("ascii")).digest()
    ).decode("ascii")
    return raw_key, accept


def _send_ws_frame(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    """Send a single unmasked WebSocket frame (server→client direction unused here)."""
    length = len(payload)
    # Client frames must be masked (RFC 6455 §5.3).
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", length))
    header.extend(mask)
    sock.sendall(bytes(header) + masked)


def _recv_ws_message(sock: socket.socket) -> bytes | None:
    """Receive one complete WebSocket message (handles fragmentation / control frames).

    Returns the message payload, or None on close/error.
    Opcode 0x8 (close) returns None. Control frames (ping/pong) are silently handled.
    """
    accumulated = bytearray()
    while True:
        # Read 2-byte frame header.
        header = _recv_exact(sock, 2)
        if header is None:
            return None
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        payload_len = header[1] & 0x7F

        if payload_len == 126:
            ext = _recv_exact(sock, 2)
            if ext is None:
                return None
            payload_len = struct.unpack(">H", ext)[0]
        elif payload_len == 127:
            ext = _recv_exact(sock, 8)
            if ext is None:
                return None
            payload_len = struct.unpack(">Q", ext)[0]

        mask_bytes = b""
        if masked:
            mask_bytes = _recv_exact(sock, 4) or b""

        payload = _recv_exact(sock, payload_len) or b""
        if masked:
            payload = bytes(b ^ mask_bytes[i % 4] for i, b in enumerate(payload))

        # Handle control frames (close=0x8, ping=0x9, pong=0xA).
        if opcode == 0x8:
            return None
        if opcode in (0x9, 0xA):
            # Ignore ping/pong; continue reading.
            continue

        accumulated.extend(payload)
        if fin:
            return bytes(accumulated)
        # Continuation frame (FIN=0) — keep accumulating.


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """Read exactly n bytes from sock. Returns None on EOF/error."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _open_ws_connection(
    base_url: str,
    path: str,
) -> socket.socket | None:
    """Open a WebSocket connection to base_url+path.

    Performs the HTTP Upgrade handshake over a raw TCP socket.
    Returns the connected socket or None on failure.
    """
    # Parse host:port from base_url (http://host:port or http://host).
    url = base_url.rstrip("/")
    if url.startswith("http://"):
        url = url[7:]
    elif url.startswith("https://"):
        url = url[8:]
    host, _, port_str = url.partition(":")
    port = int(port_str) if port_str else 80

    key, expected_accept = _ws_handshake_key()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")

    try:
        # Prefer the daemon's AF_UNIX stream socket (peer-cred authorized for the
        # operator uid). Fall back to TCP only if the socket is absent (dev/test).
        if _TASKS_SOCK and os.path.exists(_TASKS_SOCK):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect(_TASKS_SOCK)
        else:
            sock = socket.create_connection((host, port), timeout=10.0)
        sock.settimeout(_WS_RECV_TIMEOUT)
        sock.sendall(handshake)
        # Read response headers (until \r\n\r\n).
        response_buf = bytearray()
        while b"\r\n\r\n" not in response_buf:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                return None
            response_buf.extend(chunk)
        first_line = response_buf.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if "101" not in first_line:
            sock.close()
            return None
        return sock
    except OSError:
        return None


# ---------------------------------------------------------------------------
# ChatWorker — lives in a QThread, reads the WS stream, emits Qt signals
# ---------------------------------------------------------------------------

class ChatWorker(QObject):
    """Consumes a WS task stream and emits typed Qt signals.

    Runs in its own QThread; signals are delivered to the GUI thread via
    Qt's queued connection mechanism (automatic when objects live in different
    threads).
    """

    chunkReceived = Signal(str, str)    # (conversationId, delta)
    toolEvent = Signal(str, str)        # (conversationId, jsonEvent)
    done = Signal(str)                  # (conversationId)
    error = Signal(str, str)            # (conversationId, message)

    def __init__(
        self,
        *,
        base_url: str,
        stream_path: str,
        conversation_id: str,
    ) -> None:
        super().__init__()
        self._base_url = base_url
        self._stream_path = stream_path
        self._conversation_id = conversation_id

    @Slot()
    def run(self) -> None:
        """Entry point called by the owning QThread.started signal."""
        sock = _open_ws_connection(self._base_url, self._stream_path)
        if sock is None:
            self.error.emit(
                self._conversation_id,
                "No se pudo conectar al stream del agente. "
                "Verifica que hermes-runtime está activo.",
            )
            self.done.emit(self._conversation_id)
            return

        try:
            self._drain(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _drain(self, sock: socket.socket) -> None:
        """Read frames until done/error/close."""
        while True:
            msg = _recv_ws_message(sock)
            if msg is None:
                # Connection closed by server.
                self.done.emit(self._conversation_id)
                return

            try:
                frame = json.loads(msg.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                continue

            kind = frame.get("kind", "")

            if kind == "delta":
                delta = frame.get("delta", "")
                if delta:
                    self.chunkReceived.emit(self._conversation_id, delta)

            elif kind == "thinking_delta":
                # Silently drop thinking deltas — they are internal reasoning,
                # not part of the user-facing response per the stream protocol.
                pass

            elif kind == "tool_call":
                tool_call = frame.get("tool_call", {})
                self.toolEvent.emit(
                    self._conversation_id, json.dumps(tool_call)
                )

            elif kind == "status":
                # Lifecycle frame — no user-visible action needed.
                pass

            elif kind == "done":
                outcome = frame.get("outcome", "completed")
                err = frame.get("error")
                if err and outcome != "completed":
                    self.error.emit(
                        self._conversation_id,
                        f"El agente terminó con error: {err}",
                    )
                self.done.emit(self._conversation_id)
                return

            elif kind == "error":
                err_msg = frame.get("error", "Error desconocido del agente.")
                self.error.emit(self._conversation_id, err_msg)
                self.done.emit(self._conversation_id)
                return


# ---------------------------------------------------------------------------
# UI preferences helpers (module-level, no Qt deps — testable standalone)
# ---------------------------------------------------------------------------


def _load_ui_prefs() -> dict[str, str]:
    """Load UI preferences from the JSON store. Returns {} on any read error.

    Never raises: a corrupt or absent prefs file must not crash Lumen at startup.
    """
    try:
        raw = _LUMEN_PREFS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return {}


def _persist_ui_prefs(prefs: dict[str, str]) -> None:
    """Write UI preferences to the JSON store atomically via a temp file.

    Atomic: write to <file>.tmp then rename — prevents partial writes from
    corrupting the prefs on power loss. Never raises: best-effort persistence.
    """
    try:
        _LUMEN_PREFS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _LUMEN_PREFS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_LUMEN_PREFS_FILE)
    except OSError:
        pass  # Non-fatal: preference will be lost on reboot but UI remains stable.


# ---------------------------------------------------------------------------
# Backend — QObject exposed to QML
# ---------------------------------------------------------------------------

_ACCOUNT_SENTINEL = Path(
    os.environ.get("HERMES_ACCOUNT_SENTINEL", "/var/lib/hermes/account-applied")
)


class Backend(QObject):
    """Bridge exposed to QML as `backend`.

    Signals emitted to QML:
      agentChunk(conversationId, delta)     — streaming text delta
      agentToolEvent(conversationId, json)  — tool call event (JSON string)
      agentDone(conversationId)             — stream finished
      agentError(conversationId, message)   — error (agent unavailable, etc.)

      providersChanged(jsonList)            — full provider list as JSON string
      activeProviderChanged()               — active provider state changed

      needsOnboardingChanged()              — first-run gate flipped
      accountCreated(success, errorMessage) — result of createAccount slot
    """

    # ── existing signals (clock, connected) ─────────────────────────────
    connectedChanged = Signal()
    clockChanged = Signal()

    # ── chat signals ─────────────────────────────────────────────────────
    agentChunk = Signal(str, str)       # (conversationId, delta)
    agentToolEvent = Signal(str, str)   # (conversationId, jsonEvent)
    agentDone = Signal(str)             # (conversationId)
    agentError = Signal(str, str)       # (conversationId, message)

    # ── provider signals ─────────────────────────────────────────────────
    providersChanged = Signal(str)           # JSON array of provider objects
    activeProviderChanged = Signal()         # fires when hasActiveModel changes
    providerTestResult = Signal(str, bool, str)  # (providerId, ok, errorMsg)

    # ── onboarding signals ───────────────────────────────────────────────
    needsOnboardingChanged = Signal()        # first-run gate flipped
    accountCreated = Signal(bool, str)       # (success, errorMessage)
    finalizeOnboardingDone = Signal(bool, bool)  # (success, partial)

    # ── datos REALES del SO (cero mock) ──────────────────────────────────
    # Cada vista pide su lista por D-Bus al daemon y la recibe aquí.
    listLoaded = Signal(str, str)            # (key, jsonArray)
    filesLoaded = Signal(str, str)           # (path, jsonArray)
    shellOutput = Signal(str, str, str)      # (cmd, output, cwd) — terminal REAL

    def __init__(self) -> None:
        super().__init__()
        self._connected = False
        self._clock = ""
        self._has_active_model = False
        import os as _os
        self._shell_cwd = _os.path.expanduser("~")
        self._providers_json: str = "[]"
        # needsOnboarding: true only when the account sentinel is absent.
        # Provider presence is NOT a gate for the desktop (spec 011 US4 — deferred
        # model). Computed in _recompute_needs_onboarding(); cached to avoid
        # redundant signals.
        self._needs_onboarding: bool = not _ACCOUNT_SENTINEL.exists()

        # Active chat workers: conversationId -> (QThread, ChatWorker).
        # Kept alive until done/error so Qt doesn't GC the thread mid-run.
        self._active: dict[str, tuple[QThread, ChatWorker]] = {}

        # ── Onboarding finalize state ────────────────────────────────────
        # Accumulates partial failures across the setX() → finalizeOnboarding()
        # chain. Reset each time finalizeOnboarding() is called.
        self._finalize_partial: bool = False

        # Locale chosen in the language step — stored so step 3 can re-apply it.
        self._current_locale: str = ""

        # ── UI preferences (setSetting/getSetting) ────────────────────────
        self._ui_prefs: dict[str, str] = _load_ui_prefs()

        # QNetworkAccessManager for async HTTP POST.
        self._nam = QNetworkAccessManager(self)

        # Adaptive back-off counters: track how many consecutive cycles the
        # observed state has been unchanged.  Once a poller reaches
        # _HEALTH_SETTLE_CYCLES stable results it slows to the settled interval
        # so the process stops waking the ARM CPU at 3-5 s on idle.
        self._health_stable_cycles: int = 0
        self._provider_stable_cycles: int = 0

        # Onboarding is checked once at startup and whenever providers/health
        # change.  Stop polling it independently once configured.
        self._onboarding_settled: bool = False

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh)
        self._poll.start(_HEALTH_POLL_FAST_MS)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_clock)
        self._tick.start(_CLOCK_POLL_MS)

        # Poll active provider (fast initially, backs off once settled).
        self._provider_poll = QTimer(self)
        self._provider_poll.timeout.connect(self._refresh_active_provider)
        self._provider_poll.start(_PROVIDER_POLL_FAST_MS)

        self._refresh()
        self._update_clock()
        self._refresh_active_provider()

    # ------------------------------------------------------------------
    # Onboarding: needsOnboarding property + createAccount slot
    # ------------------------------------------------------------------

    def _recompute_needs_onboarding(self) -> None:
        """Recompute and emit needsOnboardingChanged when the gate flips.

        Spec 011 US4 (deferred model): only the account sentinel gates the
        desktop. Provider absence is surfaced via hasActiveModel / the
        deferred-model banner — it must never re-open the onboarding overlay.
        """
        new_val = not _ACCOUNT_SENTINEL.exists()
        if new_val != self._needs_onboarding:
            self._needs_onboarding = new_val
            self.needsOnboardingChanged.emit()

    @Property(bool, notify=needsOnboardingChanged)
    def needsOnboarding(self) -> bool:
        return self._needs_onboarding

    @Slot(str, str)
    def createAccount(self, username: str, password: str) -> None:
        """StageAccount (D-Bus) — GATE 0 / M7, cero HTTP.

        El daemon deja staged las credenciales; el path-unit root las aplica.
        Emits accountCreated(True, "") cuando staged=True.
        Emits accountCreated(False, errorMessage) en cualquier fallo.
        La contraseña no se almacena; va directa al verbo D-Bus.
        """
        self._dbus_call(
            "StageAccount",
            (username, password),
            self._on_stage_account_json,
            lambda err: self.accountCreated.emit(
                False,
                "No se pudo contactar con el agente. Verifica que el runtime está activo.",
            ),
        )

    def _on_stage_account_json(self, raw) -> None:
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError, TypeError):
            data = {}
        if data.get("staged"):
            # El sentinel lo escribe el servicio root tras aplicar la cuenta.
            # Recalculamos el estado de onboarding para que el wizard avance.
            self._recompute_needs_onboarding()
            self.accountCreated.emit(True, "")
            return
        self.accountCreated.emit(False, self._friendly_account_error(data.get("error", "")))

    @staticmethod
    def _friendly_account_error(code: str) -> str:
        if code == "already_configured":
            return "La cuenta ya está configurada. Continúa al siguiente paso."
        if code == "invalid_username":
            return (
                "Nombre de usuario no válido. Usa solo letras minúsculas, "
                "números, guiones o guiones bajos (máx. 32 caracteres)."
            )
        if code == "invalid_password":
            return "Contraseña no válida. No se permiten caracteres de control."
        return "No se pudo crear el usuario. Inténtalo de nuevo."

    # ------------------------------------------------------------------
    # currentLocale property — read by OnboardingView step 3
    # ------------------------------------------------------------------

    @Property(str)
    def currentLocale(self) -> str:
        return self._current_locale

    # ------------------------------------------------------------------
    # setLocale — OS-native locale application via gsettings (session).
    # The D-Bus daemon has no setLocale verb; we apply it locally in the
    # user session (gsettings is the correct surface for a Wayland session).
    # Idempotent: calling twice with the same locale is a no-op.
    # ------------------------------------------------------------------

    @Slot(str)
    def setLocale(self, locale: str) -> None:
        """Apply the UI locale in the operator session (gsettings) and cache it.

        gsettings writes to the user's dconf database — the correct OS-native
        mechanism for per-session locale under GNOME/Wayland. Does not require
        root or D-Bus daemon involvement.

        Pending daemon verb: none (best-effort; locale is session-only for now).
        """
        import logging as _log  # noqa: PLC0415
        _logger = _log.getLogger("hermes.lumen.backend")

        locale = (locale or "").strip()
        if not locale:
            return
        self._current_locale = locale
        self._apply_gsettings_locale(locale)
        _logger.info("lumen.setLocale locale=%s", locale)

    def _apply_gsettings_locale(self, locale: str) -> None:
        """Apply locale via gsettings in a daemon thread — never blocks the GUI."""
        import subprocess  # noqa: PLC0415
        import threading  # noqa: PLC0415

        def _run() -> None:
            try:
                subprocess.run(  # noqa: S603
                    ["gsettings", "set", "org.gnome.system.locale", "region", locale],
                    timeout=5, capture_output=True, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass  # Non-fatal: locale preference stored in _current_locale

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Finalize chain — setProfile / setNetwork / setTenant / setConsents /
    # reviewServices / finalizeOnboarding.
    #
    # None of these verbs exist on the daemon's D-Bus interface today.
    # Each setter stores its value in-process (best-effort); finalizeOnboarding
    # writes the account sentinel (which the existing createAccount path already
    # creates via StageAccount). If the sentinel already exists the call is a
    # no-op. Emits finalizeOnboardingDone(success, partial) when done.
    #
    # Pending daemon verbs: StageProfile, SetNetwork, SetTenant, SetConsents,
    # ReviewServices — not yet exposed on org.hermes.Runtime1. When the daemon
    # adds them, route the calls from the respective setter below.
    # ------------------------------------------------------------------

    @Slot(str)
    def setProfile(self, kind: str) -> None:
        """Store the system profile kind for finalization (best-effort, no daemon verb yet)."""
        import logging as _log  # noqa: PLC0415
        _log.getLogger("hermes.lumen.backend").info(
            "lumen.setProfile kind=%s (best-effort, no daemon verb)", kind
        )

    @Slot(str)
    def setNetwork(self, state: str) -> None:
        """Store the network state for finalization (best-effort, no daemon verb yet)."""
        import logging as _log  # noqa: PLC0415
        _log.getLogger("hermes.lumen.backend").info(
            "lumen.setNetwork state=%s (best-effort, no daemon verb)", state
        )

    @Slot(str)
    def setTenant(self, mode: str) -> None:
        """Store the tenant mode for finalization (best-effort, no daemon verb yet)."""
        import logging as _log  # noqa: PLC0415
        _log.getLogger("hermes.lumen.backend").info(
            "lumen.setTenant mode=%s (best-effort, no daemon verb)", mode
        )

    @Slot("QVariantList")
    def setConsents(self, items: list) -> None:
        """Store the initial consent list for finalization (best-effort, no daemon verb yet)."""
        import logging as _log  # noqa: PLC0415
        _log.getLogger("hermes.lumen.backend").info(
            "lumen.setConsents count=%d (best-effort, no daemon verb)", len(items)
        )

    @Slot(bool)
    def reviewServices(self, ack: bool) -> None:
        """Acknowledge service review (best-effort, no daemon verb yet)."""
        import logging as _log  # noqa: PLC0415
        _log.getLogger("hermes.lumen.backend").info(
            "lumen.reviewServices ack=%s (best-effort, no daemon verb)", ack
        )

    @Slot()
    def finalizeOnboarding(self) -> None:
        """Complete the onboarding flow and emit finalizeOnboardingDone(success, partial).

        The real work (account creation) was done by createAccount/StageAccount.
        This slot checks the sentinel and emits the completion signal so the
        QML step-3 state machine can advance to the desktop CTA.

        partial=True when the sentinel is absent (account step may have been
        skipped or failed silently) — the UI shows a soft note rather than blocking.
        """
        import logging as _log  # noqa: PLC0415
        _log.getLogger("hermes.lumen.backend").info("lumen.finalizeOnboarding")
        self._finalize_partial = False
        self._do_finalize()

    def _do_finalize(self) -> None:
        sentinel_exists = _ACCOUNT_SENTINEL.exists()
        if not sentinel_exists:
            # Account not yet applied — partial success; don't block the user.
            self._finalize_partial = True
        self.finalizeOnboardingDone.emit(True, self._finalize_partial)

    # ------------------------------------------------------------------
    # stopGeneration — cancel the active WS stream for a conversation.
    # ------------------------------------------------------------------

    @Slot(str)
    def stopGeneration(self, conversation_id: str) -> None:
        """Cancel the active ChatWorker stream for conversation_id.

        Quits the worker thread so no further chunks are delivered to QML.
        If no stream is active for the conversation, this is a no-op.
        The worker's done() signal fires after quit; cleanup runs via the
        existing lambda connected in _start_ws_worker.
        """
        import logging as _log  # noqa: PLC0415
        entry = self._active.get(conversation_id)
        if entry is None:
            return
        thread, _worker = entry
        _log.getLogger("hermes.lumen.backend").info(
            "lumen.stopGeneration conv=%s", conversation_id
        )
        thread.quit()
        # Cleanup is handled by the done() lambda in _start_ws_worker when
        # the thread actually stops. We do not wait here to avoid blocking GUI.

    # ------------------------------------------------------------------
    # setSetting / getSetting — lightweight UI preference store.
    # Persists to _LUMEN_PREFS_FILE (JSON, operator's writable home).
    # Keys and values are capped at _SETTING_MAX_LEN to prevent log injection.
    # ------------------------------------------------------------------

    @Slot(str, str)
    def setSetting(self, key: str, value: str) -> None:
        """Persist a UI preference key→value pair to the local JSON store.

        Changes are written synchronously (file is small, < 4 KiB in practice).
        Invalid inputs (oversized key/value) are silently dropped — UI prefs
        must never crash the shell.
        """
        import logging as _log  # noqa: PLC0415
        if not key or len(key) > _SETTING_MAX_LEN or len(value) > _SETTING_MAX_LEN:
            return
        self._ui_prefs[key] = value
        _persist_ui_prefs(self._ui_prefs)
        _log.getLogger("hermes.lumen.backend").debug(
            "lumen.setSetting key=%s", key
        )

    @Slot(str, result=str)
    def getSetting(self, key: str) -> str:
        """Return a persisted UI preference value, or "" if not set."""
        if not key or len(key) > _SETTING_MAX_LEN:
            return ""
        return self._ui_prefs.get(key, "")

    # ------------------------------------------------------------------
    # Existing properties
    # ------------------------------------------------------------------

    @Property(bool, notify=activeProviderChanged)
    def hasActiveModel(self) -> bool:
        return self._has_active_model

    def _refresh(self) -> None:
        """Liveness por D-Bus (GATE 0: cero HTTP). Si el daemon responde a una
        lectura barata (GetActiveProvider, sin authZ), está vivo."""
        self._dbus_call(
            "GetActiveProvider", (),
            lambda _raw: self._on_health(True),
            lambda _err: self._on_health(False),
        )

    def _on_health(self, ok: bool) -> None:
        state_changed = ok != self._connected
        if state_changed:
            self._connected = ok
            self._health_stable_cycles = 0
            self.connectedChanged.emit()
            # Reset to fast polling so any change is detected promptly.
            self._poll.setInterval(_HEALTH_POLL_FAST_MS)
        else:
            self._health_stable_cycles += 1
            if self._health_stable_cycles >= _HEALTH_SETTLE_CYCLES:
                # State is stable — back off to reduce idle wakeups.
                self._poll.setInterval(_HEALTH_POLL_SETTLED_MS)

    def _update_clock(self) -> None:
        now = datetime.datetime.now().strftime("%H:%M")
        if now != self._clock:
            self._clock = now
            self.clockChanged.emit()

    @Property(bool, notify=connectedChanged)
    def connected(self) -> bool:
        return self._connected

    @Property(str, notify=clockChanged)
    def clock(self) -> str:
        return self._clock

    # ------------------------------------------------------------------
    # Provider API — D-Bus DIRECTO al daemon (GATE 0 / M1: SO-nativo, sin HTTP).
    # El operador (uid 1000) es caller DIRECTO de org.hermes.Runtime1 → cero
    # token, cero shell-server. Async (QDBusPendingCallWatcher) para no bloquear
    # la UI: TestProvider valida por el runtime Nous real (segundos).
    # ------------------------------------------------------------------

    def _dbus_call(self, member, args, on_reply, on_error=None, *, multi=False) -> None:
        from PySide6.QtDBus import (  # noqa: PLC0415
            QDBusConnection,
            QDBusMessage,
            QDBusPendingCallWatcher,
        )

        msg = QDBusMessage.createMethodCall(
            _DBUS_SERVICE, _DBUS_PATH, _DBUS_IFACE, member
        )
        if args:
            msg.setArguments(list(args))
        pending = QDBusConnection.systemBus().asyncCall(msg)
        watcher = QDBusPendingCallWatcher(pending, self)

        def _finished(w) -> None:
            reply = w.reply()
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                if on_error is not None:
                    on_error(reply.errorMessage() or "")
            else:
                a = reply.arguments()
                # multi=True: pasa la lista completa de retornos (p.ej. Enqueue → ss).
                on_reply(list(a) if multi else (a[0] if a else None))
            w.deleteLater()

        watcher.finished.connect(_finished)

    def _refresh_active_provider(self) -> None:
        """GetActiveProvider (D-Bus) — ¿hay modelo activo? {} = no."""
        self._dbus_call(
            "GetActiveProvider", (),
            self._on_active_provider_json,
            self._on_active_provider_unreachable,
        )

    def _on_active_provider_unreachable(self, _err: str) -> None:
        # Daemon inalcanzable: tratar como sin modelo activo.
        if self._has_active_model:
            self._has_active_model = False
            self._provider_stable_cycles = 0
            self._provider_poll.setInterval(_PROVIDER_POLL_FAST_MS)
            self.activeProviderChanged.emit()
        self._recompute_needs_onboarding()

    def _on_active_provider_json(self, raw) -> None:
        try:
            data = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError, TypeError):
            data = None
        new_state = isinstance(data, dict) and bool(data)  # {} = sin activo
        state_changed = new_state != self._has_active_model
        if state_changed:
            self._has_active_model = new_state
            self._provider_stable_cycles = 0
            self._provider_poll.setInterval(_PROVIDER_POLL_FAST_MS)
            self.activeProviderChanged.emit()
        else:
            self._provider_stable_cycles += 1
            if self._provider_stable_cycles >= _HEALTH_SETTLE_CYCLES:
                self._provider_poll.setInterval(_PROVIDER_POLL_SETTLED_MS)
        self._recompute_needs_onboarding()
        if not self._onboarding_settled and not self._needs_onboarding:
            self._onboarding_settled = True

    @Slot()
    def listProviders(self) -> None:
        """ListProviders (D-Bus) — emits providersChanged(jsonArray)."""
        self._dbus_call(
            "ListProviders", (),
            self._on_list_providers_json,
            lambda _e: self.providersChanged.emit("[]"),
        )

    def _on_list_providers_json(self, raw) -> None:
        try:
            json.loads(raw)  # validate
            self._providers_json = raw
        except (json.JSONDecodeError, ValueError, TypeError):
            self._providers_json = "[]"
        self.providersChanged.emit(self._providers_json)

    @Slot(str, str, str, str)
    def addProvider(
        self,
        provider_kind: str,
        alias: str,
        default_model: str,
        api_key: str,
    ) -> None:
        """POST /api/v1/providers — creates a provider and activates it.

        Args:
            provider_kind: ProviderKind value string (e.g. "anthropic").
            alias:         Human-readable label chosen by the user.
            default_model: Default model string for this provider.
            api_key:       Secret key (sent once; stored encrypted server-side).
        """
        draft = json.dumps(
            {
                "kind": provider_kind,
                "alias": alias,
                "default_model": default_model,
                "api_key": api_key if api_key else None,
                # NO auto-activar al crear: la activación la posee la secuencia
                # explícita add→test→activate (activar aquí adelantaría el wizard
                # antes de validar la clave).
                "set_active": False,
            }
        )
        self._dbus_call(
            "AddProvider", (draft,),
            self._on_add_provider_ok,
            self._on_add_provider_err,
        )

    def _on_add_provider_ok(self, _raw) -> None:
        # Provider creado (no activado). Refrescar la lista → el watcher QML lo
        # encuentra por kind y lanza el test.
        self.listProviders()

    def _on_add_provider_err(self, err: str) -> None:
        # Surface el fallo para que el wizard salga de "testing".
        self.providerTestResult.emit(
            "", False, err or "No se pudo guardar el proveedor."
        )

    @Slot(str)
    def activateProvider(self, provider_id: str) -> None:
        """SetActiveProvider (D-Bus)."""
        self._dbus_call(
            "SetActiveProvider", (provider_id,),
            self._on_activate_ok,
            self._on_activate_err,
        )

    def _on_activate_ok(self, _raw) -> None:
        self._refresh_active_provider()
        self.listProviders()

    def _on_activate_err(self, err: str) -> None:
        self.providerTestResult.emit(
            "", False, err or "No se pudo activar el modelo."
        )

    @Slot(str)
    def testProvider(self, provider_id: str) -> None:
        """TestProvider (D-Bus) — valida por el runtime Nous REAL en el daemon."""
        self._dbus_call(
            "TestProvider", (provider_id,),
            lambda raw: self._on_test_json(provider_id, raw, None),
            lambda err: self._on_test_json(provider_id, None, err),
        )

    def _on_test_json(self, provider_id: str, raw, err) -> None:
        ok = False
        error_msg = err or ""
        if raw is not None:
            try:
                data = json.loads(raw)
                ok = bool(data.get("ok", False))
                error_msg = data.get("error") or ""
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        # Refresca la lista para que la UI muestre la conectividad actualizada.
        self.listProviders()
        self.providerTestResult.emit(provider_id, ok, error_msg)

    # ------------------------------------------------------------------
    # Chat API
    # ------------------------------------------------------------------

    @Slot(str, str)
    def send(self, conversation_id: str, text: str) -> None:
        """Encola el mensaje vía D-Bus Enqueue (daemon) y abre el stream AF_UNIX.

        GATE 0 / M2 — cero HTTP: el operador (uid ∈ authorized_uids) llama
        directo al daemon. conversation_id SIEMPRE viene no vacío del QML
        (_newConvId). operator_token="" porque somos llamada directa, no proxy.
        El daemon persiste el mensaje del usuario y devuelve (task_id, stream_path).

        Args:
            conversation_id: UUID string (no vacío — lo genera el QML).
            text:            The user's message.
        """
        # CTRL-P1-27: idempotencia doble-envío (mismo dedup_key = 1 ejecución).
        dedup_key = f"chat:{conversation_id}:{hash(text)}"
        self._dbus_call(
            "Enqueue",
            ("chat_message", text, 0, dedup_key, conversation_id, ""),
            lambda rv: self._on_enqueue_reply(conversation_id, rv),
            lambda err: self._on_enqueue_error(conversation_id, err),
            multi=True,
        )

    def _on_enqueue_reply(self, conversation_id: str, rv) -> None:
        """rv = [task_id, stream_path] del verbo Enqueue (D-Bus)."""
        task_id = rv[0] if rv and len(rv) > 0 else ""
        stream_path = rv[1] if rv and len(rv) > 1 else ""
        if not task_id or not stream_path:
            self.agentError.emit(
                conversation_id,
                "El agente no devolvió una ruta de stream válida.",
            )
            self.agentDone.emit(conversation_id)
            return
        self._start_ws_worker(conversation_id, stream_path)

    def _on_enqueue_error(self, conversation_id: str, err: str) -> None:
        # El daemon 503/deniega → mensaje legible; nunca silencioso (T051).
        msg = err or "El agente no está disponible. Comprueba que el runtime está activo."
        if "AgentUnavailable" in msg or "unavailable" in msg.lower():
            msg = "El agente no está disponible. Comprueba que el runtime está activo."
        self.agentError.emit(conversation_id, msg)
        self.agentDone.emit(conversation_id)

    def _start_ws_worker(self, conversation_id: str, stream_path: str) -> None:
        """Spin up a ChatWorker in a dedicated QThread."""
        # Clean up any stale worker for the same conversation.
        self._cleanup_worker(conversation_id)

        thread = QThread(self)
        worker = ChatWorker(
            base_url=_BACKEND_URL,
            stream_path=stream_path,
            conversation_id=conversation_id,
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.chunkReceived.connect(self.agentChunk)
        worker.toolEvent.connect(self.agentToolEvent)
        worker.done.connect(self.agentDone)
        worker.error.connect(self.agentError)
        worker.done.connect(lambda _: self._cleanup_worker(conversation_id))

        self._active[conversation_id] = (thread, worker)
        thread.start()

    def _cleanup_worker(self, conversation_id: str) -> None:
        """Quit and schedule deletion of a finished worker+thread pair."""
        entry = self._active.pop(conversation_id, None)
        if entry is None:
            return
        thread, _ = entry
        thread.quit()
        thread.wait(3000)

    @staticmethod
    def _friendly_http_error(status: int | None, raw: bytes) -> str:
        """Translate HTTP error status to a user-facing Spanish message."""
        if status == 503:
            return (
                "El agente no está disponible. "
                "Comprueba que hermes-runtime está activo."
            )
        if status == 429:
            return "Demasiadas peticiones. Espera un momento e inténtalo de nuevo."
        if status is None:
            return (
                "No se pudo contactar con el servidor. "
                "Verifica tu conexión."
            )
        try:
            detail = json.loads(raw).get("detail", {})
            if isinstance(detail, dict):
                return detail.get("message", f"Error del servidor ({status}).")
            return str(detail) or f"Error del servidor ({status})."
        except Exception:
            return f"Error del servidor ({status})."

    @Slot(str)
    def loadConversation(self, conv_id: str) -> None:
        """GetConversation (D-Bus) — historial de una conversación (read-only).

        GATE 0 / M2 — cero HTTP. El resultado aún no se renderiza en QML
        (historial = follow-up); el slot existe para que QML lo llame y el
        backend lo cachee/loguee. El daemon es dueño del store.
        """
        self._dbus_call(
            "GetConversation", (conv_id,),
            lambda _raw: None,
            lambda _err: None,
        )

    # ------------------------------------------------------------------
    # Datos REALES del SO por D-Bus (cero mock). Cada vista pide su lista.
    # ------------------------------------------------------------------

    # key -> (D-Bus member, takes_limit)
    _LIST_VERBS = {
        "recent_tasks": ("ListRecentTasks", True),
        "configured_tasks": ("ListConfiguredTasks", True),
        "pending": ("ListPending", True),
        "skills": ("ListSkills", False),
        "agents": ("ListAgents", False),
    }

    @Slot(str)
    @Slot(str, int)
    def loadList(self, key: str, limit: int = 50) -> None:
        """Carga una lista REAL del daemon por D-Bus → emite listLoaded(key, json)."""
        spec = self._LIST_VERBS.get(key)
        if spec is None:
            self.listLoaded.emit(key, "[]")
            return
        member, takes_limit = spec
        args = (limit,) if takes_limit else ()
        self._dbus_call(
            member, args,
            lambda raw: self.listLoaded.emit(key, raw if raw else "[]"),
            lambda _err: self.listLoaded.emit(key, "[]"),
        )

    @Slot(str)
    def loadFiles(self, path: str) -> None:
        """Lista REAL del filesystem del operador. Sin path → su HOME.

        Lumen corre como el operador (uid 1000); lee sus propios archivos
        directamente (no es backend remoto, es SU equipo). No sigue symlinks
        fuera del home para evitar fugas; oculta dotfiles.
        """
        import os as _os  # noqa: PLC0415

        base = path or _os.path.expanduser("~")
        out: list[dict] = []
        try:
            base = _os.path.realpath(base)
            with _os.scandir(base) as it:
                for e in it:
                    name = e.name
                    if name.startswith("."):
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                        out.append({
                            "name": name,
                            "path": _os.path.join(base, name),
                            "is_dir": e.is_dir(follow_symlinks=False),
                            "size": int(st.st_size),
                            "mtime": int(st.st_mtime),
                        })
                    except OSError:
                        continue
            out.sort(key=lambda d: (not d["is_dir"], d["name"].lower()))
        except OSError:
            out = []
        self.filesLoaded.emit(base, json.dumps(out))

    @Slot(str)
    def runShell(self, cmd: str) -> None:
        """Terminal REAL: ejecuta el comando como el operador y emite la salida.

        Corre en su propio equipo (uid 1000), su propio shell. `cd` se gestiona
        en proceso (el cwd de subprocess no persiste). Ejecución en un hilo para
        no congelar la UI; la señal se entrega al hilo de Qt (queued).
        """
        import os as _os  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        import threading  # noqa: PLC0415

        cmd = (cmd or "").strip()
        if not cmd:
            self.shellOutput.emit("", "", self._shell_cwd)
            return

        # `cd` interno (persistente).
        if cmd == "cd" or cmd.startswith("cd "):
            target = cmd[2:].strip() or _os.path.expanduser("~")
            target = _os.path.expanduser(target)
            if not _os.path.isabs(target):
                target = _os.path.join(self._shell_cwd, target)
            target = _os.path.realpath(target)
            if _os.path.isdir(target):
                self._shell_cwd = target
                self.shellOutput.emit(cmd, "", self._shell_cwd)
            else:
                self.shellOutput.emit(cmd, f"cd: no existe el directorio: {target}", self._shell_cwd)
            return
        if cmd == "clear":
            self.shellOutput.emit("__clear__", "", self._shell_cwd)
            return

        cwd = self._shell_cwd

        def _run() -> None:
            try:
                r = subprocess.run(  # noqa: S603
                    ["/bin/bash", "-lc", cmd],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                output = (r.stdout or "") + (r.stderr or "")
            except subprocess.TimeoutExpired:
                output = "(el comando superó 20 s y se canceló)"
            except Exception as exc:  # noqa: BLE001
                output = str(exc)
            self.shellOutput.emit(cmd, output.rstrip("\n"), cwd)

        threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    # Inject --gpu-effects into argv when HERMES_GPU_EFFECTS=1 so Theme.qml
    # can detect it via Qt.application.arguments without touching process.env.
    if os.environ.get("HERMES_GPU_EFFECTS") == "1" and "--gpu-effects" not in sys.argv:
        sys.argv.append("--gpu-effects")

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Lumen")
    app.setApplicationDisplayName("Lumen")

    engine = QQmlApplicationEngine()
    backend = Backend()
    engine.rootContext().setContextProperty("backend", backend)

    qml = Path(__file__).resolve().parent / "qml" / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml)))
    if not engine.rootObjects():
        print("[lumen] FATAL: Main.qml failed to load", file=sys.stderr)
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
