"""AppLaunchSurfaceAdapter — lanza apps de escritorio via el compositor.

El daemon hermes corre sin display (usuario "hermes"). Para lanzar una app
nativa debe señalizar al compositor safentso-shell (usuario "hermes-user")
via D-Bus: AppLaunchRequested(cmd: s). El compositor ejecuta:
    onAppLaunchRequested(cmd) → sysManager.launchNativeApp(cmd)

Este adapter:
  1. Resuelve app_name → binario via una allow-list de aliases (fail-closed).
  2. Valida que el binario resultante sea un nombre seguro (a-z0-9-_, sin args).
  3. Si el binario es el navegador (chromium-browser) y el payload trae una
     url válida, el cmd emitido es "chromium-browser <url>". El compositor
     usa shlex.split() internamente, por lo que un espacio en cmd es seguro.
  4. Invoca launch_emitter(cmd) — callback inyectado que emite la señal D-Bus.
  5. Devuelve ReplayOutcome.ok sin esperar confirmación (fire-and-signal).

Seguridad:
  - URLs validadas con fail-closed: solo http:// y https://; longitud ≤ 2048;
    sin metacaracteres de shell (;|&$`<>\\n\\r\\t y comillas). file://, data:,
    javascript: bloqueados explícitamente.
  - NUNCA usa subprocess ni shell — delega 100% al compositor.
  - Binarios fuera de la allow-list y sin forma válida → REJECTED_BY_POLICY.
  - app_name se normaliza (lower+accent-strip) antes de resolver — no hay
    path-injection posible porque el resultado es siempre un basename puro.

Capa: infrastructure (adapta el contrato de dominio al canal D-Bus/compositor).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable
from typing import Any
from uuid import UUID

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger("hermes.agents_os.app_launch")

# ---------------------------------------------------------------------------
# Allow-list: alias (lower, accent-stripped) → binary basename
# Only binaries baked in the image (Containerfile.personal-desktop dnf install).
# ---------------------------------------------------------------------------
_ALIAS_MAP: dict[str, str] = {
    # gnome-calculator
    "calculadora": "gnome-calculator",
    "calculator": "gnome-calculator",
    "calc": "gnome-calculator",
    "gnome-calculator": "gnome-calculator",
    # gnome-text-editor
    "editor": "gnome-text-editor",
    "texto": "gnome-text-editor",
    "text editor": "gnome-text-editor",
    "bloc": "gnome-text-editor",
    "bloc de notas": "gnome-text-editor",
    "gnome-text-editor": "gnome-text-editor",
    # evince
    "pdf": "evince",
    "evince": "evince",
    "documentos pdf": "evince",
    "visor pdf": "evince",
    "document viewer": "evince",
    # chromium — el RPM chromium en Fedora instala el binario como
    # `chromium-browser` (NO `chromium`); mapear al basename real o el
    # pre-check which() falla y "abre el navegador" no abre nada.
    "navegador": "chromium-browser",
    "browser": "chromium-browser",
    "chromium": "chromium-browser",
    "chromium-browser": "chromium-browser",
    "chrome": "chromium-browser",
    "web": "chromium-browser",
    "internet": "chromium-browser",
}

# Binaries that accept a URL as their first positional argument.
# Any binary NOT in this set will have URLs silently ignored (fail-safe).
_URL_ACCEPTING_BINARIES: frozenset[str] = frozenset({
    "chromium-browser",
    "chromium",
    "firefox",
    "xdg-open",
})

# Safe binary basename pattern: only a-z, 0-9, hyphen, underscore.
_SAFE_BINARY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# URL safety: only http/https, max 2048 chars, no shell metacharacters.
# Blocks file://, data:, javascript:, and shell injection chars.
_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_URL_MAX_LEN = 2048
# Shell metacharacters that must never appear in a URL passed to the compositor.
_URL_SHELL_METACHAR_RE = re.compile(r'[;|&$`<>\\\n\r\t"\'\\]')

_AVAILABLE_APPS = sorted({*_ALIAS_MAP.values()})


class AppLaunchSurfaceAdapter:
    """SurfaceAdapterPort para APP_LAUNCH — bridge daemon→compositor.

    Args:
        launch_emitter: callback(cmd: str) → None. Llamado para emitir
            AppLaunchRequested(cmd) al bus. Inyectado desde DbusRuntimeAdapter
            tras la construcción (patrón idéntico a scan_signal_emitter).
            None hasta que el bus arranque — en ese ventana los intentos
            de replay devuelven execution_failed con mensaje claro.
    """

    surface_kind: SurfaceKind = SurfaceKind.APP_LAUNCH

    def __init__(self, *, launch_emitter: Callable[[str], None] | None = None) -> None:
        self._launch_emitter = launch_emitter

    def set_launch_emitter(self, emitter: Callable[[str], None]) -> None:
        """Inyecta (o reemplaza) el emitter tras la construcción del bus."""
        self._launch_emitter = emitter

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Captura pasiva — registra la intención sin ejecutar."""
        return CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=intent_desc,
            payload=dict(params),
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Resuelve app_name (+ url opcional) y emite AppLaunchRequested via D-Bus.

        Fail-closed en todos los ejes:
          - surface_kind mismatch → REJECTED_BY_POLICY.
          - app_name ausente → REJECTED_BY_POLICY.
          - nombre no resuelto ni seguro → REJECTED_BY_POLICY (listing disponibles).
          - url presente pero inválida → REJECTED_BY_POLICY.
          - emitter no disponible → EXECUTED_FAILED (bus aún no listo).
        """
        if action.surface_kind != SurfaceKind.APP_LAUNCH:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=(
                    f"surface_kind mismatch: esperado APP_LAUNCH, "
                    f"recibido {action.surface_kind!r} — fail-closed"
                ),
            )

        app_name = action.payload.get("app_name")
        if not isinstance(app_name, str) or not app_name.strip():
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason="payload.app_name requerido y debe ser un string no vacío",
            )

        binary = _resolve_binary(app_name)
        if binary is None:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=(
                    f"app_name {app_name!r} no reconocido. "
                    f"Apps disponibles: {_AVAILABLE_APPS}"
                ),
            )

        url = action.payload.get("url") or ""
        cmd_result = _build_cmd(binary, url)
        if isinstance(cmd_result, str) and cmd_result.startswith("ERROR:"):
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=cmd_result[len("ERROR:"):].strip(),
            )
        cmd: str = cmd_result  # type: ignore[assignment]

        if self._launch_emitter is None:
            return ReplayOutcome.failed(
                action.action_id,
                error=(
                    "D-Bus emitter no disponible — el bus aún no ha arrancado. "
                    "Reintentar en unos segundos."
                ),
            )

        self._launch_emitter(cmd)
        logger.info(
            "hermes.app_launch.requested app_name=%r url=%r cmd=%r action_id=%s",
            app_name,
            url or None,
            cmd,
            action.action_id,
        )
        return ReplayOutcome.ok(
            action.action_id,
            result={"launched": cmd, "app_name": app_name, "url": url or None},
        )

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Serialización canónica determinista para HMAC cross-surface."""
        canonical = {
            "surface_kind": action.surface_kind.value,
            "app_name": action.payload.get("app_name", ""),
            "url": action.payload.get("url", ""),
            "intent_desc": action.intent_desc,
        }
        return json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode("utf-8")


