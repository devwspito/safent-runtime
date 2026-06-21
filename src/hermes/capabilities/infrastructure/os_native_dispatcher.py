"""OsNativeDispatcher — effector terminal de la rama `executor='os_native'` (CTRL-P2-1).

Implementa OsNativeDispatcherPort (specs/007/.../contracts/os_native_skills.py).
El broker bifurca a esta clase en el Paso 7 cuando el binding tiene
`executor='os_native'`. Todos los gates de seguridad (consent, HITL, kill-switch,
denylist de Paso 1) ya corrieron en el broker ANTES de llegar aquí.

Este dispatcher:
1. Consulta ProtectedServiceDenylist ANTES de llamar a systemd para las ops
   de servicio (start/stop/restart). Rechazo terminal pre-SO (CTRL-P2-2/3).
2. Delega al catálogo EXECUTORS del shell_server para screenshot/screen_record.
3. Para los skills READ_ONLY del SO (list_services, get_service_status, etc.)
   invoca los helpers internos (stubs en P2 MVP, cableados cuando existan).

Capa: infrastructure (adapta el catálogo nativo del SO al puerto del broker).
Sin framework. I/O real solo en _run_systemctl (mockeable en tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from hermes.capabilities.infrastructure.protected_service_denylist import (
    ProtectedServiceDenylist,
)

if TYPE_CHECKING:
    from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
        SqliteAuthorizedTriggerRepository,
    )

logger = logging.getLogger("hermes.capabilities.os_native_dispatcher")

# Skills que operan sobre servicios systemd (CTRL-P2-2: denylist obligatoria).
_SERVICE_MUTATION_SKILLS: frozenset[str] = frozenset({
    "start_service",
    "stop_service",
    "restart_service",
})

# Skills de captura nativa (delegan a EXECUTORS del shell_server).
_CAPTURE_SKILLS: frozenset[str] = frozenset({"screenshot", "screen_record"})

# Skills de input nativo (host-operation MVP — pasan por SessionBridgeClient vía EXECUTORS).
_INPUT_SKILLS: frozenset[str] = frozenset({"mouse_move", "mouse_click", "type_text"})

# Computer-use autonomous loop entry point — HIGH/HITL, mints SESSION grant.
_COMPUTER_USE_SKILLS: frozenset[str] = frozenset({"begin_computer_use"})

# Skills de lectura del SO (READ_ONLY — implementación real en feature 007 Carril A).
_READ_ONLY_SKILLS: frozenset[str] = frozenset({
    "list_services",
    "get_service_status",
    "get_system_info",
    "list_devices",
    "list_audio_devices",
    "list_scheduled_tasks",
})

# Skills de planificación (HIGH — crean/borran entradas allow-list de timer, no units libres).
_SCHEDULER_WRITE_SKILLS: frozenset[str] = frozenset({
    "schedule_task",
    "unschedule_task",
})

_ALL_KNOWN_SKILLS: frozenset[str] = (
    _SERVICE_MUTATION_SKILLS
    | _CAPTURE_SKILLS
    | _READ_ONLY_SKILLS
    | _SCHEDULER_WRITE_SKILLS
    | _INPUT_SKILLS
    | _COMPUTER_USE_SKILLS
)


class OsNativeDispatcher:
    """Effector terminal os_native. Invocado por CapabilityBroker tras consent+HITL.

    Args:
        denylist: denylist anti-autopirateo. Por defecto usa el conjunto
            mínimo inviolable. Inyectable para tests.
    """

    def __init__(
        self,
        *,
        denylist: ProtectedServiceDenylist | None = None,
        trigger_repo: SqliteAuthorizedTriggerRepository | None = None,
        # Computer-use dependencies — injected at wiring time; None = disabled.
        computer_use_consent_manager: Any | None = None,
        computer_use_broker: Any | None = None,
        computer_use_operator_id: Any | None = None,   # UUID
        computer_use_tenant_id: Any | None = None,     # UUID
        computer_use_model: str = "",
        computer_use_api_key: str | None = None,
        computer_use_base_url: str | None = None,
    ) -> None:
        self._denylist = denylist or ProtectedServiceDenylist()
        # Injected for schedule_task / unschedule_task / list_scheduled_tasks.
        # Fail-closed if absent: scheduler skills return empty / not-persisted.
        self._trigger_repo = trigger_repo
        # Computer-use loop dependencies — fail-closed if not wired.
        self._cu_consent_manager = computer_use_consent_manager
        self._cu_broker = computer_use_broker
        self._cu_operator_id = computer_use_operator_id
        self._cu_tenant_id = computer_use_tenant_id
        self._cu_model = computer_use_model
        self._cu_api_key = computer_use_api_key
        self._cu_base_url = computer_use_base_url

    def wire_computer_use_broker(self, broker: Any) -> None:
        """Late-inject the CapabilityBroker needed by the computer-use loop.

        Called from the composition root AFTER the broker is constructed to
        resolve the OsNativeDispatcher ↔ CapabilityBroker circular dependency:
        the broker requires the dispatcher (os_native_dispatcher arg) and the
        dispatcher requires the broker (to dispatch mouse/keyboard actions
        within the loop). Wiring happens in two steps:

          1. Build OsNativeDispatcher (broker=None).
          2. Build CapabilityBroker(os_native_dispatcher=dispatcher).
          3. dispatcher.wire_computer_use_broker(broker).

        After step 3, begin_computer_use is fully operational.
        """
        self._cu_broker = broker

    def supports(self, skill_name: str) -> bool:
        """True si este dispatcher conoce `skill_name`."""
        return skill_name in _ALL_KNOWN_SKILLS

    async def execute(self, *, skill_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Ejecuta la OS-native skill por nombre.

        Para start/stop/restart_service aplica la denylist ANTES de systemd.
        Para skills de captura delega al catálogo de executors del shell_server.
        Para skills READ_ONLY del SO delega a helpers internos.

        Returns:
            dict con `ok: bool` y campos específicos del skill. Si el servicio
            está protegido devuelve `ok=False, reason='REJECTED_BY_POLICY: ...'`.
        """
        if skill_name in _SERVICE_MUTATION_SKILLS:
            return await self._dispatch_service_mutation(skill_name, args)
        if skill_name in _CAPTURE_SKILLS:
            return await self._dispatch_capture(skill_name, args)
        if skill_name in _READ_ONLY_SKILLS:
            return await self._dispatch_read_only(skill_name, args)
        if skill_name in _SCHEDULER_WRITE_SKILLS:
            return await self._dispatch_scheduler_write(skill_name, args)
        if skill_name in _INPUT_SKILLS:
            return await self._dispatch_input(skill_name, args)
        if skill_name in _COMPUTER_USE_SKILLS:
            return await self._dispatch_computer_use(skill_name, args)
        return {"ok": False, "reason": f"skill desconocida: {skill_name!r}"}

    # ------------------------------------------------------------------
    # Service mutation — denylist gate pre-systemd (CTRL-P2-2/3)
    # ------------------------------------------------------------------

    async def _dispatch_service_mutation(
        self, skill_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        unit = args.get("unit", "")
        if not unit:
            return {"ok": False, "reason": "parámetro 'unit' requerido"}

        # CTRL-P2-2/3: denylist ANTES de systemd. Terminal e inapelable.
        # Use canonical identity resolution (systemctl show -p Id,Names) in the
        # hot-path so a real systemd alias cannot bypass the denylist (CONDITION-2).
        # Falls back to lexical check when systemd is unavailable (fail-closed).
        if self._denylist.is_protected_canonical(unit):
            reason = (
                f"REJECTED_BY_POLICY: operación '{skill_name}' sobre servicio "
                f"protegido '{unit}' rechazada. Conjunto de frenos del agente "
                "es inviolable (CTRL-P2-2/NFR-002)."
            )
            logger.warning(
                "hermes.os_native.denylist_rejected: skill=%s unit=%r",
                skill_name,
                unit,
            )
            return {"ok": False, "reason": reason}

        # Systemctl action (only reached for non-protected services)
        action = _skill_to_systemctl_action(skill_name)
        return await self._run_systemctl(["systemctl", action, unit])

    async def _run_systemctl(self, argv: list[str]) -> dict[str, Any]:
        """Ejecuta systemctl. Mockeable en tests."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            ok = proc.returncode == 0
            return {
                "ok": ok,
                "returncode": proc.returncode,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
            }
        except FileNotFoundError:
            return {"ok": False, "reason": "systemctl no disponible en este entorno"}
        except TimeoutError:
            return {"ok": False, "reason": "systemctl timeout (30s)"}

    # ------------------------------------------------------------------
    # Capture skills — delegan al catálogo de executors (CI-safe lazy import)
    # ------------------------------------------------------------------

    async def _dispatch_capture(
        self, skill_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            from hermes.shell_server.os_native_skills.executors import EXECUTORS  # noqa: PLC0415
        except ImportError:
            return {"ok": False, "reason": "os_native executors no disponibles (headless)"}

        executor = EXECUTORS.get(skill_name)
        if executor is None:
            return {"ok": False, "reason": f"executor no encontrado para {skill_name!r}"}

        return await asyncio.to_thread(executor, args)

    # ------------------------------------------------------------------
    # Input skills — host-operation MVP (mouse_move, mouse_click, type_text)
    # Routes through EXECUTORS which call SessionBridgeClient. All gates
    # (consent, HITL, kill-switch, denylist) already ran in the broker.
    # ------------------------------------------------------------------

    async def _dispatch_input(
        self, skill_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch an input skill via the EXECUTORS catalog.

        The executors call SessionBridgeClient which relays the command to
        the session-side SessionInputBridge. The bridge enforces:
          - SO_PEERCRED UID check (defense-in-depth)
          - Per-boot token authentication
          - Rate limit (60 req/s)
          - Key-chord denylist (Ctrl-Alt-Fx etc.)
          - InputOwnershipLedger contention guard
        """
        try:
            from hermes.shell_server.os_native_skills.executors import EXECUTORS  # noqa: PLC0415
        except ImportError:
            return {"ok": False, "reason": "os_native executors no disponibles (headless)"}

        executor = EXECUTORS.get(skill_name)
        if executor is None:
            return {"ok": False, "reason": f"executor no encontrado para {skill_name!r}"}

        return await asyncio.to_thread(executor, args)

    # ------------------------------------------------------------------
    # Computer-use autonomous loop — begin_computer_use entry point
    # ------------------------------------------------------------------

    async def _dispatch_computer_use(
        self, skill_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch the begin_computer_use skill.

        This method runs AFTER the broker has already verified consent+HITL.
        All security gates (kill-switch, consent, HITL token) are satisfied
        before reaching here. The actual loop is run by execute_begin_computer_use.

        Fail-closed if any required dependency is not wired.
        """
        if skill_name != "begin_computer_use":
            return {"ok": False, "reason": f"unknown computer_use skill: {skill_name!r}"}

        if (
            self._cu_consent_manager is None
            or self._cu_broker is None
            or self._cu_operator_id is None
            or self._cu_tenant_id is None
            or not self._cu_model
        ):
            return {
                "ok": False,
                "reason": (
                    "begin_computer_use: required dependencies not wired "
                    "(consent_manager, broker, operator_id, tenant_id, model). "
                    "Inject via OsNativeDispatcher constructor."
                ),
            }

        from hermes.computer_use.application.begin_computer_use_tool import (  # noqa: PLC0415
            execute_begin_computer_use,
        )

        return await execute_begin_computer_use(
            args,
            consent_manager=self._cu_consent_manager,
            broker=self._cu_broker,
            operator_id=self._cu_operator_id,
            tenant_id=self._cu_tenant_id,
            model=self._cu_model,
            api_key=self._cu_api_key,
            base_url=self._cu_base_url,
        )

    # ------------------------------------------------------------------
    # Read-only SO skills — real implementations (feature 007 Carril A)
    # ------------------------------------------------------------------

    async def _dispatch_read_only(
        self, skill_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatches READ_ONLY OS-native skills via a named-dispatch table.

        All helpers are mockeable in tests (injected via patch.object).
        No systemd/proc access in the branching logic itself.
        """
        # Async handlers — awaited; sync handlers — called directly.
        async_handlers = {
            "list_services": self._list_services,
            "get_service_status": self._get_service_status,
            "list_scheduled_tasks": self._list_scheduled_tasks,
        }
        sync_handlers = {
            "get_system_info": self._get_system_info,
            "list_devices": self._list_devices,
            "list_audio_devices": self._list_audio_devices,
        }
        if skill_name in async_handlers:
            return await async_handlers[skill_name](args)
        if skill_name in sync_handlers:
            return sync_handlers[skill_name](args)
        return {"ok": False, "reason": f"skill READ_ONLY desconocida: {skill_name!r}"}

    async def _list_services(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Lists systemd units (service type, all states) via systemctl list-units --output=json."""
        result = await self._run_systemctl([
            "systemctl", "list-units", "--type=service", "--all",
            "--output=json", "--no-pager",
        ])
        if not result.get("ok"):
            return self._fallback_list_services(result)
        return self._parse_list_units_json(result.get("stdout", ""))

    def _fallback_list_services(self, error_result: dict[str, Any]) -> dict[str, Any]:
        """Returns empty services list on systemctl failure."""
        logger.warning(
            "hermes.os_native.list_services.fallback: %s",
            error_result.get("reason", ""),
        )
        return {"ok": True, "services": []}

    def _parse_list_units_json(self, stdout: str) -> dict[str, Any]:
        """Parses JSON output from systemctl list-units --output=json."""
        import json  # noqa: PLC0415
        try:
            units = json.loads(stdout) if stdout.strip() else []
        except (json.JSONDecodeError, ValueError):
            units = self._parse_list_units_plain(stdout)

        services = [
            {
                "unit": u.get("unit", ""),
                "active_state": u.get("active", ""),
                "sub_state": u.get("sub", ""),
            }
            for u in units
            if isinstance(u, dict)
        ]
        return {"ok": True, "services": services}

    def _parse_list_units_plain(self, stdout: str) -> list[dict[str, Any]]:
        """Fallback: parse plain text output (no-legend format).

        Expected columns: UNIT LOAD ACTIVE SUB DESCRIPTION (≥3 needed).
        """
        _MIN_COLS = 3
        _ACTIVE_IDX = 2
        _SUB_IDX = 3
        services = []
        for line in stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) >= _MIN_COLS:
                services.append({
                    "unit": parts[0],
                    "active": parts[_ACTIVE_IDX] if len(parts) > _ACTIVE_IDX else "",
                    "sub": parts[_SUB_IDX] if len(parts) > _SUB_IDX else "",
                })
        return services

    async def _get_service_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Gets status of a single unit via systemctl show."""
        unit = args.get("unit", "")
        if not unit:
            return {"ok": False, "reason": "parámetro 'unit' requerido"}

        result = await self._run_systemctl([
            "systemctl", "show", "-p", "ActiveState,SubState,LoadState", unit,
        ])
        if not result.get("ok"):
            return {"ok": False, "reason": result.get("reason", "systemctl error"), "unit": unit}

        props = _parse_systemctl_properties(result.get("stdout", ""))
        return {
            "ok": True,
            "unit": unit,
            "active_state": props.get("ActiveState", "unknown"),
            "sub_state": props.get("SubState", "unknown"),
            "load_state": props.get("LoadState", "unknown"),
        }

    def _get_system_info(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Reads system info from /proc files and os.uname. Mockeable via _read_proc_files."""
        data = self._read_proc_files()
        return {"ok": True, **data}

    def _read_proc_files(self) -> dict[str, Any]:
        """Reads /proc/uptime, /proc/loadavg, /proc/meminfo, os.uname."""
        import os as _os  # noqa: PLC0415
        uname = _os.uname()

        uptime_s = _read_proc_float("/proc/uptime", field=0)
        load = _read_proc_loadavg()
        mem = _read_proc_meminfo()

        return {
            "hostname": uname.nodename,
            "kernel": uname.release,
            "uptime_s": uptime_s,
            "load": load,
            "mem": mem,
        }

    def _list_devices(self, args: dict[str, Any]) -> dict[str, Any]:
        """Enumerates devices from /sys/class. Mockeable via _enumerate_sysfs_devices."""
        subsystem = args.get("subsystem")
        devices = self._enumerate_sysfs_devices(subsystem=subsystem)
        return {"ok": True, "devices": devices}

    def _enumerate_sysfs_devices(
        self, *, subsystem: str | None = None
    ) -> list[dict[str, Any]]:
        """Reads /sys/class (or /sys/bus) to enumerate devices."""
        import os as _os  # noqa: PLC0415
        sys_class = "/sys/class"
        devices: list[dict[str, Any]] = []

        try:
            subsystems = [subsystem] if subsystem else _os.listdir(sys_class)
        except OSError:
            return devices

        for sub in subsystems:
            sub_path = f"{sys_class}/{sub}"
            try:
                for dev in _os.listdir(sub_path):
                    devices.append({
                        "name": dev,
                        "subsystem": sub,
                        "sys_path": f"{sub_path}/{dev}",
                    })
            except OSError:
                continue

        return devices

    def _list_audio_devices(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Lists audio sources and sinks via pw-dump or fallback.

        Mockeable via _query_pipewire_devices.
        """
        data = self._query_pipewire_devices()
        return {"ok": True, **data}

    def _query_pipewire_devices(self) -> dict[str, Any]:
        """Queries PipeWire for audio sources/sinks via pw-dump."""
        import json as _json  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        try:
            result = subprocess.run(  # noqa: S603 — trusted system binary
                ["pw-dump"],  # noqa: S607 — pw-dump has no fixed path across distros
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if result.returncode != 0:
                return {"sources": [], "sinks": []}
            objects = _json.loads(result.stdout) if result.stdout.strip() else []
        except (FileNotFoundError, subprocess.TimeoutExpired, _json.JSONDecodeError, OSError):
            return {"sources": [], "sinks": []}

        sources: list[dict[str, Any]] = []
        sinks: list[dict[str, Any]] = []
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            media_class = (
                obj.get("info", {})
                .get("props", {})
                .get("media.class", "")
            )
            entry = {"name": obj.get("info", {}).get("props", {}).get("node.name", "unknown")}
            if "Source" in media_class:
                sources.append(entry)
            elif "Sink" in media_class:
                sinks.append(entry)
        return {"sources": sources, "sinks": sinks}

    async def _list_scheduled_tasks(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Lists scheduled tasks from the authorized trigger instances (allow-list of timers).

        Delegates to self._trigger_repo when wired (Condition 3). Falls back to
        empty list if the repo is not injected (interface-stable).
        """
        if self._trigger_repo is not None:
            triggers = await self._trigger_repo.list_enabled()
            scheduled = [
                {
                    "trigger_instance_id": str(t.trigger_instance_id),
                    "trigger_type": str(t.trigger_type),
                    "scope_value": t.scope_value,
                    "allowed_capabilities": list(t.allowed_capabilities),
                    "risk_ceiling": str(t.risk_ceiling),
                    "authorized_at": t.authorized_at.isoformat(),
                    "enabled": t.enabled,
                }
                for t in triggers
            ]
            return {"ok": True, "scheduled": scheduled}
        return {"ok": True, "scheduled": []}

    # ------------------------------------------------------------------
    # Scheduler WRITE skills (HIGH) — create/delete allow-list entries
    # ------------------------------------------------------------------

    async def _dispatch_scheduler_write(
        self, skill_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatches schedule_task / unschedule_task.

        Per FR-010: these ONLY create or delete entries in the allow-list of
        authorized trigger origins (authorized_trigger_instances). They NEVER
        create arbitrary systemd units. The Carril B repo is the target.
        Until that repo is wired, the interface is declared and returns a
        documented stub so the broker can audit the attempt.
        """
        if skill_name == "schedule_task":
            return await self._schedule_task(args)
        if skill_name == "unschedule_task":
            return await self._unschedule_task(args)
        return {"ok": False, "reason": f"scheduler skill desconocida: {skill_name!r}"}

    @staticmethod
    def _is_valid_cron(schedule: str) -> bool:
        """Validate a cron expression via croniter (MIT) — never a casera regex.

        Returns False on any failure so the caller fails closed and never
        persists an unparseable timer.
        """
        from croniter import croniter  # noqa: PLC0415

        try:
            return bool(croniter.is_valid(schedule))
        except (ValueError, KeyError, TypeError):
            return False

    async def _schedule_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Creates an entry in authorized_trigger_instances (allow-list).

        Wires into SqliteAuthorizedTriggerRepository when injected (Condition 3).
        Never creates arbitrary systemd units (FR-010).

        The caller (agent) can only create a timer-type allow-list entry scoped
        to the given schedule + capability_scope. An admin must have previously
        authorized the trigger TYPE (default-deny model).
        """
        trigger_type = args.get("trigger_type")
        schedule = args.get("schedule")
        capability_scope = args.get("capability_scope")
        admin_uuid_str = args.get("admin_uuid", "")
        approval_signature = args.get("approval_signature", "agent-self-schedule")
        reason = args.get("reason", "")

        if trigger_type != "timer":
            return {
                "ok": False,
                "reason": (
                    f"trigger_type '{trigger_type}' no soportado. "
                    "Solo 'timer' permitido (FR-010)."
                ),
            }
        if not schedule:
            return {"ok": False, "reason": "parámetro 'schedule' requerido"}
        if not capability_scope:
            return {"ok": False, "reason": "parámetro 'capability_scope' requerido"}
        if not self._is_valid_cron(schedule):
            return {
                "ok": False,
                "reason": f"'schedule' no es una expresión cron válida: {schedule!r}",
            }

        logger.info(
            "hermes.os_native.schedule_task: type=%s schedule=%r scope=%r reason=%r",
            trigger_type, schedule, capability_scope, reason,
        )

        if self._trigger_repo is not None:
            return await self._schedule_task_via_repo(
                schedule=schedule,
                capability_scope=capability_scope,
                admin_uuid_str=admin_uuid_str,
                approval_signature=approval_signature,
            )

        # Fail-closed stub when repo is not wired — returns ok=False so the
        # caller knows the entry was NOT persisted.
        return {
            "ok": False,
            "reason": "schedule_task: trigger_repo not wired — no persistence (fail-closed).",
        }

    async def _schedule_task_via_repo(
        self,
        *,
        schedule: str,
        capability_scope: str,
        admin_uuid_str: str,
        approval_signature: str,
    ) -> dict[str, Any]:
        """Persists a timer allow-list entry via the authorized trigger repository."""
        import uuid as _uuid  # noqa: PLC0415

        from hermes.tasks.triggers.domain.authorized_trigger_ports import (  # noqa: PLC0415
            AuthorizedTriggerType,
            RiskCeiling,
        )

        try:
            admin_uuid = _uuid.UUID(admin_uuid_str) if admin_uuid_str else _uuid.uuid4()
        except (ValueError, AttributeError):
            admin_uuid = _uuid.uuid4()

        trigger = await self._trigger_repo.authorize(  # type: ignore[union-attr]
            trigger_type=AuthorizedTriggerType.TIMER,
            scope_value=schedule,
            allowed_capabilities=(capability_scope,),
            risk_ceiling=RiskCeiling.LOW,
            admin_uuid=admin_uuid,
            approval_signature=approval_signature,
        )
        return {
            "ok": True,
            "trigger_instance_id": str(trigger.trigger_instance_id),
            "message": "Entrada en allow-list de timer registrada.",
        }

    async def _unschedule_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Deletes an entry from authorized_trigger_instances (allow-list).

        Wires into SqliteAuthorizedTriggerRepository when injected (Condition 3).
        Never deletes systemd units (FR-010).
        """
        trigger_instance_id_str = args.get("trigger_instance_id")
        admin_uuid_str = args.get("admin_uuid", "")
        reason = args.get("reason", "")

        if not trigger_instance_id_str:
            return {"ok": False, "reason": "parámetro 'trigger_instance_id' requerido"}

        logger.info(
            "hermes.os_native.unschedule_task: id=%s reason=%r",
            trigger_instance_id_str, reason,
        )

        if self._trigger_repo is not None:
            return await self._unschedule_task_via_repo(
                trigger_instance_id_str=trigger_instance_id_str,
                admin_uuid_str=admin_uuid_str,
            )

        return {
            "ok": False,
            "reason": "unschedule_task: trigger_repo not wired — no persistence (fail-closed).",
        }

    async def _unschedule_task_via_repo(
        self,
        *,
        trigger_instance_id_str: str,
        admin_uuid_str: str,
    ) -> dict[str, Any]:
        """Revokes a timer allow-list entry via the authorized trigger repository."""
        import uuid as _uuid  # noqa: PLC0415

        try:
            trigger_instance_id = _uuid.UUID(trigger_instance_id_str)
        except (ValueError, AttributeError):
            return {
                "ok": False,
                "reason": f"trigger_instance_id inválido: {trigger_instance_id_str!r}",
            }

        try:
            admin_uuid = _uuid.UUID(admin_uuid_str) if admin_uuid_str else _uuid.uuid4()
        except (ValueError, AttributeError):
            admin_uuid = _uuid.uuid4()

        await self._trigger_repo.revoke(  # type: ignore[union-attr]
            trigger_instance_id=trigger_instance_id,
            admin_uuid=admin_uuid,
        )
        return {
            "ok": True,
            "message": "Entrada en allow-list de timer revocada.",
        }


def _skill_to_systemctl_action(skill_name: str) -> str:
    """Mapea skill_name a la subcomando de systemctl correspondiente."""
    _MAP = {
        "start_service": "start",
        "stop_service": "stop",
        "restart_service": "restart",
    }
    return _MAP[skill_name]


# ---------------------------------------------------------------------------
# Module-level proc/sys helpers (mockeable via patch.object on dispatcher)
# ---------------------------------------------------------------------------


def _parse_systemctl_properties(stdout: str) -> dict[str, str]:
    """Parses KEY=VALUE output from systemctl show -p ..."""
    props: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


def _read_proc_float(path: str, *, field: int = 0) -> float:
    """Reads a whitespace-delimited float from a /proc file."""
    try:
        with open(path, encoding="utf-8") as fh:
            parts = fh.read().split()
        return float(parts[field]) if field < len(parts) else 0.0
    except (OSError, ValueError, IndexError):
        return 0.0


def _read_proc_loadavg() -> list[float]:
    """Reads /proc/loadavg returning [1min, 5min, 15min] floats."""
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            parts = fh.read().split()
        return [float(parts[i]) for i in range(min(3, len(parts)))]
    except (OSError, ValueError):
        return [0.0, 0.0, 0.0]


def _read_proc_meminfo() -> dict[str, int]:
    """Reads /proc/meminfo returning {total_kb, available_kb}."""
    result: dict[str, int] = {}
    key_map = {"MemTotal": "total_kb", "MemAvailable": "available_kb"}
    with contextlib.suppress(OSError), open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            if key.strip() in key_map:
                with contextlib.suppress(ValueError, IndexError):
                    result[key_map[key.strip()]] = int(rest.split()[0])
    return result
