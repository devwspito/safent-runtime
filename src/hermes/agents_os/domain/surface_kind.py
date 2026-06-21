"""SurfaceKind — taxonomía de superficies operables por Hermes.

FR-027 (spec 003): el runtime soporta entrenamiento de skills sobre
superficies NO-navegador. Cada superficie se modela como un valor de
``SurfaceKind`` y tiene un ``SurfaceAdapter`` que implementa el contrato
abstracto (captura + replay).

Una skill puede componer steps de superficies heterogéneas
(``BROWSER`` + ``TERMINAL`` + ``FILESYSTEM`` + ``API_CALL``) — eso es lo
que permite "aprender CUALQUIER tarea".
"""

from __future__ import annotations

from enum import StrEnum


class SurfaceKind(StrEnum):
    """Superficies de captura disponibles.

    Cada valor tiene un adapter dedicado en ``infrastructure/`` que cumple
    el contrato ``SurfaceAdapterPort``.
    """

    BROWSER = "browser"
    """Heredada de spec 001/002: BrowserSession + Playwright + Stagehand."""

    TERMINAL = "terminal"
    """Comandos shell + outputs + exit codes. PTY wrapper o eBPF kprobe."""

    FILESYSTEM = "filesystem"
    """Lectura/escritura de archivos. fanotify + path allowlist."""

    API_CALL = "api_call"
    """Llamadas HTTP/gRPC autenticadas a APIs locales o de red."""

    DESKTOP_APP = "desktop_app"
    """Aplicaciones del escritorio operadas via AT-SPI / D-Bus."""

    SYSTEM_SETTINGS = "system_settings"
    """Settings del SO via D-Bus + PolicyKit."""

    PACKAGE_MANAGER = "package_manager"
    """rpm-ostree + flatpak + dnf install/uninstall/query."""

    SKILL_STORE = "skill_store"
    """Unified skill store — write/sign/persist SKILL.md artefacts.

    Handles skill_manage proposals from Nous after HITL approval.
    Produces signed SKILL.md files in the unified store (skill_packages_view
    + on-disk /var/lib/hermes/skills/). All writes go through SkillSigner v2.
    """

    MEMORY = "memory"
    """Tenant-scoped agent memory — governed writes to MEMORY.md / USER.md.

    Handles `memory` tool proposals from Nous (add/replace/remove).
    Writes are confined to /var/lib/hermes/memory/<tenant_id>/.
    PII is rejected at write time (fail-closed threat-pattern scanner).
    Classified LOW + auto_executable: no HITL required because memory is
    reversible internal agent state with no external effect.
    """

    MCP_CALL = "mcp_call"
    """Tool call dispatched to an MCP server via McpSurfaceAdapter.

    Each call is broker-gated (CTRL-1..14). The MCP server is treated as an
    UNTRUSTED content+effect source — identical posture to a browser page or
    a Composio action. HITL is forced for any HIGH-risk tool or tainted context
    (CTRL-5). Trust ladder: BUILTIN / USER_TRUSTED / USER_ADDED (plan.md §013).
    """

    APP_LAUNCH = "app_launch"
    """Lanza una aplicación de escritorio nativa via el compositor (lumenso-shell).

    El daemon (hermes, sin display) NUNCA puede lanzar apps directamente.
    En su lugar emite la señal D-Bus AppLaunchRequested(cmd) al compositor
    (hermes-user), que ejecuta sysManager.launchNativeApp(cmd).

    Solo los binarios en el resolver allow-list de AppLaunchSurfaceAdapter
    pueden ser lanzados — fail-closed ante cualquier nombre desconocido.
    """


_REQUIRES_CONSENT_IN_PERSONAL_DESKTOP: frozenset[SurfaceKind] = frozenset(
    {
        # FR-013: en personal-desktop, el agente NO accede sin consentimiento.
        SurfaceKind.FILESYSTEM,
        SurfaceKind.TERMINAL,
        SurfaceKind.DESKTOP_APP,
        SurfaceKind.SYSTEM_SETTINGS,
        SurfaceKind.PACKAGE_MANAGER,
        SurfaceKind.API_CALL,
        SurfaceKind.MCP_CALL,
        # BROWSER queda fuera porque el browser context del agente ES suyo;
        # NO toca el navegador personal del humano (esos son procesos
        # distintos).
    }
)


def requires_consent_in_personal_desktop(kind: SurfaceKind) -> bool:
    """FR-013: capability-based consent para personal-desktop."""
    return kind in _REQUIRES_CONSENT_IN_PERSONAL_DESKTOP
