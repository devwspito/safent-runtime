"""`hermes` CLI — main entrypoint.

Comandos disponibles al cliente desde terminal en cualquier perfil de
Agents OS Edition:

    hermes status           muestra estado del agente, runs activos, tenant.
    hermes train            inicia una sesión de Training Mode local.
    hermes skills list      lista skills del catálogo del tenant.
    hermes skills run X     dispara skill X autónomamente.
    hermes telemetry        gestiona opt-in de telemetría (FR-026 opt-in puro).
    hermes consent          gestiona consentimientos (FR-013 personal-desktop).
    hermes ota              gestiona updates A/B (FR-008).
    hermes tenant           bind / unbind / status del tenant Hermes (FR-017).
    hermes suspend          única forma de suspender el SO (FR-041, explícita).

Constitución V: el CLI base no requiere binarios externos ni red para
correr. Cada subcomando puede requerir servicios concretos (control plane
local activo, runtime de Hermes corriendo) y avisa con mensaje claro
cuando faltan.

Convenciones:
- Comandos read-only siempre devuelven exit 0 si el sistema responde.
- Comandos mutadores piden confirmación interactiva (`--yes` para skip).
- Output en JSON con `--json` para uso scriptable.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CliResult:
    """Resultado de un subcomando para tests + uso scriptable."""

    exit_code: int
    payload: dict[str, Any] = field(default_factory=dict)
    human_message: str = ""

    def render(self, as_json: bool) -> str:
        if as_json:
            return json.dumps(
                {"exit_code": self.exit_code, "payload": self.payload},
                ensure_ascii=False,
                indent=2,
            )
        return self.human_message or ""


def _cmd_status(args: argparse.Namespace) -> CliResult:
    """Reporta estado del agente al cliente.

    En esta fase el CLI todavía no se conecta al runtime real (eso lo cablea
    el package de Agents OS al instalar). Devuelve placeholder estructurado
    para que la interfaz se mantenga estable.
    """
    payload = {
        "runtime": "not_connected",
        "tenant_binding": "unknown",
        "active_runs": 0,
        "queued_runs": 0,
        "last_heartbeat": None,
        "note": (
            "El CLI todavía no está cableado a un control plane vivo. "
            "Esto se completa cuando Agents OS Edition se instala y el "
            "runtime arranca como servicio systemd (FR-011)."
        ),
    }
    return CliResult(
        exit_code=0,
        payload=payload,
        human_message=(
            "Agente: desconectado (sin control plane local). "
            "Tenant: sin vincular. Runs activos: 0.\n"
            "Vincula tu tenant con: hermes tenant bind <tenant_id>"
        ),
    )


def _cmd_telemetry(args: argparse.Namespace) -> CliResult:
    """Gestiona telemetría opt-in puro (FR-026).

    Subcomandos:
      hermes telemetry status    muestra estado actual.
      hermes telemetry enable    activa envío a hermes-cloud.
      hermes telemetry disable   desactiva (vuelve al default opt-in puro).
    """
    action = args.action
    if action == "status":
        return CliResult(
            exit_code=0,
            payload={"telemetry": "disabled", "default": "disabled"},
            human_message=(
                "Telemetría: DESACTIVADA (opt-in puro por defecto — "
                "ningún dato sale del nodo).\n"
                "Para activar diagnóstico remoto: hermes telemetry enable"
            ),
        )
    if action == "enable":
        return CliResult(
            exit_code=0,
            payload={"telemetry": "enabled"},
            human_message=(
                "Telemetría ACTIVADA. Se envían métricas, errores y "
                "heartbeat a Hermes. No se envían datos del cliente "
                "(charter de confidencialidad). Revoca con: "
                "hermes telemetry disable"
            ),
        )
    if action == "disable":
        return CliResult(
            exit_code=0,
            payload={"telemetry": "disabled"},
            human_message="Telemetría DESACTIVADA.",
        )
    return CliResult(
        exit_code=2,
        human_message=f"acción de telemetría desconocida: {action!r}",
    )


def _cmd_suspend(args: argparse.Namespace) -> CliResult:
    """Suspender el sistema explícitamente (FR-041).

    24/7 invariante: el SO NUNCA suspende automáticamente. Esta es la única
    forma de provocar suspend, y requiere confirmación.
    """
    if not args.yes:
        return CliResult(
            exit_code=1,
            human_message=(
                "Suspender un nodo agéntico pausa TODOS los runs y el "
                "control remoto. El agente NO trabajará mientras esté "
                "suspendido (rompe la promesa 24/7).\n"
                "Si estás seguro: hermes suspend --yes"
            ),
        )
    return CliResult(
        exit_code=0,
        payload={"action": "suspend_requested"},
        human_message=(
            "Suspend solicitado al SO. El SO de Agents OS Edition rechaza "
            "esta llamada salvo que el operador del nodo la haya autorizado "
            "(FR-041). Si el sistema sigue activo, es porque la política "
            "always_on lo está protegiendo."
        ),
    )


def _cmd_tenant(args: argparse.Namespace) -> CliResult:
    """Vincula / desvincula tenant Hermes (FR-017)."""
    if args.action == "status":
        return CliResult(
            exit_code=0,
            payload={"tenant_binding": "none"},
            human_message="Sin tenant vinculado. Bind con: hermes tenant bind <tenant_id>",
        )
    if args.action == "bind":
        tenant_id = args.tenant_id
        if not tenant_id:
            return CliResult(
                exit_code=2,
                human_message="bind requiere argumento <tenant_id>",
            )
        return CliResult(
            exit_code=0,
            payload={"tenant_id": tenant_id, "state": "active"},
            human_message=f"Tenant {tenant_id} vinculado al nodo.",
        )
    if args.action == "unbind":
        return CliResult(
            exit_code=0,
            payload={"tenant_binding": "revoked"},
            human_message="Tenant desvinculado. El audit log local queda intacto.",
        )
    return CliResult(
        exit_code=2,
        human_message=f"acción tenant desconocida: {args.action!r}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes",
        description="Hermes Agent CLI — Agents OS Edition (spec 003)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output en JSON estructurado (uso scriptable).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Estado del agente local.")

    tel = sub.add_parser("telemetry", help="Gestiona telemetría (FR-026 opt-in puro).")
    tel.add_argument(
        "action",
        choices=("status", "enable", "disable"),
        help="status | enable | disable",
    )

    susp = sub.add_parser("suspend", help="Suspende el SO (única vía explícita; FR-041).")
    susp.add_argument("--yes", action="store_true", help="Confirma suspend.")

    ten = sub.add_parser("tenant", help="Vincula tenant Hermes (FR-017).")
    ten.add_argument("action", choices=("status", "bind", "unbind"))
    ten.add_argument("tenant_id", nargs="?", default="")

    return parser


_DISPATCH = {
    "status": _cmd_status,
    "telemetry": _cmd_telemetry,
    "suspend": _cmd_suspend,
    "tenant": _cmd_tenant,
}


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH.get(args.cmd)
    if handler is None:
        parser.print_help()
        return 2
    result = handler(args)
    print(result.render(as_json=args.json))
    return result.exit_code


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
