"""Training API: REST endpoints para enseñar una skill nueva.

Flow:
  POST /api/v1/training                inicia una sesión
  POST /api/v1/training/{id}/start     abre el navegador enjaulado y comienza capture
  POST /api/v1/training/{id}/pause     pausa la grabación (capturing → paused)
  POST /api/v1/training/{id}/resume    reanuda la grabación (paused → capturing)
  POST /api/v1/training/{id}/stop      detiene capture y cierra el navegador
  POST /api/v1/training/{id}/cancel    cancela la sesión en cualquier estado activo
  POST /api/v1/training/{id}/sign      firma SkillPackage + persist
  POST /api/v1/training/{id}/abandon   abandona
  GET  /api/v1/training/{id}           estado actual

Navegador enjaulado (Part 1):
  POST /start llama a _browser_controller.start() del módulo agent_browser,
  que ya encapsula la jaula Landlock + netns + launcher (HERMES_BROWSER_JAIL).
  En CI (HERMES_BROWSER_JAIL=0) arranca Chromium sin jaula.
  POST /stop llama a _browser_controller.stop().
  El coordinator (_coord()) es siempre None en producción (no hay captura de
  PipeWire en el contenedor Playwright); las llamadas a coord son no-ops cuando
  None y no bloquean el flujo del estado.

State machine de la grabación:
  idle → capturing → paused → capturing → review → validated
                  ↘ cancelled

Persistence helpers (compile + persist SkillPackage) live in persist.py
and are shared with the GTK4 shell process, which drives the coordinator
in-session.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
    TrainingSessionState,
    VoiceCaptureRequired,
)
from hermes.agents_os.domain.skill_content_scan import SkillContentBlockedError
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.shell_server.training.persist import compile_and_persist

logger = logging.getLogger(__name__)

_TRAINING_SCHEMA = """
CREATE TABLE IF NOT EXISTS training_sessions (
  session_id          TEXT PRIMARY KEY,
  skill_name          TEXT NOT NULL,
  description         TEXT,
  state               TEXT NOT NULL DEFAULT 'idle',
  started_at          TEXT NOT NULL,
  stopped_at          TEXT,
  signed_at           TEXT,
  step_count          INTEGER NOT NULL DEFAULT 0,
  surface_kind        TEXT DEFAULT 'browser',
  teaching_context_key TEXT,
  tenant_id           TEXT,
  human_operator_id   TEXT
);
"""

# Idempotent ALTER TABLE migrations for existing DBs (spec 004 / US3).
_TRAINING_MIGRATIONS = [
    "ALTER TABLE training_sessions ADD COLUMN surface_kind TEXT DEFAULT 'browser'",
    "ALTER TABLE training_sessions ADD COLUMN teaching_context_key TEXT",
    "ALTER TABLE training_sessions ADD COLUMN tenant_id TEXT",
    "ALTER TABLE training_sessions ADD COLUMN human_operator_id TEXT",
]

# SkillPackage table lives in audit_api.py's db; we write to the same db.
# P0-4: signing_method column tracks key derivation strategy (v2 = native keystore).
# Security hardening: signature_hex stores the full 64-char HMAC for verification
# at promotion time (promote_skill re-verifies before AUTONOMOUS transition).
_SKILL_PACKAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_packages_view (
  package_id         TEXT PRIMARY KEY,
  skill_id           TEXT NOT NULL,
  skill_name         TEXT NOT NULL,
  version            INTEGER NOT NULL,
  state              TEXT NOT NULL,
  surface_kinds      TEXT NOT NULL,
  signed_at          TEXT NOT NULL,
  signature_short    TEXT,
  signing_method     TEXT NOT NULL DEFAULT 'v1',
  signature_hex      TEXT
);
CREATE INDEX IF NOT EXISTS skill_state_idx
  ON skill_packages_view (state, signed_at DESC);
"""

# Idempotent ALTER TABLE migrations for skill_packages_view in training db.
_SKILL_PACKAGES_TRAINING_MIGRATIONS = [
    "ALTER TABLE skill_packages_view ADD COLUMN signing_method TEXT NOT NULL DEFAULT 'v1'",
    # Security hardening: full signature stored for promotion-time re-verification.
    "ALTER TABLE skill_packages_view ADD COLUMN signature_hex TEXT",
]

