"""BrowserJailSpawner — construye el argv de lanzamiento jailed del navegador.

Spec 009 §4 — confinamiento del proceso navegador:
  - Privilegio: el daemon (User=hermes) no tiene los caps para llamar a
    systemd-run --scope con NetworkNamespacePath= (necesita CAP_NET_ADMIN +
    CAP_SYS_ADMIN). El helper root hermes-browser-launcher.service recibe la
    solicitud por AF_UNIX + SO_PEERCRED y ejecuta el systemd-run con una
    plantilla HARDCODED — el caller no puede ampliar su propio scope.
  - Jail script: /usr/libexec/hermes/browser-jail — verifica netns, aplica
    Landlock soft y execv al binario del navegador.

Política de activación (env HERMES_BROWSER_JAIL):
  - "1" (default en nodo): jail vía hermes-browser-launcher (root helper).
    INVARIANTE DE SEGURIDAD: si el launcher no está disponible y el jail
    está activo, build_jailed_argv LANZA BrowserLauncherRequired — NO
    hay fallback a argv directo. Sin launcher = sin browser.
  - "0" (CI sin systemd): argv directo al binario. Nunca en producción.
  El flag se establece EXPLÍCITAMENTE en las units hermes-runtime.service y
  hermes-shell-server.service (auditable con `systemctl show`).

Env vars inyectadas al jail por el daemon (Finding D — fail-closed credenciales):
  HERMES_JAIL_HAS_CREDENTIALS=1  — tarea con credenciales/secrets → Landlock
      forzado (sin Landlock + credenciales = abort en el jail script).
  HERMES_JAIL_SUPERVISED=1       — sesión teaching con HITL activo →
      degradación suave permitida (sin credenciales, operador en control).

Asimetría teaching / autónomo (spec 009 §3):
  - Sesiones autónomas (credenciales): jail completo + egress default-deny.
  - Sesiones teaching (supervisadas): mismo netns + mismo jail, open-logged.
    La asimetría está en la política del proxy, no en el jail físico.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

_JAIL_SCRIPT = "/usr/libexec/hermes/browser-jail"
_NETNS_PATH = "/run/netns/hermes-browser"
_BROWSER_SESSIONS_BASE = Path("/var/lib/hermes/browser-sessions")
_EGRESS_PROXY_SOCK = Path("/run/hermes/egress-proxy.sock")
_SLICE = "agents-os-browser.slice"

_PROXY_MSG_MAX_BYTES = 16_384


class BrowserLauncherRequired(RuntimeError):
    """Raised when the jail is active but the caller tried to bypass the launcher.

    When HERMES_BROWSER_JAIL=1, the browser MUST be launched via
    hermes-browser-launcher (root helper). There is no bare-argv fallback.
    Use BrowserLauncherClient.launch() instead of subprocess directly.
    """


def _jail_enabled() -> bool:
    return os.environ.get("HERMES_BROWSER_JAIL", "1") == "1"


def build_jail_env(
    *,
    has_credentials: bool,
    supervised: bool,
    session_name: str,
) -> dict[str, str]:
    """Build the environment dict that the jail script consumes.

    These vars drive the fail-closed logic in hermes-browser-jail:
      HERMES_JAIL_HAS_CREDENTIALS=1  → Landlock mandatory; abort if unavailable.
      HERMES_JAIL_SUPERVISED=1       → soft-degrade allowed (teaching, HITL active).
      HERMES_BROWSER_SESSION         → used by landlock_loader to resolve the
                                       per-session browser-sessions path.

    Args:
        has_credentials: True when the task carries secrets / whitelist non-empty.
        supervised: True when a human operator is watching in real-time (teaching).
        session_name: the browser session name (e.g. "exec-abc123").

    Returns:
        Dict to pass as additional env to systemd-run / the jail.
    """
    return {
        "HERMES_JAIL_HAS_CREDENTIALS": "1" if has_credentials else "0",
        "HERMES_JAIL_SUPERVISED": "1" if supervised else "0",
        "HERMES_BROWSER_SESSION": session_name,
    }


def build_jailed_argv(
    *,
    session_name: str,
    browser_argv: list[str],
    domains_whitelist: tuple[str, ...] = (),
    has_credentials: bool = False,
    supervised: bool = False,
) -> list[str]:
    """Build the argv for an unconfined (CI) or RAISES for a jailed launch.

    When HERMES_BROWSER_JAIL=0 (CI), returns browser_argv unchanged.

    When HERMES_BROWSER_JAIL=1 (node), raises BrowserLauncherRequired.
    Callers must use BrowserLauncherClient.launch() — there is NO bare-argv
    fallback in production. This hard-fail prevents accidentally running an
    unconfined browser when the jail is expected.

    Args:
        session_name: browser session name (e.g. "exec-abc123").
        browser_argv: the browser binary + flags.
        domains_whitelist: permitted domains (empty = open-logged / discovery).
        has_credentials: True when the task carries secrets.
        supervised: True when a human is watching (teaching mode).

    Returns:
        browser_argv unchanged when jail is disabled (CI only).

    Raises:
        BrowserLauncherRequired: when HERMES_BROWSER_JAIL=1.
    """
    if not _jail_enabled():
        logger.debug(
            "browser_jail.disabled session=%s — HERMES_BROWSER_JAIL!=1",
            session_name,
        )
        return browser_argv

    # INVARIANT: no bare-argv fallback when jail is active.
    raise BrowserLauncherRequired(
        f"browser_jail: HERMES_BROWSER_JAIL=1 for session={session_name!r}; "
        "use BrowserLauncherClient.launch() instead of subprocess directly. "
        "There is no bare-argv fallback when the jail is active."
    )


def push_egress_policy(
    *,
    session_name: str,
    domains_whitelist: tuple[str, ...],
    teaching_mode: bool = False,
) -> None:
    """Push the egress policy to the filtering proxy via its control socket.

    The proxy accepts newline-delimited JSON. If the socket is absent
    (dev/CI without proxy), log and continue — not a fatal error.

    Asymmetry (spec 009 §3):
        - teaching_mode=True → "open-logged" (all navigation audited).
        - No whitelist → "open-logged" (discovery).
        - Whitelist non-empty → "default-deny".

    Args:
        session_name: session identifier (policy key in the proxy).
        domains_whitelist: allowed domains (empty = open-logged).
        teaching_mode: True when a human supervises the session live.
    """
    if teaching_mode or not domains_whitelist:
        mode = "open-logged"
    else:
        mode = "default-deny"

    payload = json.dumps({
        "session_id": session_name,
        "mode": mode,
        "domains": list(domains_whitelist),
    }) + "\n"

    sock_path = _EGRESS_PROXY_SOCK
    if not sock_path.exists():
        logger.warning(
            "browser_jail.egress_proxy_sock_missing path=%s session=%s "
            "— egress policy NOT sent (proxy not active)",
            sock_path,
            session_name,
        )
        return

    try:
        _send_to_unix_sock(sock_path, payload.encode())
        logger.info(
            "browser_jail.egress_policy_pushed session=%s mode=%s domains=%d",
            session_name,
            mode,
            len(domains_whitelist),
        )
    except OSError as exc:
        logger.warning(
            "browser_jail.egress_policy_push_failed session=%s error=%s "
            "— session continues without active egress policy",
            session_name,
            exc,
        )


def _send_to_unix_sock(sock_path: Path, data: bytes) -> None:
    """Send *data* to a UNIX stream socket and close. Timeout 2 s."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(str(sock_path))
        total = 0
        view = memoryview(data)
        while total < len(data):
            sent = sock.send(view[total:])
            if sent == 0:
                break
            total += sent
