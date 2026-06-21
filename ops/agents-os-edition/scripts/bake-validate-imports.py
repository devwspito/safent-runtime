#!/usr/bin/env python3
"""bake-validate-imports — FAIL-LOUD gate de horneado.

Simula los imports que `hermes-runtime` y `hermes-shell-server` ejecutan al
arrancar. El patrón "COPY de módulo nuevo olvidado / wheel stale" pasa los
imports top-level del bake pero revienta en runtime (active_provider 2026-06-10,
providers, model_config…) → hermes-runtime muere → Requires= arrastra al
shell-server → todo verbo D-Bus da "name is not activatable" → SO pisapapel.

Este gate convierte ese fallo de primer-boot en fallo de horneado, donde se ve
y se arregla en minutos. NO ejecuta los daemons (necesitarían master.key, bus),
solo importa sus módulos.
"""
from __future__ import annotations

import importlib
import sys

CRITICAL_MODULES = [
    # runtime daemon (hermes-runtime.service)
    "hermes.runtime.__main__",
    "hermes.runtime.active_provider",
    "hermes.runtime.nous_engine",
    "hermes.runtime.model_config",
    "hermes.runtime.provider_config_source",
    "hermes.runtime.capability_tool_specs",
    "hermes.runtime.composio_tool_specs",
    "hermes.runtime.composio_tools_registry",
    # D-Bus surface (la cara SO-nativa del daemon)
    "hermes.agents_os.infrastructure.dbus_runtime_service",
    "hermes.agents_os.infrastructure.dbus_fast_runtime_adapter",
    # P3 scheduled-tasks: schema, domain, triggers, timer source, control-plane
    "hermes.tasks.infrastructure.schema",
    "hermes.tasks.control_plane.domain.ports",
    "hermes.tasks.control_plane.application.control_plane_service",
    "hermes.tasks.triggers.domain.authorized_trigger_ports",
    "hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository",
    "hermes.tasks.triggers.application.timer_trigger_source",
    # MCP Registry client (wheel stale no lo trae — COPY override en Containerfile)
    "hermes.mcp.infrastructure.registry_client",
    # Package Store de apps (paquete nuevo — sin COPY el daemon falla "No module")
    "hermes.package_store.application.package_store_service",
    # shell-server (hermes-shell-server.service)
    "hermes.shell_server.main",
    "hermes.shell_server.providers.domain",
    "hermes.shell_server.chat.conversation_repo",
    # acceso remoto (verbos D-Bus enable/disable/get_remote_access_status)
    "hermes.shell_server.remote_access_tunnel.api",
    "hermes.shell_server.remote_access_tunnel.service_status",
    # capa nativa de providers (hermes_cli del motor Nous)
    "hermes_cli.auth",
    "hermes_cli.config",
    "hermes_cli.runtime_provider",
    "hermes_cli.web_server",
    # motor Nous
    "run_agent",
    # utilidad de cron (scheduled-tasks): next_run_at. NO cron_descriptor —
    # su wheel trae un top-level tools/ que pisa el tools/ de hermes-agent
    # (motor Nous) en site-packages → ModuleNotFoundError: tools.registry.
    # El texto legible de la recurrencia se genera con una función propia.
    "croniter",
]


def main() -> int:
    failed: list[tuple[str, str]] = []
    for module_name in CRITICAL_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — queremos TODO fallo, no solo ImportError
            failed.append((module_name, f"{type(exc).__name__}: {exc}"))

    if failed:
        print("✕ BAKE FAIL — módulos críticos no importan:", file=sys.stderr)
        for name, err in failed:
            print(f"    {name}\n        {err}", file=sys.stderr)
        print(
            "  → añade el COPY override que falta en Containerfile.personal-desktop",
            file=sys.stderr,
        )
        return 1

    print(f"  bake-validate: {len(CRITICAL_MODULES)}/{len(CRITICAL_MODULES)} modules import OK")

    from hermes_cli.auth import PROVIDER_REGISTRY

    count = len(PROVIDER_REGISTRY)
    if count < 30:
        print(f"✕ BAKE FAIL — PROVIDER_REGISTRY={count} (<30): wheel stale", file=sys.stderr)
        return 1
    print(f"  PROVIDER_REGISTRY = {count} providers ✓")

    if _validate_qml() != 0:
        return 1
    return 0


def _validate_qml() -> int:
    """Falla el bake si algún QML del compositor tiene un ERROR DE SINTAXIS.

    Un parse-error QML deja al compositor en PANTALLA NEGRA (el error va al
    stderr del proceso, NO al journal → invisible). Este gate lo caza en build.
    Carga cada componente con QQmlComponent (offscreen, sin instanciar) y mira
    SOLO errores de sintaxis (los de "tipo desconocido" por falta de context
    properties hermes/sysManager son esperados sin runtime y se ignoran)."""
    import glob
    import os

    qml_dir = "/usr/lib/python3.13/site-packages/hermes/lumen/compositor/qml/desktop"
    if not os.path.isdir(qml_dir):
        print("  (qml gate: dir no encontrado, omito)")
        return 0
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import QUrl  # noqa: PLC0415
        from PySide6.QtGui import QGuiApplication  # noqa: PLC0415
        from PySide6.QtQml import QQmlComponent, QQmlEngine  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"  (qml gate: PySide6 no disponible: {exc}, omito)")
        return 0

    _app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    eng = QQmlEngine()
    eng.addImportPath(os.path.dirname(qml_dir))
    syntax_markers = (
        "Unexpected token", "Expected token", "Syntax error", "Expected ",
        "Unexpected ", "is not a type",
    )
    bad: list[tuple[str, str]] = []
    for path in sorted(glob.glob(os.path.join(qml_dir, "*.qml"))):
        comp = QQmlComponent(eng, QUrl.fromLocalFile(path))
        if not comp.isError():
            continue
        for e in comp.errors():
            msg = e.toString()
            if any(m in msg for m in syntax_markers):
                bad.append((os.path.basename(path), msg))
    if bad:
        print("✕ BAKE FAIL — QML con errores de SINTAXIS (pantalla negra):", file=sys.stderr)
        for name, err in bad:
            print(f"    {err}", file=sys.stderr)
        return 1
    n = len(glob.glob(os.path.join(qml_dir, "*.qml")))
    print(f"  QML gate: {n} componentes desktop parsean sin errores de sintaxis ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