# Shared in-process state: one orchestrator per db path so the router
# and the coordinator see the same sessions.
_ORCHESTRATORS: dict[Path, TrainingSessionOrchestrator] = {}


def _get_orchestrator(db_path: Path) -> TrainingSessionOrchestrator:
    if db_path not in _ORCHESTRATORS:
        _ORCHESTRATORS[db_path] = TrainingSessionOrchestrator()
    return _ORCHESTRATORS[db_path]


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class TeachingContextResponse(BaseModel):
    """Embedded teaching context info in TrainingState (spec 004 / US3)."""

    context_id: str | None = None
    isolation_key: str | None = None
    surface_kind: str = "browser"
    input_owner: str = "operator"


class TrainingStartRequest(BaseModel):
    skill_name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    surface_kind: str = "browser"
    site_id: str = ""


class TrainingState(BaseModel):
    session_id: str
    skill_name: str
    description: str | None
    state: str
    started_at: str
    stopped_at: str | None
    signed_at: str | None
    step_count: int
    surface_kind: str = "browser"
    teaching_context: TeachingContextResponse | None = None


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _conn(db_path) as c:
        c.executescript("PRAGMA journal_mode=WAL;")
        c.executescript(_TRAINING_SCHEMA)
        c.executescript(_SKILL_PACKAGES_SCHEMA)
    _run_training_migrations(db_path)


def _run_training_migrations(db_path: Path) -> None:
    """Apply idempotent ALTER TABLE migrations for existing DBs."""
    with _conn(db_path) as c:
        for sql in _TRAINING_MIGRATIONS:
            try:
                c.execute(sql)
            except Exception as exc:  # noqa: BLE001
                # SQLite raises OperationalError "duplicate column name" when
                # the column already exists — that is the idempotency signal.
                if "duplicate column" not in str(exc).lower():
                    logger.warning("training migration skipped: %s — %s", sql[:60], exc)
        for sql in _SKILL_PACKAGES_TRAINING_MIGRATIONS:
            try:
                c.execute(sql)
            except Exception as exc:  # noqa: BLE001
                if "duplicate column" not in str(exc).lower():
                    logger.warning("skill_packages migration skipped: %s — %s", sql[:60], exc)


