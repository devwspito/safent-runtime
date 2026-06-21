"""Parser del comando de control enviado por el socket root.

El comando es un JSON en una sola línea:

    {"session_id": "...", "mode": "open-logged|default-deny", "domains": [...]}

El socket de control ``/run/hermes/egress-proxy.sock`` es modo 0600 y
pertenece a root.  El proceso del navegador (no privilegiado, en netns
hermes-browser) NO puede acceder a este socket (UNIX domain sockets
están fuera del netns del navegador — residen en el filesystem del host).
"""

from __future__ import annotations

import json

from hermes.egress_proxy.domain.policy import EgressMode, SessionPolicy

# Cap the domain list so a malformed/huge command can't bloat the policy engine.
# (Defense-in-depth; the control socket already caps the raw frame at 64 KiB.)
_MAX_DOMAINS: int = 4096


class ControlCommandError(ValueError):
    """JSON inválido o campos faltantes/incorrectos en el comando de control."""


def parse_control_command(raw: str | bytes) -> SessionPolicy:
    """Parsea el JSON de un comando de control y devuelve la SessionPolicy.

    Args:
        raw: línea JSON enviada por el socket de control.

    Returns:
        SessionPolicy lista para pasarse a EgressPolicyEngine.push_policy().

    Raises:
        ControlCommandError: si el JSON es inválido o falta algún campo.
    """
    # Catch EVERY parse failure, not just JSONDecodeError (red-team fuzz 2026-06-19):
    #   - UnicodeDecodeError: raw bytes that aren't valid UTF-8 (ValueError subclass).
    #   - RecursionError: deeply-nested JSON ({"a":[[[[…) blows the stack.
    #   - TypeError/ValueError: any other json scanner failure.
    # An UNCAUGHT exception here propagated out of the control-socket handler (it only
    # caught ControlCommandError) → a malformed push from a compromised setter could
    # break the proxy's control plane. Normalising to ControlCommandError keeps the
    # socket answering "ERR" and serving.
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError, TypeError) as exc:
        raise ControlCommandError(
            f"comando de control no parseable: {type(exc).__name__}"
        ) from exc

    if not isinstance(data, dict):
        raise ControlCommandError("El comando de control debe ser un objeto JSON")

    session_id = _require_str(data, "session_id")
    mode_raw = _require_str(data, "mode")
    domains_raw = data.get("domains", [])

    try:
        mode = EgressMode(mode_raw)
    except ValueError as exc:
        valid = ", ".join(m.value for m in EgressMode)
        raise ControlCommandError(
            f"Modo inválido {mode_raw!r}. Valores válidos: {valid}"
        ) from exc

    if not isinstance(domains_raw, list):
        raise ControlCommandError("El campo 'domains' debe ser una lista")
    if len(domains_raw) > _MAX_DOMAINS:
        raise ControlCommandError(
            f"demasiados domains: {len(domains_raw)} (máx {_MAX_DOMAINS})"
        )

    domains: frozenset[str] = frozenset(
        d.lower().strip() for d in domains_raw if isinstance(d, str) and d.strip()
    )

    return SessionPolicy(
        session_id=session_id,
        mode=mode,
        domains_whitelist=domains,
    )


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ControlCommandError(
            f"Campo '{key}' requerido (string no vacío) en comando de control"
        )
    return value
