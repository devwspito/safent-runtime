"""SqliteAuthorizedTriggerRepository — allow-list firmada de orígenes auto-disparadores.

Implementa AuthorizedTriggerRepositoryPort sobre SQLite (tablas
authorized_trigger_types + authorized_trigger_instances ya creadas por
schema.py:ensure_tasks_schema).

Semántica de seguridad (CTRL-P2-7/8/9/15/CTRL-P2-18):
  - is_authorized(): lee enabled=1 en CADA llamada (NO cacheado, CTRL-P2-15).
    Fail-closed: cualquier error → None (deny).
  - authorize(): admin_uuid NUNCA proviene del contenido — el caller lo extrae
    del canal autenticado (D-Bus GetConnectionUnixUser) antes de llamar aquí.
  - revoke(): enabled=0 + revoked_at timestampado + revoked_by. Inmediato.
  - consume_budget(): token-bucket persistente por-origen (CTRL-P2-10). Verifica
    cuántas tareas se encolaron en la última hora para este instance_id y rechaza
    si ya se alcanzó el budget.

in_memory(): devuelve una instancia sobre ':memory:' con el esquema P2 aplicado.
Útil para tests sin fichero de DB.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from hermes.tasks.triggers.domain.authorized_trigger_ports import (
    AuthorizedTrigger,
    AuthorizedTriggerType,
    RiskCeiling,
)

# Columnas del SELECT en authorized_trigger_instances (orden estable).
# P3: target_agent_id, task_instruction, one_shot, title añadidos al final
# para mantener compatibilidad con código que solo lee los primeros 10 campos.
_SELECT_INSTANCE = (
    "instance_id, trigger_type, scope_value, "
    "allowed_capabilities_json, risk_ceiling, hourly_budget, "
    "created_by_admin_uuid, authorized_at, approval_signature, enabled, "
    "target_agent_id, task_instruction, one_shot, title"
)


def _row_to_trigger(row: tuple[Any, ...]) -> AuthorizedTrigger:
    # First 10 columns are the P0/P2 core; P3 columns are optional tail.
    (
        instance_id,
        trigger_type,
        scope_value,
        allowed_caps_json,
        risk_ceiling,
        _hourly_budget,
        created_by_admin_uuid,
        authorized_at_str,
        approval_signature,
        enabled,
        *p3_tail,
    ) = row
    caps = tuple(json.loads(allowed_caps_json) if allowed_caps_json else [])
    # P3 fields (present after schema migration; None on old DB rows before ALTER).
    target_agent_id: str | None = p3_tail[0] if len(p3_tail) > 0 else None
    task_instruction: str = (p3_tail[1] or "") if len(p3_tail) > 1 else ""
    one_shot: bool = bool(p3_tail[2]) if len(p3_tail) > 2 else False
    title: str = (p3_tail[3] or "") if len(p3_tail) > 3 else ""
    return AuthorizedTrigger(
        trigger_instance_id=UUID(instance_id),
        trigger_type=AuthorizedTriggerType(trigger_type),
        scope_value=scope_value,
        allowed_capabilities=caps,
        risk_ceiling=RiskCeiling(risk_ceiling),
        created_by_admin_uuid=UUID(created_by_admin_uuid),
        authorized_at=datetime.fromisoformat(authorized_at_str),
        approval_signature=approval_signature,
        enabled=bool(enabled),
        target_agent_id=target_agent_id,
        task_instruction=task_instruction,
        one_shot=one_shot,
        title=title,
    )


class SqliteAuthorizedTriggerRepository:
    """Repository SQLite de la allow-list firmada (CTRL-P2-9/15/18).

    Recibe una `sqlite3.Connection` ya inicializada (con el esquema P2
    aplicado por ensure_tasks_schema). Testeable con in_memory().

    El token-bucket de presupuesto por hora (consume_budget) tiene dos modos:
      - Con agent_tasks poblada (SQLite de producción): cuenta filas reales.
      - Con agent_tasks vacía (InMemoryWorkQueue en tests): usa un contador
        in-process por instance_id. El contador se resetea al instanciar
        (aceptable para tests).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Contador in-process para el budget: {instance_id → count_this_hour}
        # Usado como fallback cuando agent_tasks no tiene la FK poblada
        # (InMemoryWorkQueue en tests no persiste en SQLite).
        self._budget_counter: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Factory: in-memory para tests (aplica el esquema P2 al vuelo)
    # ------------------------------------------------------------------

    @classmethod
    def in_memory(cls) -> SqliteAuthorizedTriggerRepository:
        """Devuelve una instancia sobre ':memory:' con esquema P2 aplicado."""
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        ensure_tasks_schema(conn)
        return cls(conn)

    # ------------------------------------------------------------------
    # AuthorizedTriggerRepositoryPort
    # ------------------------------------------------------------------

    async def is_authorized(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        scope_value: str,
    ) -> AuthorizedTrigger | None:
        """Devuelve el AuthorizedTrigger HABILITADO (enabled=1) que cubre
        (tipo, scope), o None (fail-closed, CTRL-P2-15).

        La consulta NO está cacheada — cada llamada lee de SQLite para que
        la revocación sea inmediata (CTRL-P2-15). Scope: coincidencia exacta
        o scope_value='*' para wilcard de admin (timer).

        LOW fix (one-shot trigger provenance, FASE 3 A2A): si MÁS DE UNA fila
        enabled=1 cubre (tipo, scope) — p.ej. una autorización previa para el
        mismo from_employee_id aún no revocada por una carrera/fallo — el
        orden SIN `ORDER BY` era ARBITRARIO (rowid de SQLite), pudiendo
        devolver una fila VIEJA en vez de la recién minteada por la aprobación
        EN CURSO; `created_by_admin_uuid` de esa fila vieja se filtraría como
        `enqueued_by`, rompiendo la garantía "enqueued_by = el aprobador
        actual". `ORDER BY authorized_at DESC` hace la elección determinista:
        siempre gana la autorización MÁS RECIENTE.
        """
        try:
            row = self._conn.execute(
                # _SELECT_INSTANCE is a module-level constant (not user input);
                # all user values are passed as parameterized args — no SQL injection.
                f"SELECT {_SELECT_INSTANCE} FROM authorized_trigger_instances "  # noqa: S608
                "WHERE trigger_type = ? AND (scope_value = ? OR scope_value = '*') "
                "AND enabled = 1 "
                "ORDER BY authorized_at DESC "
                "LIMIT 1",
                (str(trigger_type), scope_value),
            ).fetchone()
        except sqlite3.Error:
            return None  # fail-closed

        if row is None:
            return None
        return _row_to_trigger(tuple(row))

    async def authorize(
        self,
        *,
        trigger_type: AuthorizedTriggerType,
        scope_value: str,
        allowed_capabilities: tuple[str, ...],
        risk_ceiling: RiskCeiling,
        admin_uuid: UUID,
        approval_signature: str,
        hourly_budget: int = 10,
    ) -> AuthorizedTrigger:
        """Registra un nuevo origen autorizado firmado (CTRL-P2-9).

        admin_uuid SIEMPRE viene del canal autenticado (el caller lo extrae
        del bus). NUNCA del contenido del mensaje.
        """
        instance_id = uuid4()
        now = datetime.now(tz=UTC)
        caps_json = json.dumps(list(allowed_capabilities))

        self._conn.execute(
            """
            INSERT INTO authorized_trigger_instances (
                instance_id, trigger_type, scope_value,
                allowed_capabilities_json, risk_ceiling, hourly_budget,
                created_by_admin_uuid, authorized_at, approval_signature,
                enabled, revoked_at, revoked_by_admin_uuid,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,1,NULL,NULL,?,?)
            """,
            (
                str(instance_id),
                str(trigger_type),
                scope_value,
                caps_json,
                str(risk_ceiling),
                hourly_budget,
                str(admin_uuid),
                now.isoformat(),
                approval_signature,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self._conn.commit()

        return AuthorizedTrigger(
            trigger_instance_id=instance_id,
            trigger_type=trigger_type,
            scope_value=scope_value,
            allowed_capabilities=allowed_capabilities,
            risk_ceiling=risk_ceiling,
            created_by_admin_uuid=admin_uuid,
            authorized_at=now,
            approval_signature=approval_signature,
            enabled=True,
        )

    async def revoke(
        self,
        *,
        trigger_instance_id: UUID,
        admin_uuid: UUID,
    ) -> None:
        """Deshabilita un origen (enabled=0). Corte INMEDIATO (CTRL-P2-15/FR-018).

        La coherencia I11 exige: enabled=0 ↔ revoked_at IS NOT NULL.
        """
        now = datetime.now(tz=UTC)
        self._conn.execute(
            """
            UPDATE authorized_trigger_instances
            SET enabled = 0,
                revoked_at = ?,
                revoked_by_admin_uuid = ?,
                updated_at = ?
            WHERE instance_id = ? AND enabled = 1
            """,
            (
                now.isoformat(),
                str(admin_uuid),
                now.isoformat(),
                str(trigger_instance_id),
            ),
        )
        self._conn.commit()

    async def consume_budget(self, *, trigger_instance_id: UUID) -> bool:
        """Token-bucket por-origen (CTRL-P2-10/FR-022).

        Estrategia dual:
          1. Primero intenta contar filas reales en agent_tasks (producción).
          2. Si la tabla no tiene trigger_instance_id o está vacía, usa el
             contador in-process self._budget_counter (tests con InMemoryWorkQueue).

        Retorna True si queda presupuesto, False si agotado (fail-closed).
        """
        instance_key = str(trigger_instance_id)
        row = self._conn.execute(
            "SELECT hourly_budget FROM authorized_trigger_instances "
            "WHERE instance_id = ? AND enabled = 1",
            (instance_key,),
        ).fetchone()
        if row is None:
            return False  # fail-closed: origen no encontrado/revocado

        hourly_budget: int = row[0]
        if hourly_budget == 0:
            return False  # presupuesto cero → siempre deniega

        # Intentar contar en agent_tasks (producción con SQLite poblada)
        from datetime import timedelta  # noqa: PLC0415
        window_start_str = (
            datetime.now(tz=UTC) - timedelta(hours=1)
        ).isoformat()
        sqlite_count = self._count_sqlite_tasks(instance_key, window_start_str)

        if sqlite_count > 0:
            # Hay filas reales → modo producción
            return sqlite_count < hourly_budget

        # Fallback: contador in-process (tests con InMemoryWorkQueue)
        current = self._budget_counter.get(instance_key, 0)
        if current >= hourly_budget:
            return False
        self._budget_counter[instance_key] = current + 1
        return True

    def _count_sqlite_tasks(self, instance_key: str, window_start_str: str) -> int:
        """Cuenta tareas en agent_tasks para este origen en la última hora."""
        try:
            count_row = self._conn.execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE trigger_instance_id = ? AND created_at > ?",
                (instance_key, window_start_str),
            ).fetchone()
            return count_row[0] if count_row else 0
        except sqlite3.OperationalError:
            return 0

    async def list_enabled(self) -> list[AuthorizedTrigger]:
        """Lista todos los orígenes habilitados (para supervisión/UI)."""
        rows = self._conn.execute(
            # _SELECT_INSTANCE is a module-level constant (not user input) — no injection.
            f"SELECT {_SELECT_INSTANCE} FROM authorized_trigger_instances "  # noqa: S608
            "WHERE enabled = 1 ORDER BY authorized_at ASC"
        ).fetchall()
        return [_row_to_trigger(tuple(r)) for r in rows]

    def get_by_id(self, trigger_id: str) -> AuthorizedTrigger | None:
        """Return one enabled trigger by its instance_id, or None if not found."""
        try:
            row = self._conn.execute(
                f"SELECT {_SELECT_INSTANCE} FROM authorized_trigger_instances "  # noqa: S608
                "WHERE instance_id = ? AND enabled = 1",
                (trigger_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return _row_to_trigger(tuple(row))

    def update_task(
        self,
        *,
        trigger_id: str,
        label: str,
        instruction: str,
        cron: str,
        target_agent_id: str | None,
        risk_ceiling: str,
    ) -> bool:
        """Update mutable fields of a scheduled task trigger (fail-safe → False).

        Returns True on success, False if the trigger was not found or not enabled.
        Idempotent: re-applying the same values is a no-op with no side effect.
        """
        now = datetime.now(tz=UTC).isoformat()
        try:
            cur = self._conn.execute(
                """
                UPDATE authorized_trigger_instances
                SET scope_value       = ?,
                    task_instruction  = ?,
                    title             = ?,
                    target_agent_id   = ?,
                    risk_ceiling      = ?,
                    updated_at        = ?
                WHERE instance_id = ? AND enabled = 1
                """,
                (
                    cron,
                    instruction,
                    label,
                    target_agent_id,
                    risk_ceiling,
                    now,
                    trigger_id,
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def list_triggers_with_last_run(
        self,
        *,
        limit: int = 200,
    ) -> list[tuple[AuthorizedTrigger, str | None, str | None]]:
        """LEFT-JOIN triggers with their most-recent agent_tasks row.

        Returns a list of (trigger, last_run_at_iso, last_status) for all
        non-revoked triggers (enabled = 1 only — revoked ones are excluded).
        last_run_at_iso / last_status are None when no task has been spawned.

        Parameterized; no user data in the query template. CTRL-P1-5: returns
        only metadata (no payload, no instruction).
        """
        try:
            rows = self._conn.execute(
                """
                SELECT
                    ati.instance_id,
                    ati.trigger_type,
                    ati.scope_value,
                    ati.allowed_capabilities_json,
                    ati.risk_ceiling,
                    ati.hourly_budget,
                    ati.created_by_admin_uuid,
                    ati.authorized_at,
                    ati.approval_signature,
                    ati.enabled,
                    ati.target_agent_id,
                    ati.task_instruction,
                    ati.one_shot,
                    ati.title,
                    recent.created_at  AS last_run_at,
                    recent.status      AS last_status
                FROM authorized_trigger_instances ati
                LEFT JOIN (
                    SELECT t.trigger_instance_id,
                           t.created_at,
                           t.status
                    FROM agent_tasks t
                    INNER JOIN (
                        SELECT trigger_instance_id,
                               MAX(created_at) AS max_created_at
                        FROM agent_tasks
                        WHERE trigger_instance_id IS NOT NULL
                        GROUP BY trigger_instance_id
                    ) latest
                        ON t.trigger_instance_id = latest.trigger_instance_id
                       AND t.created_at = latest.max_created_at
                ) recent
                    ON recent.trigger_instance_id = ati.instance_id
                WHERE ati.enabled = 1
                ORDER BY ati.authorized_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []

        result = []
        for row in rows:
            # row[:14] = 10 core + 4 P3 (target_agent_id, task_instruction,
            # one_shot, title) → _row_to_trigger los hidrata desde el tail.
            trigger = _row_to_trigger(tuple(row[:14]))
            last_run_at: str | None = row[14]
            last_status: str | None = row[15]
            result.append((trigger, last_run_at, last_status))
        return result

    def list_recent_tasks(
        self,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Most-recent work items across all statuses, ordered by enqueued_at DESC.

        Returns dicts with: task_id, status, trigger_kind, trigger_instance_id,
        enqueued_at, claimed_at, instruction_truncated.

        CTRL-P1-5: instruction truncated to 120 chars; no full payload exposed.
        Parameterized; no user data in the query template.
        """
        try:
            rows = self._conn.execute(
                """
                SELECT
                    task_id,
                    status,
                    trigger_kind,
                    trigger_instance_id,
                    created_at,
                    claimed_at,
                    payload_json
                FROM agent_tasks
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []

        result = []
        for row in rows:
            result.append({
                "task_id": row[0],
                "status": row[1],
                "trigger_kind": row[2],
                "trigger_instance_id": row[3],
                "enqueued_at": row[4],
                "claimed_at": row[5],
                "instruction_truncated": _extract_instruction(row[6]),
            })
        return result


def _extract_instruction(payload_json: str | None) -> str:
    """Extract and truncate instruction from payload_json.

    CTRL-P1-5: caps at 120 chars. Returns empty string if absent or unparseable.
    SECURITY: instruction is user-authored intent (safe to show). Never expose
    tokens, api_key, enqueued_by UUID, or any credential field.
    """
    if not payload_json:
        return ""
    try:
        payload = json.loads(payload_json)
        instruction = str(payload.get("instruction", ""))
        return instruction[:120]
    except (json.JSONDecodeError, TypeError):
        return ""