def create_training_router(
    db_path: Path,
    *,
    coordinator=None,  # TrainingCaptureCoordinator | None — injected in tests
    teaching_orchestrator=None,  # TeachingSessionOrchestrator | None — injected in tests
) -> APIRouter:
    """Create the training API router.

    Args:
        db_path:               Path to the SQLite database.
        coordinator:           Optional TrainingCaptureCoordinator.  When None the
                               router skips capture (coordinator calls are no-ops).
                               In production the server-side coordinator is injected
                               here; the GTK4 shell drives its own in-session
                               coordinator separately.
        teaching_orchestrator: Optional TeachingSessionOrchestrator (spec 004/US3).
                               When provided, POST /training opens an isolated context
                               and returns teaching_context in the response.
                               When None, the endpoint behaves as pre-spec-004.
    """
    init_schema(db_path)
    orchestrator = _get_orchestrator(db_path)
    router = APIRouter(prefix="/api/v1/training", tags=["training"])

    # coordinator may be None here; it's accessed per-request after binding.
    _coordinator_ref: list = [coordinator]  # mutable cell
    _teaching_orchestrator_ref: list = [teaching_orchestrator]  # mutable cell

    def _coord():
        return _coordinator_ref[0]

    @router.post("", response_model=TrainingState)
    async def create_session(payload: TrainingStartRequest) -> TrainingState:
        sid = uuid4()
        now = _now_iso()
        surface_kind = payload.surface_kind or "browser"
        with _conn(db_path) as c:
            c.execute(
                """
                INSERT INTO training_sessions (
                  session_id, skill_name, description, state, started_at, surface_kind
                ) VALUES (?, ?, ?, 'idle', ?, ?)
                """,
                (str(sid), payload.skill_name, payload.description, now, surface_kind),
            )

        teaching_ctx = None
        if _teaching_orchestrator_ref[0] is not None:
            teaching_ctx = _open_teaching_context(
                db_path=db_path,
                orchestrator_ref=_teaching_orchestrator_ref,
                session_id=sid,
                surface_kind=surface_kind,
                site_id=payload.site_id,
            )

        return TrainingState(
            session_id=str(sid),
            skill_name=payload.skill_name,
            description=payload.description,
            state="idle",
            started_at=now,
            stopped_at=None,
            signed_at=None,
            step_count=0,
            surface_kind=surface_kind,
            teaching_context=teaching_ctx,
        )

    @router.post("/{session_id}/start", response_model=TrainingState)
    async def start_recording(session_id: UUID) -> TrainingState:
        with _conn(db_path) as c:
            row = c.execute(
                "SELECT skill_name, description FROM training_sessions "
                "WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "session not found")
            skill_name = row["skill_name"]

            res = c.execute(
                "UPDATE training_sessions SET state = 'capturing' "
                "WHERE session_id = ? AND state = 'idle'",
                (str(session_id),),
            )
            if res.rowcount == 0:
                raise HTTPException(409, "session not in idle state")

        # Start the in-memory orchestrator session keyed by the DB session_id
        # so that compile_and_persist can look it up by the same UUID.
        _all_surfaces = frozenset(SurfaceKind)
        try:
            orchestrator.start(
                tenant_id=uuid4(),          # anonymous tenant for now
                human_user_id=uuid4(),      # anonymous user for now
                skill_id=skill_name,
                surface_kinds_allowed=_all_surfaces,
                session_id=session_id,      # pin to DB UUID (F6.1)
            )
        except Exception:
            logger.exception("orchestrator.start failed session=%s", session_id)
            # Don't block the DB state transition — capture degrades gracefully.

        coord = _coord()
        if coord is not None:
            try:
                # Pasa el skill_name real: coord.begin re-arranca el orquestador
                # (con voice_required) y sin esto sobrescribiría skill_id="skill".
                coord.begin(session_id=session_id, skill_name=skill_name)
            except Exception:
                logger.exception("coordinator.begin failed session=%s", session_id)

        # Launch the jailed browser so the operator has a real isolated surface
        # to demonstrate in. Uses the existing hermes-browser-launcher / Landlock
        # jail (HERMES_BROWSER_JAIL=1) or direct Chromium spawn in CI/dev
        # (HERMES_BROWSER_JAIL=0). Fail-soft: never blocks the state transition.
        await _launch_recording_browser(session_id)

        return _read_state(db_path, session_id)

    @router.post("/{session_id}/pause", response_model=TrainingState)
    async def pause_recording(session_id: UUID) -> TrainingState:
        """Pause an active recording session (capturing → paused). Idempotent."""
        with _conn(db_path) as c:
            res = c.execute(
                "UPDATE training_sessions SET state = 'paused' "
                "WHERE session_id = ? AND state = 'capturing'",
                (str(session_id),),
            )
            if res.rowcount == 0:
                # Idempotent: if already paused, return current state.
                row = c.execute(
                    "SELECT state FROM training_sessions WHERE session_id = ?",
                    (str(session_id),),
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "session not found")
                if row["state"] == "paused":
                    return _read_state(db_path, session_id)
                raise HTTPException(409, f"session not capturing (state={row['state']})")

        logger.info("training.pause session=%s", session_id)
        return _read_state(db_path, session_id)

    @router.post("/{session_id}/resume", response_model=TrainingState)
    async def resume_recording(session_id: UUID) -> TrainingState:
        """Resume a paused recording session (paused → capturing). Idempotent."""
        with _conn(db_path) as c:
            res = c.execute(
                "UPDATE training_sessions SET state = 'capturing' "
                "WHERE session_id = ? AND state = 'paused'",
                (str(session_id),),
            )
            if res.rowcount == 0:
                row = c.execute(
                    "SELECT state FROM training_sessions WHERE session_id = ?",
                    (str(session_id),),
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "session not found")
                if row["state"] == "capturing":
                    return _read_state(db_path, session_id)
                raise HTTPException(409, f"session not paused (state={row['state']})")

        logger.info("training.resume session=%s", session_id)
        return _read_state(db_path, session_id)

    @router.post("/{session_id}/cancel", response_model=TrainingState)
    async def cancel_recording(session_id: UUID) -> TrainingState:
        """Cancel a session in any active state (idle/capturing/paused/review → cancelled).

        Idempotent: already-cancelled sessions return 200 with current state.
        Stops the recording browser and cleans up the orchestrator.
        """
        with _conn(db_path) as c:
            res = c.execute(
                "UPDATE training_sessions SET state = 'cancelled', stopped_at = ? "
                "WHERE session_id = ? AND state IN ('idle','capturing','paused','review')",
                (_now_iso(), str(session_id)),
            )
            if res.rowcount == 0:
                row = c.execute(
                    "SELECT state FROM training_sessions WHERE session_id = ?",
                    (str(session_id),),
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "session not found")
                # Idempotent for already-terminal states.

        _stop_recording_browser(session_id)

        coord = _coord()
        if coord is not None:
            try:
                coord.end(session_id=session_id)
            except Exception:
                pass
        try:
            orchestrator.abandon(session_id=session_id, reason="user_cancel")
        except Exception:
            pass

        logger.info("training.cancel session=%s", session_id)
        return _read_state(db_path, session_id)

    @router.post("/{session_id}/stop", response_model=TrainingState)
    async def stop_recording(session_id: UUID) -> TrainingState:
        with _conn(db_path) as c:
            res = c.execute(
                "UPDATE training_sessions SET state = 'review', stopped_at = ? "
                "WHERE session_id = ? AND state IN ('capturing','paused')",
                (_now_iso(), str(session_id)),
            )
            if res.rowcount == 0:
                raise HTTPException(409, "session not capturing or paused")

        _stop_recording_browser(session_id)

        coord = _coord()
        if coord is not None:
            try:
                coord.end(session_id=session_id)
            except Exception:
                logger.exception("coordinator.end failed session=%s", session_id)

        # Transition orchestrator to REVIEWING state.
        _transition_orchestrator_to_review(orchestrator, session_id)

        return _read_state(db_path, session_id)

    @router.post("/{session_id}/sign", response_model=TrainingState)
    async def sign_session(session_id: UUID) -> TrainingState:
        with _conn(db_path) as c:
            row = c.execute(
                "SELECT skill_name, description, step_count FROM training_sessions "
                "WHERE session_id = ? AND state = 'review'",
                (str(session_id),),
            ).fetchone()
            if row is None:
                raise HTTPException(409, "session not in review state")
            skill_name = row["skill_name"]
            skill_description = (row["description"] or "").strip()

        now = _now_iso()

        # Generalize the demonstrated (now semantic) steps into a reusable skill body
        # via the LLM — best-effort. On no-provider / any error, generalized_body stays
        # None and compile_and_persist falls back to the verbatim semantic steps.
        generalized_body: str | None = None
        try:
            sess_obj = orchestrator.get_session(session_id=session_id)
            from hermes.shell_server.training.persist import format_steps_lines  # noqa: PLC0415
            trace, n_steps = format_steps_lines(list(getattr(sess_obj, "steps", []) or []))
            if n_steps:
                from hermes.shell_server.skills.skill_synthesis import (  # noqa: PLC0415
                    generalize_steps_to_body,
                )
                generalized_body = await generalize_steps_to_body(
                    name=skill_name,
                    description=skill_description,
                    steps_trace=trace,
                    db_path=db_path,
                )
        except Exception:  # noqa: BLE001 — LLM optional; fall back to verbatim steps
            logger.info("training.sign generalize skipped session=%s", session_id, exc_info=True)
            generalized_body = None

        # Recoge la voz capturada por el coordinator (si lo hay) para que el
        # gate de voz vea la transcripción. compile_and_persist FIRMA (aplicando
        # el invariante de voz) y luego compila — es el único que firma.
        coord = _coord()
        voice_captions: list[str] = []
        if coord is not None:
            try:
                voice_captions = coord.collected_voice_captions(session_id=session_id)
            except Exception:  # noqa: BLE001
                voice_captions = []

        try:
            persisted = compile_and_persist(
                db_path=db_path,
                orchestrator=orchestrator,
                session_id=session_id,
                skill_name=skill_name,
                signed_at=now,
                voice_captions=voice_captions,
                generalized_body=generalized_body,
            )
        except VoiceCaptureRequired as exc:
            # Mic activo, sin voz y sin opt-out: NO firmamos una skill muda.
            raise HTTPException(422, str(exc)) from exc
        except SkillContentBlockedError as exc:
            # Red-team 2026-06-19: la demostración contiene un patrón de troyano
            # CRÍTICO (dropper / reverse shell / exec ofuscado). NO acuñamos una
            # skill ejecutable. La sesión queda en 'review' (NO avanza a validated):
            # el `raise` sale de la función antes del UPDATE de estado de abajo, así
            # que NUNCA existe un paquete ejecutable ni una sesión "validada".
            logger.warning(
                "training.sign BLOCKED by content scan session=%s: %s",
                session_id, exc,
            )
            raise HTTPException(
                422, f"Skill bloqueada por el Centro de Seguridad: {exc}"
            ) from exc
        except Exception:
            logger.exception("skill_compile_persist failed session=%s", session_id)
            persisted = False

        # Count persisted steps.
        step_count = _count_steps(orchestrator, session_id)

        # Avanza a 'validated' (spec 004/US3: sign → validated, not autonomous).
        # Pre-spec-004 callers reading 'signed' can still function; the DB now
        # stores the canonical state 'validated'. `persisted` queda en log.
        logger.info(
            "training.sign session=%s persisted=%s steps=%s",
            session_id,
            persisted,
            step_count,
        )
        with _conn(db_path) as c:
            c.execute(
                "UPDATE training_sessions SET state = 'validated', signed_at = ?, "
                "step_count = ? WHERE session_id = ?",
                (now, step_count, str(session_id)),
            )

        return _read_state(db_path, session_id)

    @router.post("/{session_id}/synthesize")
    async def synthesize(session_id: UUID, request: Request) -> dict:
        """Turn the demonstration (name + written description) into a real SKILL.md
        via the active LLM, write it to the agent's skills dir + register it for the
        Skills list. This is the web path: no low-level step capture, the model
        synthesizes a generalizable skill (audio out of scope for now)."""
        with _conn(db_path) as c:
            row = c.execute(
                "SELECT skill_name, description, state FROM training_sessions "
                "WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "session not found")
        name = row["skill_name"]
        description = (row["description"] or "").strip()
        if not description:
            raise HTTPException(422, "Describe qué hace la skill y sus pasos para poder crearla.")

        from hermes.shell_server.skills.skill_synthesis import (  # noqa: PLC0415
            NoActiveProvider,
            synthesize_and_persist,
        )

        proxy = getattr(request.app.state, "dbus_proxy", None)

        try:
            meta = await synthesize_and_persist(
                db_path=db_path,
                name=name,
                description=description,
                dbus_proxy=proxy,
            )
        except HTTPException:
            raise
        except NoActiveProvider as exc:
            raise HTTPException(409, "Conecta un modelo en Proveedores para crear skills.") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("synthesize failed session=%s", session_id)
            raise HTTPException(502, "No se pudo sintetizar la skill. Reintenta.") from exc

        now = _now_iso()
        with _conn(db_path) as c:
            c.execute(
                "UPDATE training_sessions SET state = 'validated', signed_at = ?, "
                "step_count = 1 WHERE session_id = ?",
                (now, str(session_id)),
            )
        return {"ok": True, "skill": meta}

    @router.post("/{session_id}/abandon", response_model=TrainingState)
    async def abandon(session_id: UUID) -> TrainingState:
        with _conn(db_path) as c:
            res = c.execute(
                "UPDATE training_sessions SET state = 'abandoned' "
                "WHERE session_id = ? AND state IN ('idle','capturing','review')",
                (str(session_id),),
            )
            if res.rowcount == 0:
                raise HTTPException(404, "session not found or already done")

        coord = _coord()
        if coord is not None and coord.active_session_id() == session_id:
            try:
                coord.end(session_id=session_id)
            except Exception:
                pass
        try:
            orchestrator.abandon(session_id=session_id, reason="user_abandon")
        except Exception:
            pass

        return _read_state(db_path, session_id)

    @router.get("/{session_id}", response_model=TrainingState)
    async def get_state(session_id: UUID) -> TrainingState:
        return _read_state(db_path, session_id)

    @router.get("", response_model=list[TrainingState])
    async def list_sessions() -> list[TrainingState]:
        with _conn(db_path) as c:
            rows = c.execute(
                "SELECT * FROM training_sessions ORDER BY started_at DESC LIMIT 50"
            ).fetchall()
        return [_row_to_state(r) for r in rows]

    return router


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_state(db_path: Path, session_id: UUID) -> TrainingState:
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT * FROM training_sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    return _row_to_state(row)


def _row_to_state(row) -> TrainingState:
    keys = row.keys()
    surface_kind = row["surface_kind"] if "surface_kind" in keys else "browser"
    teaching_ctx_key = row["teaching_context_key"] if "teaching_context_key" in keys else None
    teaching_context = None
    if teaching_ctx_key is not None:
        teaching_context = TeachingContextResponse(
            isolation_key=teaching_ctx_key,
            surface_kind=surface_kind or "browser",
            input_owner="operator",
        )
    return TrainingState(
        session_id=row["session_id"],
        skill_name=row["skill_name"],
        description=row["description"],
        state=row["state"],
        started_at=row["started_at"],
        stopped_at=row["stopped_at"],
        signed_at=row["signed_at"],
        step_count=int(row["step_count"]),
        surface_kind=surface_kind or "browser",
        teaching_context=teaching_context,
    )


def _open_teaching_context(
    *,
    db_path: Path,
    orchestrator_ref: list,
    session_id: UUID,
    surface_kind: str,
    site_id: str,
) -> TeachingContextResponse | None:
    """Open an isolated teaching context and persist its isolation_key."""
    from hermes.agents_os.application.teaching.teaching_context import (  # noqa: PLC0415
        InputOwnershipViolation,
        SurfaceKind,
    )

    teach_orch = orchestrator_ref[0]
    if teach_orch is None:
        return None

    try:
        sk = SurfaceKind(surface_kind)
    except ValueError:
        sk = SurfaceKind.BROWSER

    # Use anonymous UUIDs for local personal-desktop (single-tenant).
    from uuid import UUID as _UUID  # noqa: PLC0415
    _LOCAL_TENANT = _UUID("a9501e55-0000-4000-8000-000000000001")
    _LOCAL_OPERATOR = _UUID("a9501e55-0000-4000-8000-000000000002")

    try:
        result = teach_orch.open_teaching_session(
            teaching_session_id=session_id,
            surface_kind=sk,
            tenant_id=_LOCAL_TENANT,
            operator_id=_LOCAL_OPERATOR,
            site_id=site_id,
        )
    except InputOwnershipViolation:
        raise
    except Exception:
        logger.exception("teaching_orchestrator.open failed session=%s", session_id)
        return None

    with _conn(db_path) as c:
        c.execute(
            "UPDATE training_sessions SET teaching_context_key = ? WHERE session_id = ?",
            (result.context.isolation_key, str(session_id)),
        )

    return TeachingContextResponse(
        context_id=str(result.context.context_id),
        isolation_key=result.context.isolation_key,
        surface_kind=surface_kind,
        input_owner="operator",
    )


def _transition_orchestrator_to_review(
    orchestrator: TrainingSessionOrchestrator,
    session_id: UUID,
) -> None:
    """Try to move the orchestrator session to REVIEWING; swallow if it has no steps."""
    try:
        sess = orchestrator.get_session(session_id=session_id)
    except Exception:
        return
    if sess.state in (TrainingSessionState.RECORDING, TrainingSessionState.PAUSED):
        try:
            orchestrator.request_review(session_id=session_id)
        except Exception as exc:
            # NoStepsCapturedError is expected if nothing was captured.
            logger.debug("request_review skipped session=%s: %s", session_id, exc)


def _count_steps(
    orchestrator: TrainingSessionOrchestrator, session_id: UUID
) -> int:
    try:
        return len(orchestrator.get_session(session_id=session_id).steps)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Browser launch/stop helpers (Part 1)
# ---------------------------------------------------------------------------
# We reuse the existing _BrowserController from agent_browser (which already
# encapsulates the Landlock jail / hermes-browser-launcher / direct Popen
# paths). Importing it here is safe: the module is always bundled.

async def _launch_recording_browser(session_id: UUID) -> None:
    """Launch the jailed recording browser for a training session.

    Fail-soft: logs the error without re-raising so the state transition
    is never blocked by a browser launch failure.
    """
    try:
        from hermes.shell_server.agent_browser import _browser_controller  # noqa: PLC0415
        await _browser_controller.start()
        logger.info("training.browser_launched session=%s", session_id)
    except Exception:
        logger.warning(
            "training.browser_launch_failed session=%s — "
            "el navegador no pudo abrirse; la sesión continúa sin captura visual",
            session_id,
            exc_info=True,
        )


def _stop_recording_browser(session_id: UUID) -> None:
    """Stop the recording browser (fail-soft)."""
    try:
        from hermes.shell_server.agent_browser import _browser_controller  # noqa: PLC0415
        _browser_controller.stop()
        logger.info("training.browser_stopped session=%s", session_id)
    except Exception:
        logger.warning(
            "training.browser_stop_failed session=%s",
            session_id,
            exc_info=True,
        )
