"""DbusRuntimeAdapter — binding dbus-fast real sobre el system bus.

T038 🔒 — Binding D-Bus real sobre el system bus (CTRL-P1-1 / G1).

Expone org.hermes.Runtime1 en el system bus bajo el nombre bien conocido
`org.hermes.Runtime` en el path `/org/hermes/Runtime`.

Seguridad (CWE-290 / CWE-862 / G1):
  - El `sender_uid` lo resuelve este adapter llamando a `GetConnectionUnixUser`
    sobre `org.freedesktop.DBus` con el `msg.sender` (unique bus name) del
    mensaje entrante. NUNCA se acepta el UID de un argumento del mensaje.
  - Fail-closed: si la resolución del UID falla, la operación se deniega.
  - El wiring puro (DbusRuntimeServiceWiring) recibe el UID ya verificado.

Arquitectura:
  - `Runtime1ServiceInterface` es la clase exportada como ServiceInterface
    dbus-fast. Define los métodos con sus firmas D-Bus exactas (CTRL-P1-2).
  - El sender_uid se resuelve ANTES de llamar al wiring, via un message handler
    que intercepta las llamadas a los métodos mutadores.
  - Los métodos read-only (GetQueueStatus/ListPending/GetTaskStatus) no
    requieren resolución de UID pero sí pasan por el mismo flujo.

Introspection XML: generado automáticamente por ServiceInterface.introspect()
a partir de las anotaciones de tipo D-Bus de cada método.

Nota sobre testing:
  Runtime1ServiceInterface puede instanciarse SIN bus para introspection tests.
  El bus real solo se necesita para `DbusRuntimeAdapter.start()`.
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import TYPE_CHECKING, Callable
from uuid import UUID

from dbus_fast import DBusError, Variant
from dbus_fast.service import ServiceInterface, method, signal

if TYPE_CHECKING:
    from dbus_fast.aio import MessageBus

    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        DbusRuntimeServiceWiring,
    )

logger = logging.getLogger("hermes.agents_os.dbus_fast_adapter")

# ---------------------------------------------------------------------------
# Input validation constants (Fix-10)
# ---------------------------------------------------------------------------

_MAX_FIELD_LEN = 4096       # generic string field cap
_MAX_JSON_BYTES = 65_536    # 64 KiB — reject payloads larger than this

# Allow-list of top-level keys accepted in an agent draft (Fix-10).
# DEBE coincidir con las claves que lee draft_from_dict() (agents/application/
# serialization.py); si no, un draft válido (con instructions/persona) se
# rechaza en la frontera. El vocabulario real del dominio Agent es
# role/register/primary_mission/instructions/golden_rules/etc.
_AGENT_DRAFT_ALLOWED_KEYS = frozenset({
    "name", "role", "register", "primary_mission", "instructions",
    "color", "language", "golden_rules", "forbidden_phrases",
    "autonomy_level", "agent_id",
})

# Allow-list of keys accepted in a house rule (Fix-10).
_HOUSE_RULE_ALLOWED_KEYS = frozenset({
    "kind", "value", "description", "priority", "enabled",
})

# ContextVar that carries the D-Bus unique sender name for the current message.
#
# Why ContextVar, not a shared instance attribute (the original approach):
#   dbus-fast dispatches async methods via asyncio.ensure_future(), which copies
#   the current context at call time (Python ≥3.7 guarantee).  Because
#   _process_message() runs the user message handlers *synchronously* before
#   calling the method handler that fires ensure_future(), setting this
#   ContextVar in the user handler means each Task captures the correct sender
#   in its own context snapshot — even when two callers are interleaved on the
#   event loop.  A plain shared attribute races: Task A can be suspended at
#   await _get_connection_unix_user() while the handler for message B overwrites
#   _current_sender, causing A to resolve B's UID instead of its own.
#   (CWE-362/367, TOCTOU — security checkpoint US2)
_CURRENT_SENDER_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hermes_dbus_current_sender", default=None
)

_WELL_KNOWN_NAME = "org.hermes.Runtime"
_OBJECT_PATH = "/org/hermes/Runtime"
_INTERFACE_NAME = "org.hermes.Runtime1"


# ---------------------------------------------------------------------------
# Input validation helpers (Fix-10)
# ---------------------------------------------------------------------------


class DbusInputValidationError(ValueError):
    """Raised when a D-Bus JSON payload fails boundary validation.

    Converted to a clean D-Bus error reply by the caller — no traceback leaks.
    """


def _parse_json_bounded(raw: str, *, field_name: str) -> dict:
    """Parse JSON string with size cap and type guard (Fix-10)."""
    if len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
        raise DbusInputValidationError(
            f"{field_name}: payload excede el límite de {_MAX_JSON_BYTES} bytes"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DbusInputValidationError(
            f"{field_name}: JSON inválido — {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise DbusInputValidationError(
            f"{field_name}: se esperaba un objeto JSON, recibido {type(parsed).__name__}"
        )
    return parsed


def _assert_allowed_keys(data: dict, *, allowed: frozenset[str], field_name: str) -> None:
    """Reject unknown top-level keys (Fix-10)."""
    unknown = set(data.keys()) - allowed
    if unknown:
        raise DbusInputValidationError(
            f"{field_name}: claves no permitidas: {sorted(unknown)}"
        )


def _assert_string_lengths(data: dict, *, field_name: str) -> None:
    """Reject any top-level string value exceeding _MAX_FIELD_LEN (Fix-10)."""
    for k, v in data.items():
        if isinstance(v, str) and len(v) > _MAX_FIELD_LEN:
            raise DbusInputValidationError(
                f"{field_name}.{k}: valor demasiado largo ({len(v)} chars, máx {_MAX_FIELD_LEN})"
            )


def _parse_and_validate_agent_draft_json(raw: str) -> dict:
    """Parse + validate an agent draft JSON payload at the trust boundary (Fix-10)."""
    data = _parse_json_bounded(raw, field_name="CreateAgent/draft_json")
    _assert_allowed_keys(data, allowed=_AGENT_DRAFT_ALLOWED_KEYS, field_name="draft_json")
    _assert_string_lengths(data, field_name="draft_json")
    return data


def _parse_and_validate_house_rule_json(raw: str) -> dict:
    """Parse + validate a house rule JSON payload at the trust boundary (Fix-10).

    Requires the 'kind' key — indexing rule['kind'] in the wiring without a guard
    raises KeyError/DoS (Fix-10 regression target).
    """
    data = _parse_json_bounded(raw, field_name="SetAgentHouseRule/rule_json")
    _assert_allowed_keys(data, allowed=_HOUSE_RULE_ALLOWED_KEYS, field_name="rule_json")
    _assert_string_lengths(data, field_name="rule_json")
    if "kind" not in data:
        raise DbusInputValidationError(
            "SetAgentHouseRule/rule_json: campo 'kind' obligatorio ausente"
        )
    if not isinstance(data["kind"], str):
        raise DbusInputValidationError(
            "SetAgentHouseRule/rule_json: 'kind' debe ser una cadena"
        )
    return data


class Runtime1ServiceInterface(ServiceInterface):
    """Interfaz D-Bus org.hermes.Runtime1 exportada por dbus-fast.

    Contrato: dbus_runtime_iface_v1.md (source of truth).
    Firmas D-Bus definidas por anotaciones de string (dbus-fast convención).

    IMPORTANTE: el sender_uid NO se resuelve en estos métodos. Lo resuelve
    `DbusRuntimeAdapter._resolve_sender_uid()` ANTES de invocar el wiring.
    Este objeto se pasa al wiring ya con el UID verificado.
    """

    def __init__(self, *, wiring: DbusRuntimeServiceWiring) -> None:
        super().__init__(_INTERFACE_NAME)
        self._wiring = wiring
        self._bus: MessageBus | None = None

    # ------------------------------------------------------------------
    # Verbos mutadores (authZ en el adapter via sender_uid del bus)
    # ------------------------------------------------------------------

    @method()
    async def Enqueue(  # noqa: N802
        self, trigger_kind: "s", text: "s", priority: "i", dedup_key: "s", conversation_id: "s", operator_token: "s"  # noqa: F821,UP037
    ) -> "ss":  # noqa: F821,UP037
        """Encola un WorkItem.

        enqueued_by se inyecta server-side (= operador verificado, CTRL-P1-3).
        `enqueued_by` NO es parámetro — sería spoofeable.
        operator_token: vacío "" para llamadas DIRECTAS de un operador autorizado
        (sender_uid ∈ authorized_uids); OBLIGATORIO y firmado para llamadas PROXY
        del shell-server (confused-deputy remediation, CWE-862) — de él se extrae
        el operator_id real (el humano), nunca el uid del proxy.
        conversation_id viaja como "s" ("" = None); un chat_message lo exige
        (invariante I5 del esquema agent_tasks).
        Devuelve (task_id, stream_path).
        """
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.enqueue(
            trigger_kind=trigger_kind,
            text=text,
            priority=priority,
            dedup_key=dedup_key or None,
            sender_uid=sender_uid,
            conversation_id=conversation_id or None,
            operator_token=operator_token or None,
        )
        return str(result.task_id), result.stream_path

    @method()
    async def Pause(self, reason: "s") -> "b":  # noqa: N802,F821,UP037
        """Kill-switch. by = UID del bus (CTRL-P1-1)."""
        sender_uid = await self._resolve_current_sender_uid()
        await self._wiring.request_pause(reason=reason, sender_uid=sender_uid)
        return True

    @method()
    async def Resume(self) -> "b":  # noqa: N802,F821,UP037
        """Reanuda. by = UID del bus."""
        sender_uid = await self._resolve_current_sender_uid()
        await self._wiring.request_resume(sender_uid=sender_uid)
        return True

    @method()
    async def Approve(self, proposal_id: "s") -> "s":  # noqa: N802,F821,UP037
        """HITL approve. approved_by = UID del bus. NO dispara run_cycle."""
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.approve_action(
            proposal_id=UUID(proposal_id),
            sender_uid=sender_uid,
        )
        return result.approval_token

    @method()
    async def Reject(self, proposal_id: "s", reason: "s") -> "b":  # noqa: N802,F821,UP037
        """HITL reject. rejected_by = UID del bus."""
        sender_uid = await self._resolve_current_sender_uid()
        await self._wiring.reject_action(
            proposal_id=UUID(proposal_id),
            reason=reason,
            sender_uid=sender_uid,
        )
        return True

    # ------------------------------------------------------------------
    # Métodos read-only (supervisión, CTRL-P1-5: solo metadatos)
    # ------------------------------------------------------------------

    @method()
    async def GetQueueStatus(self) -> "a{sv}":  # noqa: N802,F722,UP037
        """Snapshot read-only de la cola. No altera estado."""
        status = await self._wiring.get_queue_status()
        return _queue_status_to_dict(status)

    @method()
    async def ListPending(self, limit: "u") -> "a(ssis)":  # noqa: N802,F821,F722,UP037
        """Items PENDING por prioridad desc. Solo metadatos (CTRL-P1-5)."""
        tasks = await self._wiring.list_pending(limit=int(limit))
        return [
            (str(t.task_id), t.trigger_kind, t.priority, t.enqueued_at_iso)
            for t in tasks
        ]

    @method()
    async def ListHitlPending(self, limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Propuestas HITL pendientes de aprobación humana. Read-only (CTRL-P1-5).

        Devuelve JSON (lista de dicts) con: proposal_id, tool_name, justification,
        risk, created_at. El QML hace json.loads. Sin payload ni credenciales.
        """
        rows = await self._wiring.list_hitl_pending(limit=int(limit))
        return json.dumps(rows)

    @method()
    async def GetTaskStatus(self, task_id: "s") -> "a{sv}":  # noqa: N802,F821,F722,UP037
        """Estado de una tarea (CTRL-P1-5: metadatos, nunca payload)."""
        status = await self._wiring.get_task_status(task_id=UUID(task_id))
        return _task_status_to_dict(status)

    @method()
    async def ListConfiguredTasks(self, limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Tareas configuradas para el tablero (1 fila por trigger autorizado).

        Devuelve JSON (lista de dicts) — el cliente hace json.loads. Solo
        metadatos + última ejecución; sin payload ni credenciales (CTRL-P1-5).
        P3: incluye target_agent_id, task_instruction, one_shot, title.
        """
        rows = await self._wiring.list_configured_tasks(limit=int(limit))
        return json.dumps(rows)

    @method()
    async def CreateScheduledTask(self, draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Crea una tarea programada firmada (P3 — calendario per-agent).

        draft_json: {title, target_agent_id, task_instruction, cron, one_shot, risk_ceiling}
        Devuelve JSON {ok, trigger_id} o {ok: false, error}.
        authZ: operador (sender_uid del bus, CWE-862).
        """
        try:
            sender_uid = await self._resolve_current_sender_uid()
            result = await self._wiring.create_scheduled_task(
                draft_json=draft_json,
                sender_uid=sender_uid,
            )
            return json.dumps(result)
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def DeleteScheduledTask(self, trigger_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Revoca (soft-delete) una tarea programada. Preserva auditoría.

        Marca enabled=0 + revoked_at. No hay borrado físico de la fila.
        Devuelve JSON {ok} o {ok: false, error}.
        authZ: operador (sender_uid del bus, CWE-862).
        """
        try:
            sender_uid = await self._resolve_current_sender_uid()
            result = await self._wiring.delete_scheduled_task(
                trigger_id=trigger_id,
                sender_uid=sender_uid,
            )
            return json.dumps(result)
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def SetScheduledTaskEnabled(self, trigger_id: "s", enabled: "b") -> "s":  # noqa: N802,F821,UP037
        """Toggle del kill-switch de una tarea programada.

        enabled=true → reactiva; enabled=false → suspende (preserva I11).
        Devuelve JSON {ok} o {ok: false, error}.
        authZ: operador (sender_uid del bus, CWE-862).
        """
        try:
            sender_uid = await self._resolve_current_sender_uid()
            result = await self._wiring.set_scheduled_task_enabled(
                trigger_id=trigger_id,
                enabled=bool(enabled),
                sender_uid=sender_uid,
            )
            return json.dumps(result)
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def ListRecentTasks(self, limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Actividad reciente (work items por estado). JSON → cliente json.loads."""
        rows = await self._wiring.list_recent_tasks(limit=int(limit))
        return json.dumps(rows)

    # ------------------------------------------------------------------
    # Gobernanza del roster multi-agente (JSON sobre D-Bus, autoría sender_uid)
    # ------------------------------------------------------------------
    @method()
    async def ListAgents(self) -> "s":  # noqa: N802,F821,UP037
        """Roster de agentes (read-only). JSON → el cliente hace json.loads."""
        return json.dumps(self._wiring.list_agents())

    @method()
    async def GetActiveAgent(self) -> "s":  # noqa: N802,F821,UP037
        """agent_id del agente activo (read-only)."""
        return self._wiring.get_active_agent()

    @method()
    async def SetActiveAgent(self, agent_id: "s") -> "b":  # noqa: N802,F821,UP037
        """Marca el agente activo. by = UID del bus (CTRL-P1-3)."""
        sender_uid = await self._resolve_current_sender_uid()
        await self._wiring.set_active_agent(agent_id=agent_id, sender_uid=sender_uid)
        return True

    @method()
    async def CreateAgent(self, draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Crea un agente desde un draft JSON. Devuelve el agente creado (JSON).

        Fix-10: valida campos JSON en la frontera antes de pasar al wiring.
        DbusInputValidationError → clean D-Bus error reply (no traceback leak).
        """
        from hermes.agents.application.serialization import draft_from_dict  # noqa: PLC0415

        try:
            sender_uid = await self._resolve_current_sender_uid()
            raw = _parse_and_validate_agent_draft_json(draft_json)
            draft = draft_from_dict(raw)
            agent = await self._wiring.create_agent(draft=draft, sender_uid=sender_uid)
            return json.dumps(agent)
        except DbusInputValidationError as exc:
            raise DBusError("org.hermes.Error.InvalidInput", str(exc)) from exc
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def UpdateAgent(self, agent_id: "s", draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Actualiza un agente. Devuelve el agente actualizado (JSON)."""
        from hermes.agents.application.serialization import draft_from_dict  # noqa: PLC0415

        sender_uid = await self._resolve_current_sender_uid()
        draft = draft_from_dict(json.loads(draft_json))
        agent = await self._wiring.update_agent(
            agent_id=agent_id, draft=draft, sender_uid=sender_uid
        )
        return json.dumps(agent)

    @method()
    async def DeleteAgent(self, agent_id: "s") -> "b":  # noqa: N802,F821,UP037
        """Elimina un agente (no el 'default' ni el último). by = UID del bus."""
        sender_uid = await self._resolve_current_sender_uid()
        await self._wiring.delete_agent(agent_id=agent_id, sender_uid=sender_uid)
        return True

    # ------------------------------------------------------------------
    # Gobernanza de skills (JSON sobre D-Bus, autoría sender_uid / P0-1)
    # ------------------------------------------------------------------

    @method()
    async def ListSkills(self) -> "s":  # noqa: N802,F821,UP037
        """Lista de skills (read-only). JSON → el cliente hace json.loads."""
        return json.dumps(self._wiring.list_skills())

    @method()
    async def PromoteSkill(self, package_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Promueve skill VALIDATED → AUTONOMOUS. by = UID del bus."""
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.promote_skill(
            package_id=package_id, sender_uid=sender_uid
        )
        return json.dumps(result)

    @method()
    async def DeprecateSkill(self, package_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Depreca una skill. by = UID del bus."""
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.deprecate_skill(
            package_id=package_id, sender_uid=sender_uid
        )
        return json.dumps(result)

    @method()
    async def SignComposioSkill(self, draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Crea y firma una Composio skill desde un draft JSON.

        draft_json: {"skill_name": str, "toolkit_slug": str, "intent_text": str}
        Devuelve el SkillPackageDTO serializado (JSON). by = UID del bus.
        """
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.sign_composio_skill(
            draft_json=draft_json, sender_uid=sender_uid
        )
        return json.dumps(result)

    # ------------------------------------------------------------------
    # GATE 0 / M1 — Providers OS-nativos (D-Bus, ya no HTTP). JSON sobre el bus.
    # Lecturas: sin authZ. Mutadores: by = sender_uid del operador (directo).
    # ------------------------------------------------------------------

    @method()
    async def ListProviders(self) -> "s":  # noqa: N802,F821,UP037
        """Lista de providers (read-only)."""
        return json.dumps(self._wiring.list_providers())

    @method()
    async def GetActiveProvider(self) -> "s":  # noqa: N802,F821,UP037
        """Provider activo, o {} (read-only)."""
        return json.dumps(self._wiring.get_active_provider())

    @method()
    async def AddProvider(self, draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Crea provider. draft: {kind, alias, default_model, base_url, api_key, set_active}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            self._wiring.add_provider(draft_json=draft_json, sender_uid=sender_uid)
        )

    @method()
    async def UpdateProvider(self, provider_id: "s", draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Actualiza alias/default_model/base_url/enabled/api_key."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            self._wiring.update_provider(
                provider_id=provider_id, draft_json=draft_json, sender_uid=sender_uid
            )
        )

    @method()
    async def DeleteProvider(self, provider_id: "s") -> "b":  # noqa: N802,F821,UP037
        sender_uid = await self._resolve_current_sender_uid()
        return self._wiring.delete_provider(
            provider_id=provider_id, sender_uid=sender_uid
        )

    @method()
    async def SetActiveProvider(self, provider_id: "s") -> "s":  # noqa: N802,F821,UP037
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            self._wiring.set_active_provider(
                provider_id=provider_id, sender_uid=sender_uid
            )
        )

    @method()
    async def TestProvider(self, provider_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Valida el provider por el runtime REAL (Nous) en el daemon. {ok, error}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            await self._wiring.test_provider(
                provider_id=provider_id, sender_uid=sender_uid
            )
        )

    # ------------------------------------------------------------------
    # GATE 0 / M2 — Conversaciones (chat). Lecturas sin authZ; delete con
    # sender_uid del bus (CWE-862). El daemon es dueño del store.
    # ------------------------------------------------------------------

    @method()
    async def ListConversations(self, agent_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Recientes (read-only). agent_id='' → todas."""
        return json.dumps(self._wiring.list_conversations(agent_id=agent_id or None))

    @method()
    async def GetConversation(self, conversation_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Detalle con mensajes (read-only). {} si no existe."""
        return json.dumps(self._wiring.get_conversation(conversation_id=conversation_id))

    @method()
    async def DeleteConversation(self, conversation_id: "s") -> "b":  # noqa: N802,F821,UP037
        sender_uid = await self._resolve_current_sender_uid()
        return self._wiring.delete_conversation(
            conversation_id=conversation_id, sender_uid=sender_uid
        )

    # ------------------------------------------------------------------
    # GATE 0 / M7 — Cuenta de SO (onboarding). Muta → sender_uid del bus.
    # ------------------------------------------------------------------

    @method()
    async def StageAccount(self, username: "s", password: "s") -> "s":  # noqa: N802,F821,UP037
        """Deja staged las credenciales de la cuenta de SO. {staged, error}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            self._wiring.stage_account(
                username=username, password=password, sender_uid=sender_uid
            )
        )

    @method()
    async def SetLocaleKeymap(self, locale: "s", keymap: "s") -> "s":  # noqa: N802,F821,UP037
        """Stagea idioma + teclado del SO (hermes-locale-apply los aplica). {staged, error}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            self._wiring.set_locale_keymap(
                locale=locale, keymap=keymap, sender_uid=sender_uid
            )
        )

    # ------------------------------------------------------------------
    # Composio (SO-nativo, Principio 0): consumo DINÁMICO de Composio Cloud
    # por el daemon. Lecturas sin authZ; mutadores con sender_uid del bus.
    # ------------------------------------------------------------------

    @method()
    async def ListNativeProviders(self) -> "s":  # noqa: N802,F821,UP037
        """Catálogo nativo de providers de Hermes (hermes_cli, 37+, suscripciones)."""
        return json.dumps(self._wiring.list_native_providers())

    @method()
    async def ConfigureNativeProvider(self, draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Configura un provider NATIVO de hermes_cli (api-key) por su id real.

        draft: {provider_id, api_key, model, base_url}. Escribe .env + config.yaml
        del HERMES_HOME — el motor lo resuelve directo. authZ operador.
        """
        import asyncio as _asyncio  # noqa: PLC0415
        from functools import partial  # noqa: PLC0415

        sender_uid = await self._resolve_current_sender_uid()
        d = json.loads(draft_json) if draft_json else {}
        result = await _asyncio.get_running_loop().run_in_executor(
            None,
            partial(
                self._wiring.configure_native_provider,
                provider_id=str(d.get("provider_id", "")),
                api_key=str(d.get("api_key", "")),
                model=str(d.get("model", "")),
                base_url=str(d.get("base_url", "")),
                sender_uid=sender_uid,
            ),
        )
        return json.dumps(result)

    @method()
    async def GetNativeActive(self) -> "s":  # noqa: N802,F821,UP037
        """Provider nativo activo según config.yaml ({} si ninguno)."""
        return json.dumps(self._wiring.get_native_active())

    @method()
    async def StartProviderOauth(self, provider_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Inicia device-code OAuth de una suscripción (Nous Portal).

        Devuelve JSON {session_id, user_code, verification_url, expires_in,
        poll_interval} o {error}. La petición HTTP del device-code corre en
        executor — el event loop del daemon NO se bloquea (el chat sigue vivo).
        authZ: sender_uid del bus (operador), igual que AddProvider.
        """
        import asyncio as _asyncio  # noqa: PLC0415
        from functools import partial  # noqa: PLC0415

        sender_uid = await self._resolve_current_sender_uid()
        result = await _asyncio.get_running_loop().run_in_executor(
            None,
            partial(
                self._wiring.start_provider_oauth,
                provider_id=provider_id,
                sender_uid=sender_uid,
            ),
        )
        return json.dumps(result)

    @method()
    async def GetProviderOauthStatus(self, session_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Estado del flow OAuth: {status: pending|approved|error|unknown}."""
        return json.dumps(
            self._wiring.get_provider_oauth_status(session_id=session_id)
        )

    # ── Skill Hub de Hermes (búsqueda + install multi-fuente) ───────────

    @method()
    async def SearchSkillsHub(self, query: "s", source: "s", limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Busca skills en el hub (red, ≤30s) — corre en executor.

        Devuelve {query_id, results, cancelled}. El caller puede cancelar
        la query en vuelo con CancelSkillsHubSearch(query_id).
        """
        import asyncio as _asyncio  # noqa: PLC0415
        import uuid as _uuid  # noqa: PLC0415
        from functools import partial  # noqa: PLC0415
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            _hub_search_cleanup,
            _hub_search_register,
        )

        query_id = _uuid.uuid4().hex
        _hub_search_register(query_id)
        try:
            payload = await _asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    self._wiring.search_skills_hub,
                    query=query,
                    source=source,
                    limit=int(limit),
                    query_id=query_id,
                ),
            )
        finally:
            _hub_search_cleanup(query_id)
        return json.dumps(payload)

    @method()
    async def CancelSkillsHubSearch(self, query_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Cancela una búsqueda en vuelo. Devuelve {ok: bool}."""
        return json.dumps(
            self._wiring.cancel_skills_hub_search(query_id=query_id)
        )

    @method()
    async def ListHubSkills(self) -> "s":  # noqa: N802,F821,UP037
        """Skills del hub instaladas (lockfile). Read-only."""
        return json.dumps(self._wiring.list_hub_skills())

    @method()
    async def InstallHubSkill(self, identifier: "s") -> "s":  # noqa: N802,F821,UP037
        """Instala una skill del hub (thread + op_id sondeable)."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(self._wiring.install_hub_skill(
            identifier=identifier, sender_uid=sender_uid))

    @method()
    async def UninstallHubSkill(self, name: "s") -> "s":  # noqa: N802,F821,UP037
        """Desinstala una skill del hub. {op_id}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(self._wiring.uninstall_hub_skill(
            name=name, sender_uid=sender_uid))

    @method()
    async def GetHubOpStatus(self, op_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Estado de una operación del hub: {status}."""
        return json.dumps(self._wiring.get_hub_op_status(op_id=op_id))

    # ── Package Store (apps Linux reales: Flatpak + RPM) ─────────────────

    @method()
    async def ListInstalledPackages(self, source: "s") -> "s":  # noqa: N802,F821,UP037
        """Paquetes instalados. source = 'flatpak' | 'rpm'. Read-only, bloqueante."""
        import asyncio as _asyncio  # noqa: PLC0415
        from functools import partial  # noqa: PLC0415
        results = await _asyncio.get_running_loop().run_in_executor(
            None,
            partial(self._wiring.list_installed_packages, source=source),
        )
        return json.dumps(results)

    @method()
    async def SearchPackages(self, query: "s", source: "s") -> "s":  # noqa: N802,F821,UP037
        """Busca en Flathub + dnf. source = 'flatpak' | 'rpm' | 'all'. Read-only, bloqueante."""
        import asyncio as _asyncio  # noqa: PLC0415
        from functools import partial  # noqa: PLC0415
        results = await _asyncio.get_running_loop().run_in_executor(
            None,
            partial(self._wiring.search_packages, query=query, source=source),
        )
        return json.dumps(results)

    @method()
    async def InstallPackage(self, source: "s", package_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Inicia instalación async. authZ operador. Devuelve {op_id} o {error}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(self._wiring.install_package(
            source=source, package_id=package_id, sender_uid=sender_uid
        ))

    @method()
    async def UninstallPackage(self, source: "s", package_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Inicia desinstalación async. authZ operador. Devuelve {op_id} o {error}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(self._wiring.uninstall_package(
            source=source, package_id=package_id, sender_uid=sender_uid
        ))

    @method()
    async def GetPkgOpStatus(self, op_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Estado de install/uninstall: {op_id, status, log_tail, error_message}."""
        return json.dumps(self._wiring.get_pkg_op_status(op_id=op_id))

    # ── MCP Apps (gestión de servidores MCP, SO-nativo) ──────────────────

    @method()
    async def ListMcpServers(self) -> "s":  # noqa: N802,F821,UP037
        """Servidores MCP configurados + salud + nº de tools (read-only)."""
        return json.dumps(await self._wiring.list_mcp_servers())

    @method()
    async def AddMcpServer(self, draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Configura+conecta un servidor MCP stdio. {ok, tool_count|error}.

        authZ operador via sender_uid; runner restringido a la allowlist
        (npx/uvx/node/python3) en el wiring.
        """
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.add_mcp_server(
            draft_json=draft_json, sender_uid=sender_uid
        )
        return json.dumps(result)

    @method()
    async def RemoveMcpServer(self, server_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Desconecta y elimina un servidor MCP configurado. {ok}."""
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.remove_mcp_server(
            server_id=server_id, sender_uid=sender_uid
        )
        return json.dumps(result)

    @method()
    async def SearchMcpRegistry(self, query: "s", limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Busca en el MCP Registry oficial (read-only, sin authZ)."""
        result = await self._wiring.search_mcp_registry(query=query, limit=int(limit) or 20)
        return json.dumps(result)

    @method()
    async def GetComposioStatus(self) -> "s":  # noqa: N802,F821,UP037
        """{configured, entity_id} — nunca expone la key (read-only)."""
        return json.dumps(self._wiring.get_composio_status())

    @method()
    async def SetComposioApiKey(self, api_key: "s") -> "s":  # noqa: N802,F821,UP037
        """Guarda la key de Composio en el vault del daemon. {ok, error}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            await self._wiring.set_composio_api_key(api_key=api_key, sender_uid=sender_uid)
        )

    @method()
    async def SetWebSearchApiKey(self, provider: "s", api_key: "s") -> "s":  # noqa: N802,F821,UP037
        """Configura la API key de un backend de búsqueda web (Brave/Tavily/Exa). {ok,error}."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            await self._wiring.set_web_search_api_key(
                provider=provider, api_key=api_key, sender_uid=sender_uid
            )
        )

    @method()
    async def GetWebSearchStatus(self) -> "s":  # noqa: N802,F821,UP037
        """Qué backends de búsqueda web tienen key (read-only)."""
        return json.dumps(self._wiring.get_web_search_status())

    @method()
    async def ListComposioApps(self) -> "s":  # noqa: N802,F821,UP037
        """Catálogo dinámico de toolkits desde Composio Cloud (read-only)."""
        return json.dumps(await self._wiring.list_composio_apps())

    @method()
    async def ListComposioConnections(self) -> "s":  # noqa: N802,F821,UP037
        """Cuentas conectadas del usuario (dinámico, read-only). Incluye alias."""
        return json.dumps(await self._wiring.list_composio_connections())

    @method()
    async def SetComposioConnectionAlias(  # noqa: N802
        self, connection_id: "s", alias: "s"  # noqa: F821,UP037
    ) -> "b":  # noqa: F821,UP037
        """Asigna alias humano a una cuenta Composio. Requiere authZ."""
        sender_uid = await self._resolve_current_sender_uid()
        return await self._wiring.set_composio_connection_alias(
            connected_account_id=connection_id,
            alias=alias,
            sender_uid=sender_uid,
        )

    @method()
    async def BindComposioConnectionToAgent(  # noqa: N802
        self, agent_id: "s", connection_id: "s", toolkit_slug: "s"  # noqa: F821,UP037
    ) -> "b":  # noqa: F821,UP037
        """Asigna una cuenta Composio a un agente. Idempotente. Requiere authZ."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.bind_composio_connection_to_agent(
            agent_id=agent_id,
            connected_account_id=connection_id,
            toolkit_slug=toolkit_slug,
            tenant_id=tenant_id,
            sender_uid=sender_uid,
        )

    @method()
    async def UnbindComposioConnectionFromAgent(  # noqa: N802
        self, agent_id: "s", connection_id: "s"  # noqa: F821,UP037
    ) -> "b":  # noqa: F821,UP037
        """Revoca el binding de una cuenta Composio de un agente. Requiere authZ."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.unbind_composio_connection_from_agent(
            agent_id=agent_id,
            connected_account_id=connection_id,
            tenant_id=tenant_id,
            sender_uid=sender_uid,
        )

    @method()
    async def ListAgentComposioConnections(  # noqa: N802
        self, agent_id: "s"  # noqa: F821,UP037
    ) -> "s":  # noqa: F821,UP037
        """IDs de cuentas Composio asignadas a un agente (read-only). JSON array."""
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return json.dumps(self._wiring.list_agent_composio_connections(agent_id, tenant_id))

    @method()
    async def ConnectComposioApp(self, toolkit_slug: "s") -> "s":  # noqa: N802,F821,UP037
        """Inicia OAuth Connect Link. {ok, redirect_url} para abrir en el navegador."""
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            await self._wiring.connect_composio_app(
                toolkit_slug=toolkit_slug, sender_uid=sender_uid
            )
        )

    # ------------------------------------------------------------------
    # Gobernanza de plataformas (feature 010, Principio 0)
    # Lecturas: sin authZ. Mutadores: by = sender_uid del bus (CWE-862).
    # ------------------------------------------------------------------

    # --- Lecturas de supervisión (read-only, sin authZ) ---

    @method()
    async def ListPlatformModels(self) -> "s":  # noqa: N802,F821,UP037
        """Lista de PlatformModels del tenant activo. JSON → cliente json.loads."""
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return json.dumps(self._wiring.list_platform_models(tenant_id))

    @method()
    async def GetPlatformModelSummary(self, model_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Resumen de un modelo (áreas, entidades, reglas, zonas). JSON."""
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return json.dumps(self._wiring.get_platform_model_summary(model_id, tenant_id))

    @method()
    async def ListAgentCapabilities(self, agent_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Capacidades asignadas a un agente. JSON."""
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return json.dumps(self._wiring.list_agent_capabilities(agent_id, tenant_id))

    @method()
    async def ListModelGaps(self, model_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Lagunas abiertas/cubiertas de un modelo (metadatos, sin PII). JSON."""
        return json.dumps(self._wiring.list_model_gaps(model_id))

    # --- Mutadores de gobernanza de plataformas (by = sender_uid) ---

    @method()
    async def StartPlatformTour(  # noqa: N802
        self, site_ref: "s", origin: "s", modality: "s"  # noqa: F821,UP037
    ) -> "s":  # noqa: F821,UP037
        """Abre un recorrido de aprendizaje. Devuelve tour_id."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.start_platform_tour(
            site_ref=site_ref,
            origin=origin,
            modality=modality,
            tenant_id=tenant_id,
            sender_uid=sender_uid,
        )

    @method()
    async def ClosePlatformTour(self, tour_id: "s") -> "s":  # noqa: N802,F821,UP037
        """Cierra el tour y compila el modelo. Devuelve model_json."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.close_platform_tour(
            tour_id=tour_id, tenant_id=tenant_id, sender_uid=sender_uid
        )

    @method()
    async def ConfirmPlatformModel(  # noqa: N802
        self, model_id: "s", corrections_json: "s"  # noqa: F821,UP037
    ) -> "s":  # noqa: F821,UP037
        """Aplica correcciones y confirma el modelo (provisional→aprendida). JSON."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        corrections = json.loads(corrections_json) if corrections_json else []
        result = await self._wiring.confirm_platform_model(
            model_id=model_id,
            tenant_id=tenant_id,
            corrections=corrections,
            sender_uid=sender_uid,
        )
        return json.dumps(result)

    @method()
    async def EnablePlatformModel(self, model_id: "s") -> "b":  # noqa: N802,F821,UP037
        """aprendida → habilitada. Fail-closed si needs_label."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.enable_platform_model(
            model_id=model_id, tenant_id=tenant_id, sender_uid=sender_uid
        )

    @method()
    async def DisablePlatformModel(self, model_id: "s") -> "b":  # noqa: N802,F821,UP037
        """habilitada → aprendida."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.disable_platform_model(
            model_id=model_id, tenant_id=tenant_id, sender_uid=sender_uid
        )

    @method()
    async def DeprecatePlatformModel(self, model_id: "s") -> "b":  # noqa: N802,F821,UP037
        """Deprecar/olvidar (derecho al borrado GDPR)."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.deprecate_platform_model(
            model_id=model_id, tenant_id=tenant_id, sender_uid=sender_uid
        )

    # --- Capability binding (mutators, by = sender_uid) ---

    @method()
    async def BindCapabilityToAgent(  # noqa: N802
        self,
        agent_id: "s",  # noqa: F821,UP037
        capability_kind: "s",  # noqa: F821,UP037
        capability_id: "s",  # noqa: F821,UP037
        capability_version: "s",  # noqa: F821,UP037
    ) -> "s":  # noqa: F821,UP037
        """Asigna una capacidad a un agente. Idempotente. Devuelve binding_json."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        result = await self._wiring.bind_capability_to_agent(
            agent_id=agent_id,
            capability_kind=capability_kind,
            capability_id=capability_id,
            capability_version=capability_version,
            tenant_id=tenant_id,
            sender_uid=sender_uid,
        )
        return json.dumps(result)

    @method()
    async def UnbindCapabilityFromAgent(  # noqa: N802
        self,
        agent_id: "s",  # noqa: F821,UP037
        capability_kind: "s",  # noqa: F821,UP037
        capability_id: "s",  # noqa: F821,UP037
    ) -> "b":  # noqa: F821,UP037
        """Desasigna una capacidad de un agente. Idempotente."""
        sender_uid = await self._resolve_current_sender_uid()
        tenant_id = getattr(self._wiring, "_tenant_id", "")
        return await self._wiring.unbind_capability_from_agent(
            agent_id=agent_id,
            capability_kind=capability_kind,
            capability_id=capability_id,
            tenant_id=tenant_id,
            sender_uid=sender_uid,
        )

    @method()
    async def SetAgentHouseRule(  # noqa: N802
        self, agent_id: "s", model_id: "s", rule_json: "s"  # noqa: F821,UP037
    ) -> "b":  # noqa: F821,UP037
        """Añade/actualiza una regla-de-la-casa por-agente (overlay, FR-037).

        Fix-10: valida campos JSON en la frontera antes de pasar al wiring.
        DbusInputValidationError → clean D-Bus error reply (no traceback leak).
        """
        try:
            sender_uid = await self._resolve_current_sender_uid()
            tenant_id = getattr(self._wiring, "_tenant_id", "")
            rule = _parse_and_validate_house_rule_json(rule_json)
            return await self._wiring.set_agent_house_rule(
                agent_id=agent_id,
                model_id=model_id,
                rule=rule,
                tenant_id=tenant_id,
                sender_uid=sender_uid,
            )
        except DbusInputValidationError as exc:
            raise DBusError("org.hermes.Error.InvalidInput", str(exc)) from exc
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    # ------------------------------------------------------------------
    # T017 — Desktop overlay methods (spec 014-agentic-desktop)
    # ------------------------------------------------------------------

    @method()
    async def OpenOverlay(self) -> "b":  # noqa: N802,F821,UP037
        """Bring the Hermes overlay to front (idempotent, no effector).

        The gnome-shell extension calls this to check daemon liveness and
        request the overlay process to focus. The wiring emits a log entry
        and returns True. The OverlayRequested signal is emitted here so
        that the overlay process (listening on D-Bus) gets notified.
        No run_cycle, no broker, no state mutation (Constitution 0.1).
        """
        sender_uid = await self._resolve_current_sender_uid()
        result = self._wiring.open_overlay(sender_uid=sender_uid)
        # Signal to overlay process(es) subscribed on the bus.
        self.OverlayRequested()
        return result

    @method()
    async def EnqueueFromOverlay(  # noqa: N802
        self, text: "s", conversation_id: "s"  # noqa: F821,UP037
    ) -> "ss":  # noqa: F821,UP037
        """Overlay chat → enqueue. Returns (task_id, stream_path).

        Delegates to the existing enqueue path (trigger_kind=chat_message)
        so rate-limit / PII tokenization / audit are applied identically.
        Authorship is derived from sender_uid (CWE-862 — no confused-deputy
        here: the overlay runs as the operator uid 1000 directly).
        conversation_id "" → None (no conversation scoping).
        """
        sender_uid = await self._resolve_current_sender_uid()
        result = await self._wiring.enqueue_from_overlay(
            text=text,
            conversation_id=conversation_id or None,
            sender_uid=sender_uid,
        )
        return str(result.task_id), result.stream_path

    @method()
    async def RequestContextSnapshot(self) -> "s":  # noqa: N802,F821,UP037
        """Return JSON snapshot of active app + focused element (read-only).

        Composed by ContextSnapshotComposer:
          - AT-SPI focused application name.
          - Window title (PII-eligible — tokenize before LLM).
          - Screenshot flag + bytes only when SCREEN_CAPTURE consent active.
        Never persisted, never logged (Constitution III).
        No broker, no effector (read-only by design).
        """
        sender_uid = await self._resolve_current_sender_uid()
        return self._wiring.request_context_snapshot(sender_uid=sender_uid)

    @method()
    async def GetAuditChainHead(self) -> "s":  # noqa: N802,F821,UP037
        """Return JSON head of the audit hash-chain (read-only).

        Used by the Security/Audit capability app to display chain state.
        {entry_id, head_hash, integrity, captured_at}
        No authZ required — read-only metadata, same policy as list_*.
        sender_uid is resolved to enforce identity logging.
        """
        sender_uid = await self._resolve_current_sender_uid()
        return self._wiring.get_audit_chain_head(sender_uid=sender_uid)

    # ------------------------------------------------------------------
    # spec 014 increment 3 — FR-013 operator consent control (D-Bus)
    # GrantConsent / RevokeConsent: mutators by sender_uid (CWE-862).
    # ListConsents: read-only, no authZ.
    # human_operator_id ALWAYS resolved server-side from sender_uid.
    # ------------------------------------------------------------------

    @method()
    async def GrantConsent(  # noqa: N802
        self, capability: "s", scope: "s"  # noqa: F821,UP037
    ) -> "s":  # noqa: F821,UP037
        """Grant capability consent for the calling operator (FR-013).

        capability: Capability enum value string (e.g. "documents").
        scope: "session" | "once" | "persistent".
        human_operator_id resolved server-side from sender_uid — NEVER from payload.
        Returns JSON: consent dict on success, {"error": reason} on failure.
        """
        try:
            sender_uid = await self._resolve_current_sender_uid()
            result = self._wiring.grant_consent(
                capability=capability,
                scope=scope,
                sender_uid=sender_uid,
            )
            return json.dumps(result)
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def RevokeConsent(self, capability: "s") -> "s":  # noqa: N802,F821,UP037
        """Revoke capability consent for the calling operator (FR-013).

        capability: Capability enum value string.
        human_operator_id resolved server-side from sender_uid — NEVER from payload.
        Returns JSON: {"revoked": true/false, ...consent_fields}.
        """
        try:
            sender_uid = await self._resolve_current_sender_uid()
            result = self._wiring.revoke_consent(
                capability=capability,
                sender_uid=sender_uid,
            )
            return json.dumps(result)
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def ListConsents(self) -> "s":  # noqa: N802,F821,UP037
        """List active consents for the calling operator (read-only).

        No authZ required — same policy as list_*.
        sender_uid scopes the list to the calling operator.
        Returns JSON list of consent dicts.
        """
        sender_uid = await self._resolve_current_sender_uid()
        return self._wiring.list_consents(sender_uid=sender_uid)

    # ------------------------------------------------------------------
    # T047 — Memory read-only verbs (spec 014-agentic-desktop, increment 2)
    # ListMemory / SearchMemory — read-only, no authZ, same policy as list_*.
    # PII: content truncado en el wiring; nunca logueado ni persistido aquí.
    # ------------------------------------------------------------------

    @method()
    async def ListMemory(self, limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Lista las entradas de memoria del agente (read-only). JSON → cliente.

        limit: máximo de entradas a devolver (u = uint32). 0 = sin límite.
        Devuelve JSON lista de {id, target, content_truncated, entry_index}.
        Devuelve [] si el store no está disponible (estado honesto, nunca mock).
        PII: content truncado en el wiring — nunca cruzará el bus completo.
        No authZ: read-only, mismo patrón que list_providers/list_conversations.
        """
        return self._wiring.list_memory(limit=int(limit) or 0)

    @method()
    async def SearchMemory(self, query: "s", limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Busca en la memoria del agente (read-only, case-insensitive). JSON → cliente.

        query: cadena de búsqueda (vacía → [] sin buscar).
        limit: máximo de resultados (0 = sin límite, pero se recomienda ≤100).
        Devuelve JSON lista de {id, target, content_truncated, entry_index}.
        No authZ: read-only — el caller no muta nada (mismo patrón que list_*).
        """
        return self._wiring.search_memory(query=query, limit=int(limit) or 50)

    # ------------------------------------------------------------------
    # Acceso remoto (Settings → toggle) — espejo noVNC con URL individual.
    # enable/disable mutan (password = consentimiento, PAM en root helper);
    # status es read-only (sin authZ, mismo patrón que list_*).
    # ------------------------------------------------------------------

    @method()
    async def EnableRemoteAccess(self, password: "s") -> "s":  # noqa: N802,F821,UP037
        """Activa el acceso remoto (espejo noVNC + túnel público).

        password: contraseña del dispositivo (verificación PAM en el root
        helper — escribir el staged request NO basta para activar).
        Devuelve JSON {ok, staged} — la UI sondea GetRemoteAccessStatus.
        """
        try:
            sender_uid = await self._resolve_current_sender_uid()
            result = self._wiring.enable_remote_access(
                password=password, sender_uid=sender_uid
            )
            return json.dumps(result)
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def DisableRemoteAccess(self, password: "s") -> "s":  # noqa: N802,F821,UP037
        """Desactiva el acceso remoto (password del dispositivo, PAM en helper)."""
        try:
            sender_uid = await self._resolve_current_sender_uid()
            result = self._wiring.disable_remote_access(
                password=password, sender_uid=sender_uid
            )
            return json.dumps(result)
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

    @method()
    async def GetRemoteAccessStatus(self) -> "s":  # noqa: N802,F821,UP037
        """Estado del acceso remoto: JSON {active, url}. Read-only, sin authZ."""
        return json.dumps(self._wiring.get_remote_access_status())

    # ------------------------------------------------------------------
    # Security Center — política + audit de instalaciones (Grupo C wiring).
    # Persistencia en shell-state.db. Lecturas sin authZ; mutadores con
    # sender_uid del bus (CWE-862). Tablas creadas idempotentemente en
    # la primera llamada — no requieren migración externa.
    # ------------------------------------------------------------------

    @method()
    async def ScanInstall(self, kind: "s", identifier: "s") -> "s":  # noqa: N802,F821,UP037
        """On-demand security scan without installing.

        kind:       install target kind ("skill" | "mcp_server" | "package" | …).
        identifier: human-readable slug or URL of the artifact to scan.
        Returns JSON {scan_id, identifier, score, verdict, risks} or {error}.
        authZ: operador (sender_uid del bus, CWE-862).
        """
        sender_uid = await self._resolve_current_sender_uid()
        k = (kind or "skill").strip()
        ident = (identifier or "").strip()
        if not ident:
            return json.dumps({"error": "identifier vacío"})
        try:
            self._wiring._authorize_and_resolve(sender_uid, operation="scan_install")
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc
        return self._wiring.scan_install(kind=k, identifier=ident)

    @method()
    async def ScanInstallDraft(self, draft_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Pre-install scan from a full draft (kind+identifier+argv/source_url).

        The UI gate: scans (no install), emits the score → InstallReview modal.
        Read-only, sin authZ (mismo patrón que ScanInstall — no muta nada).
        """
        return self._wiring.scan_install_draft(draft_json=draft_json)

    @method()
    async def GetSecurityPolicy(self) -> "s":  # noqa: N802,F821,UP037
        """Lee la política de seguridad persistida. JSON {} = valores por defecto.
        Read-only, sin authZ — mismo patrón que GetRemoteAccessStatus.
        """
        return json.dumps(self._wiring.get_security_policy())

    @method()
    async def SetSecurityPolicy(self, policy_json: "s") -> "s":  # noqa: N802,F821,UP037
        """Persiste la política de seguridad. JSON {key: value}.
        Muta → authZ por sender_uid del bus (CWE-862).
        Devuelve JSON {ok, error?}.
        """
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            self._wiring.set_security_policy(
                policy_json=policy_json, sender_uid=sender_uid
            )
        )

    @method()
    async def ListRecentScans(self, limit: "u") -> "s":  # noqa: N802,F821,UP037
        """Lista los escaneos de instalación recientes. JSON []. Read-only, sin authZ.
        limit: máximo de entradas (u = uint32). 0 → usa el default del wiring (50).
        """
        return json.dumps(self._wiring.list_recent_scans(limit=int(limit) or 50))

    @method()
    async def RecordInstallDecision(  # noqa: N802,F821,UP037
        self,
        scan_id: "s",
        decision: "s",
        identifier: "s",
        kind: "s",
        score: "i",
        verdict: "s",
        risks_json: "s",
    ) -> "s":
        """Persiste la decisión del usuario (allow/block/cancelled/installed).
        Muta → authZ por sender_uid del bus (CWE-862).
        Devuelve JSON {ok, error?}.
        """
        sender_uid = await self._resolve_current_sender_uid()
        return json.dumps(
            self._wiring.record_install_decision(
                scan_id=scan_id,
                decision=decision,
                identifier=identifier,
                kind=kind,
                score=int(score),
                verdict=verdict,
                risks_json=risks_json,
                sender_uid=sender_uid,
            )
        )

    # ------------------------------------------------------------------
    # Señal T017 — overlay requested (emitida por OpenOverlay)
    # ------------------------------------------------------------------

    @signal()
    def OverlayRequested(self) -> "":  # noqa: N802,F821,UP037
        """Emitted when OpenOverlay is called. The overlay process listens."""
        return

    # ------------------------------------------------------------------
    # Security Center signals (emitted by the daemon after scanning)
    # ------------------------------------------------------------------

    @signal()
    def ScanCompleted(  # noqa: N802
        self, scan_id: str, verdict: str
    ) -> "ss":  # noqa: F821,UP037
        """Emitted after every scan completes (pre-install or on-demand).

        scan_id: UUID string of the ScanRecord.
        verdict: "PASS" | "WARN" | "FAIL".
        """
        return scan_id, verdict

    @signal()
    def InstallReviewRequested(  # noqa: N802
        self, scan_id: str, scan_data_json: str
    ) -> "ss":  # noqa: F821,UP037
        """Emitted to open the InstallReview modal in the shell.

        scan_id:       UUID string of the ScanRecord.
        scan_data_json: JSON {scan_id, identifier, score, verdict, risks}.
        """
        return scan_id, scan_data_json

    # ------------------------------------------------------------------
    # Señales de gobernanza de plataformas (feature 010)
    # ------------------------------------------------------------------

    @signal()
    def PlatformModelStateChanged(  # noqa: N802
        self, model_id: str, old_state: str, new_state: str
    ) -> "sss":  # noqa: F821,UP037
        return model_id, old_state, new_state

    @signal()
    def PlatformZoneStale(  # noqa: N802
        self, model_id: str, zone_id: str
    ) -> "ss":  # noqa: F821,UP037
        return model_id, zone_id

    @signal()
    def ModelGapOpened(self, model_id: str, gap_id: str) -> "ss":  # noqa: N802,F821,UP037
        return model_id, gap_id

    @signal()
    def CapabilityBindingChanged(  # noqa: N802
        self, agent_id: str, capability_kind: str, bound: bool
    ) -> "ssb":  # noqa: F821,UP037
        return agent_id, capability_kind, bound

    # ------------------------------------------------------------------
    # Señales (emitidas por el daemon al loop)
    # ------------------------------------------------------------------

    @signal()
    def TaskEnqueued(self, task_id: str, trigger_kind: str, priority: int) -> "ssi":  # noqa: N802,F821,UP037
        return task_id, trigger_kind, priority

    @signal()
    def TaskStatusChanged(  # noqa: N802
        self, task_id: str, old_status: str, new_status: str
    ) -> "sss":  # noqa: F821,UP037
        return task_id, old_status, new_status

    @signal()
    def TaskPendingApproval(  # noqa: N802
        self, task_id: str, proposal_id: str, risk: str
    ) -> "sss":  # noqa: F821,UP037
        return task_id, proposal_id, risk

    @signal()
    def AgentLivenessChanged(self, alive: bool, has_model: bool) -> "bb":  # noqa: N802,F821,UP037
        return alive, has_model

    @signal()
    def AppLaunchRequested(self, cmd: str) -> "s":  # noqa: N802,F821,UP037
        """Emitted when the agent requests launching a desktop application.

        cmd: binary basename (e.g. "gnome-calculator"). No args, no shell string.
        The compositor (lumenso-shell / hermes-user) listens and calls
        sysManager.launchNativeApp(cmd).
        """
        return cmd

    # ------------------------------------------------------------------
    # Chat streaming signals (spec streaming-dbus)
    # ChatDelta   — one incremental text token/batch during LLM generation.
    # ChatStreamEnd — generation complete for this conversation.
    #
    # Policy: hermes-user already has a broad
    #   <allow receive_sender="org.hermes.Runtime" receive_interface="org.hermes.Runtime1" receive_type="signal"/>
    # in org.hermes.Runtime1.conf (W27 fix) — no per-signal entry needed.
    # ------------------------------------------------------------------

    @signal()
    def ChatDelta(  # noqa: N802
        self, conversation_id: str, seq: int, text: str
    ) -> "sis":  # noqa: F821,UP037
        """Incremental text delta emitted per coalesced batch during LLM generation.

        conversation_id: UUID string of the active conversation.
        seq: monotonically increasing counter within this generation (starts at 1).
        text: accumulated text batch for this batch window.

        The compositor filters by its active conversation_id and appends to
        streamingContent. Seq is informational — gaps indicate coalescing.
        """
        return conversation_id, seq, text

    @signal()
    def ChatStreamEnd(self, conversation_id: str) -> "s":  # noqa: N802,F821,UP037
        """Emitted once when LLM generation for a conversation is complete.

        conversation_id: UUID string matching the ChatDelta stream.
        The compositor uses this to commit streamingContent as the final
        assistant message and stop the streaming indicator.
        """
        return conversation_id

    # ------------------------------------------------------------------
    # Two-mode security kernel (spec 015)
    # ApprovalRequested — emitted by the daemon when a dangerous terminal/code
    # command needs owner approval (Modo Guardado / default mode).
    # The compositor shows an approval card; the owner approves or denies.
    # ------------------------------------------------------------------

    @signal()
    def ApprovalRequested(self, payload_json: str) -> "s":  # noqa: N802,F821,UP037
        """Emitted when a dangerous command requires owner approval (Modo Guardado).

        payload_json: JSON string with keys:
          request_id  — UUID string; pass back to ResolveApproval.
          command     — the exact command string being gated.
          description — human-readable reason from the approval engine.
          pattern_keys — list of matched dangerous-pattern names.

        The compositor subscribes to this signal, shows the approval card,
        and calls ResolveApproval(request_id, choice) when the owner decides.
        The agent thread blocks on _await_gateway_decision until resolved.
        AUTO mode ON: this signal is never emitted (session YOLO active).
        Hardline commands: this signal is never emitted (blocked unconditionally
        BEFORE check_all_command_guards runs; see security_hook.py step 2).
        """
        return payload_json

    # ------------------------------------------------------------------
    # Two-mode security kernel methods
    # ------------------------------------------------------------------

    @method()
    async def ResolveApproval(  # noqa: N802
        self, request_id: "s", choice: "s"  # noqa: F821,UP037
    ) -> "s":  # noqa: F821,UP037
        """Resolve a pending gateway approval request from the compositor.

        Called by the compositor after the owner approves or denies an
        ApprovalRequested card.

        request_id: UUID string from the ApprovalRequested payload.
        choice: "once" | "session" | "always" | "deny"
          - "once"    — approve this invocation only.
          - "session" — approve all identical commands in this session.
          - "always"  — always approve this pattern (adds to smart-approval list).
          - "deny"    — reject the command; the agent sees a BLOCKED result.
        Unknown choices are treated as "deny" (fail-closed).

        Returns JSON: {"ok": true} or {"ok": false, "error": reason}.
        authZ: operador (sender_uid del bus, CWE-862).
        """
        sender_uid = await self._resolve_current_sender_uid()
        try:
            self._wiring._authorize_and_resolve(sender_uid, operation="resolve_approval")
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

        return self._wiring.resolve_approval(
            request_id=request_id.strip(),
            choice=choice.strip(),
        )

    @method()
    async def SetAutoMode(self, enabled: "b") -> "s":  # noqa: N802,F821,UP037
        """Toggle the security mode between Guardado (default) and AUTO.

        enabled=true  → Modo AUTO: full autonomy; the gateway HITL card is
                        bypassed; the agent executes dangerous commands without
                        prompting. The hardline floor is still unconditional.
        enabled=false → Modo Guardado (DEFAULT): dangerous commands require
                        owner approval via ApprovalRequested card.

        The setting is persisted to HERMES_HOME/security_mode.json and takes
        effect on the NEXT cycle (the current cycle is not interrupted).
        Returns JSON: {"ok": true, "auto_mode": <bool>} or {"ok": false, "error"}.
        authZ: operador (sender_uid del bus, CWE-862).

        SECURITY NOTE: AUTO mode NEVER bypasses the hardline floor
        (detect_hardline_command, e.g. rm -rf /). That gate is unconditional
        and lives in security_hook.py independently of session YOLO.
        """
        sender_uid = await self._resolve_current_sender_uid()
        try:
            self._wiring._authorize_and_resolve(sender_uid, operation="set_auto_mode")
        except PermissionError as exc:
            raise DBusError("org.hermes.Error.Unauthorized", str(exc)) from exc

        return self._wiring.set_auto_mode(enabled=bool(enabled))

    @method()
    async def GetAutoMode(self) -> "s":  # noqa: N802,F821,UP037
        """Return the current security mode as JSON.

        Returns JSON: {"auto_mode": bool}.
        Read-only — no authZ required (same policy as GetRemoteAccessStatus).
        """
        return self._wiring.get_auto_mode()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def attach_bus(self, bus: MessageBus) -> None:
        """Registra el bus para resolución de sender_uid."""
        self._bus = bus

    async def _resolve_current_sender_uid(self) -> int:
        """Resuelve el UID del sender actual via GetConnectionUnixUser.

        Lee el sender del ContextVar _CURRENT_SENDER_VAR, que fue poblado
        síncronamente por el message handler antes de que ensure_future()
        creara esta tarea — garantizando aislamiento por llamada sin race.

        Fix-9: NUNCA devuelve uid 0 (root) como centinela; lanza PermissionError
        cuando el sender no puede resolverse → el método D-Bus devuelve error
        al cliente en lugar de ejecutarse con root implícito (CWE-269).

        CTRL-P1-1: el UID viene del bus (peer cred), NUNCA del mensaje.
        """
        if self._bus is None:
            raise PermissionError(
                "hermes.dbus.no_bus: bus no disponible — "
                "no se puede resolver el UID del sender (fail-closed)"
            )
        sender = _CURRENT_SENDER_VAR.get()
        if sender is None:
            raise PermissionError(
                "hermes.dbus.no_sender: sender D-Bus no disponible en el contexto — "
                "operación denegada (fail-closed)"
            )
        return await _get_connection_unix_user(self._bus, sender)


# ---------------------------------------------------------------------------
# DbusRuntimeAdapter — gestiona el ciclo de vida del bus
# ---------------------------------------------------------------------------


class DbusRuntimeAdapter:
    """Registra Runtime1ServiceInterface en el system bus y gestiona el ciclo de vida.

    Uso:
        adapter = DbusRuntimeAdapter(wiring=wiring)
        await adapter.start()  # bloquea hasta disconnect
    """

    def __init__(
        self,
        *,
        wiring: DbusRuntimeServiceWiring,
        app_launch_adapter: "object | None" = None,
        cerebro_browser_manager: "object | None" = None,
        nous_engine: "object | None" = None,
    ) -> None:
        self._wiring = wiring
        self._iface: Runtime1ServiceInterface | None = None
        self._bus: MessageBus | None = None
        # Bus event loop — captured in start(); used to marshal signal emits from
        # non-loop threads (gateway notify / hub worker / engine executor) via
        # call_soon_threadsafe. dbus-fast aio emission is NOT thread-safe.
        self._loop: "object | None" = None
        # AppLaunchSurfaceAdapter — emitter injected after bus starts (same
        # pattern as _scan_signal_emitter for the Security Center).
        self._app_launch_adapter = app_launch_adapter
        # CerebroBrowserManager — receives the SAME app-launch emitter so it
        # can request the compositor to spawn the headed Chromium.
        self._cerebro_browser_manager = cerebro_browser_manager
        # NousReasoningEngine — receives the chat-delta emitter pair after bus
        # starts so the LLM streaming loop can push tokens over D-Bus.
        self._nous_engine = nous_engine

    async def start(self) -> None:
        """Conecta al system bus, exporta la interfaz y entra en el event loop."""
        from dbus_fast.aio import MessageBus  # noqa: PLC0415
        from dbus_fast.constants import BusType  # noqa: PLC0415

        import asyncio as _asyncio  # noqa: PLC0415

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus = bus
        # Capture the bus event loop so signal emitters can marshal onto it from
        # non-loop threads (call_soon_threadsafe). start() runs on this loop.
        self._loop = _asyncio.get_running_loop()
        iface = Runtime1ServiceInterface(wiring=self._wiring)
        iface.attach_bus(bus)
        self._iface = iface
        # Inject the scan signal emitter into the wiring so install_hub_skill
        # and scan_install can emit D-Bus signals after scanning.
        self._wiring._scan_signal_emitter = self._make_scan_signal_emitter()
        # Build the app-launch emitter once; share it between AppLaunchSurfaceAdapter
        # and CerebroBrowserManager — both need the same compositor signal path.
        emitter = self._make_app_launch_emitter()
        # Inject the app launch emitter into AppLaunchSurfaceAdapter so the
        # broker can request app launches via AppLaunchRequested(cmd) D-Bus signal.
        if self._app_launch_adapter is not None:
            self._app_launch_adapter.set_launch_emitter(  # type: ignore[union-attr]
                emitter
            )
        # Inject the same emitter into CerebroBrowserManager so the headed
        # Chromium is launched through the compositor (visible on wayland-0).
        if self._cerebro_browser_manager is not None:
            self._cerebro_browser_manager.set_launch_emitter(  # type: ignore[union-attr]
                emitter
            )
        # Chat streaming (spec streaming-dbus): inject (emit_delta, emit_end)
        # into NousReasoningEngine so run_cycle can push token batches over D-Bus.
        # Done AFTER bus connects so the iface is live before any cycle runs.
        # Fail-safe: if the engine is not a NousReasoningEngine or lacks the
        # method, we skip silently (no streaming over D-Bus, poller fallback works).
        if self._nous_engine is not None:
            _emit_delta, _emit_end = self._make_chat_delta_emitters()
            _setter = getattr(self._nous_engine, "set_chat_delta_emitter", None)
            if callable(_setter):
                _setter(_emit_delta, _emit_end)

        # Two-mode security kernel (spec 015): register the gateway notify
        # callback NOW — the bus is live so the ApprovalRequested signal can
        # reach the compositor. The callback fires on the agent executor thread
        # (not here); it just posts the D-Bus signal (non-blocking).
        self._register_approval_gateway()
        # Intercept messages to inject sender before dispatching to iface
        bus.add_message_handler(self._make_message_handler(iface))
        bus.export(_OBJECT_PATH, iface)
        await bus.request_name(_WELL_KNOWN_NAME)
        logger.info(
            "hermes.dbus_adapter.started",
            extra={"name": _WELL_KNOWN_NAME, "path": _OBJECT_PATH},
        )
        await bus.wait_for_disconnect()

    def _make_message_handler(
        self, iface: Runtime1ServiceInterface  # noqa: ARG002
    ):
        """Retorna un handler que inyecta msg.sender en _CURRENT_SENDER_VAR.

        Se ejecuta síncronamente en _process_message(), ANTES de que
        dbus-fast llame al method handler con ensure_future().  La tarea
        creada por ensure_future() hereda un snapshot del contexto actual,
        donde _CURRENT_SENDER_VAR ya contiene el sender de ESTE mensaje —
        sin interferencia con otras llamadas concurrentes.
        """
        def handler(msg) -> None:
            if (
                msg.path == _OBJECT_PATH
                and msg.interface == _INTERFACE_NAME
            ):
                _CURRENT_SENDER_VAR.set(msg.sender)
        return handler

    def emit_task_enqueued(self, task_id: str, trigger_kind: str, priority: int) -> None:
        """Emite la señal TaskEnqueued al bus."""
        if self._iface is not None:
            self._iface.TaskEnqueued(task_id, trigger_kind, priority)

    def emit_liveness_changed(self, *, alive: bool, has_model: bool) -> None:
        """Emite AgentLivenessChanged al bus."""
        if self._iface is not None:
            self._iface.AgentLivenessChanged(alive, has_model)

    def emit_scan_completed(self, scan_id: str, verdict: str) -> None:
        """Emite ScanCompleted al bus. Fail-safe: logs on error."""
        if self._iface is None or self._loop is None:
            return
        fn = self._iface.ScanCompleted
        try:
            self._loop.call_soon_threadsafe(fn, scan_id, verdict)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus_adapter.scan_completed_emit_failed: %s", exc)

    def emit_install_review_requested(self, scan_id: str, scan_data_json: str) -> None:
        """Emite InstallReviewRequested al bus. Fail-safe: logs on error."""
        if self._iface is None or self._loop is None:
            return
        fn = self._iface.InstallReviewRequested
        try:
            self._loop.call_soon_threadsafe(fn, scan_id, scan_data_json)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus_adapter.install_review_emit_failed: %s", exc)

    def emit_app_launch_requested(self, cmd: str) -> None:
        """Emite AppLaunchRequested(cmd) al bus. Fail-safe: logs on error."""
        if self._iface is None or self._loop is None:
            return
        fn = self._iface.AppLaunchRequested
        try:
            self._loop.call_soon_threadsafe(fn, cmd)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus_adapter.app_launch_emit_failed cmd=%r: %s", cmd, exc)

    def emit_chat_delta(self, conversation_id: str, seq: int, text: str) -> None:
        """Emite ChatDelta(conversation_id, seq, text) al bus. Fail-safe: logs on error."""
        if self._iface is None:
            return
        try:
            self._iface.ChatDelta(conversation_id, seq, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "hermes.dbus_adapter.chat_delta_emit_failed conv=%s seq=%d: %s",
                conversation_id, seq, exc,
            )

    def emit_chat_stream_end(self, conversation_id: str) -> None:
        """Emite ChatStreamEnd(conversation_id) al bus. Fail-safe: logs on error."""
        if self._iface is None:
            return
        try:
            self._iface.ChatStreamEnd(conversation_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "hermes.dbus_adapter.chat_stream_end_emit_failed conv=%s: %s",
                conversation_id, exc,
            )

    def emit_approval_requested(self, payload_json: str) -> None:
        """Emit ApprovalRequested(payload_json) to the bus. Fail-safe: logs on error.

        Called from the gateway notify callback on the agent EXECUTOR thread.
        dbus-fast aio emission is NOT thread-safe (it runs loop.create_future/
        add_writer/sock.send synchronously), so marshal onto the bus loop via
        call_soon_threadsafe. This is the security HITL card path — must be robust.
        """
        if self._iface is None or self._loop is None:
            return
        fn = self._iface.ApprovalRequested
        try:
            self._loop.call_soon_threadsafe(fn, payload_json)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.dbus_adapter.approval_requested_emit_failed: %s", exc
            )

    def make_approval_emitter(self) -> "Callable[[str], None]":
        """Return a callable(payload_json) that emits ApprovalRequested on the bus.

        Passed to approval_gateway.register_gateway_notify_callback() after
        the bus connects (same pattern as _make_app_launch_emitter).
        Thread-safe: dbus-fast signal emission posts to the event loop.
        """
        adapter = self

        def emitter(payload_json: str) -> None:
            adapter.emit_approval_requested(payload_json)

        return emitter

    def _make_app_launch_emitter(self) -> "Callable[[str], None]":
        """Return a callable(cmd) that emits AppLaunchRequested on the bus.

        Passed to AppLaunchSurfaceAdapter.set_launch_emitter() after bus start.
        Thread-safe: dbus-fast signal emission posts to the event loop.
        """
        adapter = self

        def emitter(cmd: str) -> None:
            adapter.emit_app_launch_requested(cmd)

        return emitter

    def _make_chat_delta_emitters(
        self,
    ) -> "tuple[Callable[[str, int, str], None], Callable[[str], None]]":
        """Return (emit_delta, emit_end) callables for LLM streaming over D-Bus.

        emit_delta(conversation_id, seq, text) → ChatDelta signal
        emit_end(conversation_id)              → ChatStreamEnd signal

        THREAD-SAFETY: dbus-fast's aio emission is NOT thread-safe — it calls
        loop.create_future()/add_writer()/sock.send() synchronously in the caller
        thread. The Nous AIAgent runs run_conversation in an executor thread, so the
        ENGINE marshals every call to these emitters onto the bus loop via
        loop.call_soon_threadsafe (see _build_stream_callback in nous_engine.py). By
        the time emit_delta/emit_end run, they are ALREADY on the loop thread — do
        NOT call them directly from a non-loop thread without that marshaling.
        Fail-soft: any emission error is logged at DEBUG and never propagates.
        """
        adapter = self

        def emit_delta(conversation_id: str, seq: int, text: str) -> None:
            adapter.emit_chat_delta(conversation_id, seq, text)

        def emit_end(conversation_id: str) -> None:
            adapter.emit_chat_stream_end(conversation_id)

        return emit_delta, emit_end

    def _make_scan_signal_emitter(self):
        """Return a callable(scan_id, verdict, scan_data_json) for the wiring.

        Called from install_hub_skill / scan_install on the hub worker thread.
        D-Bus signal emission is thread-safe in dbus-fast (it posts to the loop).
        """
        adapter = self

        def emitter(scan_id: str, verdict: str, scan_data_json: str) -> None:
            adapter.emit_scan_completed(scan_id, verdict)
            adapter.emit_install_review_requested(scan_id, scan_data_json)

        return emitter

    def _register_approval_gateway(self) -> None:
        """Register the ApprovalRequested gateway notify callback.

        Called once inside start() after the D-Bus bus is live so the signal
        can reach the compositor. Fail-soft: any error is logged but the daemon
        continues (dangerous commands will be blocked fail-closed without a card).
        """
        try:
            from hermes.runtime.approval_gateway import (  # noqa: PLC0415
                register_gateway_notify_callback,
            )
            register_gateway_notify_callback(self.make_approval_emitter())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.dbus_adapter.approval_gateway_register_failed: %s — "
                "ApprovalRequested signal NOT wired; dangerous commands will be "
                "blocked permanently (fail-closed) until daemon restart",
                exc,
            )


# ---------------------------------------------------------------------------
# D-Bus helper: GetConnectionUnixUser
# ---------------------------------------------------------------------------


async def _get_connection_unix_user(bus: MessageBus, sender_unique_name: str) -> int:
    """Resuelve el UID POSIX del proceso que envió el mensaje.

    Llama a org.freedesktop.DBus.GetConnectionUnixUser con el unique name
    del sender (p.ej. ':1.123'). El daemon D-Bus verifica las credenciales
    del peer — no es falsificable por el cliente (CWE-290).

    Raises:
        PermissionError: si el bus no puede resolver el UID (fail-closed).
    """
    from dbus_fast import Message, MessageType  # noqa: PLC0415

    reply = await bus.call(
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="GetConnectionUnixUser",
            signature="s",
            body=[sender_unique_name],
        )
    )
    if reply.message_type == MessageType.ERROR:
        raise PermissionError(
            f"No se pudo resolver UID de '{sender_unique_name}': {reply.body}"
        )
    uid: int = reply.body[0]
    return uid


# ---------------------------------------------------------------------------
# D-Bus dict builders (CTRL-P1-5: solo metadatos)
# ---------------------------------------------------------------------------


def _queue_status_to_dict(status: object) -> dict:
    """Convierte QueueStatus a a{sv} para D-Bus. Solo metadatos."""
    if status is None:
        return {}
    return {
        "state": Variant("s", getattr(status, "state", "unknown")),
        "pending": Variant("u", getattr(status, "pending", 0)),
        "in_progress": Variant("u", getattr(status, "in_progress", 0)),
        "pending_approval": Variant("u", getattr(status, "pending_approval", 0)),
        "last_audit_head": Variant("s", getattr(status, "last_audit_head_hex", "")),
    }


def _task_status_to_dict(status: object) -> dict:
    """Convierte TaskStatusView a a{sv}. Solo metadatos (CTRL-P1-5)."""
    if status is None:
        return {}
    return {
        "task_id": Variant("s", str(getattr(status, "task_id", ""))),
        "status": Variant("s", str(getattr(status, "status", ""))),
        "attempts": Variant("u", getattr(status, "attempts", 0)),
        "enqueued_by": Variant("s", str(getattr(status, "enqueued_by", ""))),
        "stream_path": Variant("s", str(getattr(status, "stream_path", ""))),
        "error": Variant("s", str(getattr(status, "error", "") or "")),
    }