# ---------------------------------------------------------------------------
# Resolver (module-private)
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """Lower-case + strip accents (NFD decompose → drop combining marks)."""
    nfd = unicodedata.normalize("NFD", name.lower().strip())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _resolve_binary(app_name: str) -> str | None:
    """Resolve app_name to a safe binary basename.

    Resolution order:
      1. Alias map lookup (case/accent-insensitive).
      2. Normalized name itself, if it matches _SAFE_BINARY_RE (direct binary).

    Returns None when nothing resolves — caller produces REJECTED_BY_POLICY.
    The returned string is always a basename (no path, no args, no shell chars).
    """
    normalized = _normalize(app_name)
    binary = _ALIAS_MAP.get(normalized)
    if binary is not None:
        return binary

    if _SAFE_BINARY_RE.match(normalized):
        return normalized

    return None


def _validate_url(url: str) -> str | None:
    """Validate a URL for safe shell-free passing to the compositor.

    Returns None on success (URL is safe), or an error string on failure.

    Validation rules (fail-closed):
      - Must start with http:// or https:// (case-insensitive).
      - Length ≤ 2048 characters.
      - Must NOT contain shell metacharacters: ; | & $ ` < > \\ newline tab quotes.
      - file://, data:, and javascript: schemes are explicitly blocked by the
        scheme check above (they don't start with http/https).
    """
    if not url:
        return "URL vacía"
    if len(url) > _URL_MAX_LEN:
        return f"URL demasiado larga ({len(url)} > {_URL_MAX_LEN} chars)"
    if not _URL_SCHEME_RE.match(url):
        return (
            f"Esquema de URL no permitido en {url!r}. "
            "Solo se aceptan http:// y https:// (file://, javascript:, data: bloqueados)"
        )
    match = _URL_SHELL_METACHAR_RE.search(url)
    if match:
        return (
            f"URL contiene metacarácter de shell {match.group()!r} — "
            "rechazado por política de seguridad (fail-closed)"
        )
    return None


def _build_cmd(binary: str, url: str) -> str:
    """Build the compositor command string for a binary and optional URL.

    If a URL is provided and the binary accepts URLs, the command is
    ``"<binary> <url>"``. The compositor's launchNativeApp uses shlex.split()
    to parse this — a single space-separated arg is safe.

    If no URL is provided (or the binary doesn't accept URLs), returns
    just the binary name (current behavior, no regression).

    Returns either the cmd string or an "ERROR:<reason>" sentinel.
    """
    if not url:
        return binary

    if binary not in _URL_ACCEPTING_BINARIES:
        # Binary doesn't accept URLs — silently ignore and launch bare binary.
        logger.info(
            "hermes.app_launch.url_ignored binary=%r url=%r "
            "(binary not in URL-accepting list)",
            binary,
            url,
        )
        return binary

    error = _validate_url(url)
    if error is not None:
        return f"ERROR:{error}"

    return f"{binary} {url}"
