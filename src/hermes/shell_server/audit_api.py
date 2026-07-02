"""Audit + Skills endpoints — proyección de solo lectura del estado nativo.

Audit: lista audit entries firmadas que el AuditHashChainSigner nativo proyecta
  a `audit_entries_view` (`sqlite_audit_repository._try_project_to_view`). Esta
  capa SOLO lee esa proyección — no inventa filas ni firma nada.
Skills: lista skills desde el daemon (`list_skills_native`), fuente única en disco.

Consents: NO se sirven aquí. La fuente única es el ConsentManager nativo
  (D-Bus grant_consent/revoke_consent/list_consents → `consent_grants`), que es
  lo que consulta el gate. El antiguo store paralelo `consents_view` + sus
  endpoints REST se eliminaron (divergían del gate real y de la cadena firmada).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_entries_view (
  entry_id           TEXT PRIMARY KEY,
  timestamp          TEXT NOT NULL,
  actor              TEXT NOT NULL,
  audit_kind         TEXT NOT NULL,
  category           TEXT,
  description        TEXT NOT NULL,
  signature_short    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts_idx
  ON audit_entries_view (timestamp DESC);

CREATE TABLE IF NOT EXISTS composio_skills (
  package_id   TEXT PRIMARY KEY,
  toolkit_slug TEXT NOT NULL,
  intent_text  TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
"""

# skill_packages_view has been removed as the source of truth for native/recorded
# skills. Governance (state, signing_method, signature_hex) now lives in the
# SKILL.md frontmatter.metadata block written by SkillStoreAdapter. The list
# endpoint reads from the daemon (list_skills_native D-Bus verb) so that
# agent-created skills (which only exist on disk) always appear in Habilidades.
# Composio skills (no on-disk SKILL.md) retain their composio_skills row.
#
# Keep skill_packages_view compatible DDL for existing DBs (CREATE IF NOT EXISTS
# is safe to omit — the table may still exist from before this migration).
# Existing rows will be ignored: list_skills reads the daemon, not the table.
_SKILL_PACKAGES_MIGRATIONS: list[str] = []


def _conn(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def init_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _conn(db_path) as c:
        c.executescript("PRAGMA journal_mode=WAL;")
        c.executescript(_AUDIT_SCHEMA)
    _run_skill_migrations(db_path)


def _run_skill_migrations(db_path: Path) -> None:
    """Idempotent ALTER TABLE + backfill migrations for skill_packages_view."""
    with _conn(db_path) as c:
        for sql in _SKILL_PACKAGES_MIGRATIONS:
            try:
                c.execute(sql)
            except Exception as exc:  # noqa: BLE001
                if "duplicate column" not in str(exc).lower():
                    logger.warning("skill migration skipped: %s — %s", sql[:60], exc)


class AuditEntryDTO(BaseModel):
    entry_id: str
    timestamp: str
    actor: str
    audit_kind: str
    category: str | None
    description: str
    signature_short: str


class SkillPackageDTO(BaseModel):
    package_id: str
    skill_id: str
    skill_name: str
    version: int
    state: str
    surface_kinds: list[str]
    skill_kind: str = "recorded"
    toolkit_slug: str | None = None
    signed_at: str
    signature_short: str | None
    validated_at: str | None = None
    promoted_at: str | None = None
    signing_method: str = "v1"
    # Origin of the skill; "teaching_live" for skills minted via the live teaching
    # flow (surfaces as a "live" tag in the UI).
    teaching_origin: str | None = None


class CreateComposioSkillRequest(BaseModel):
    skill_name: str = Field(min_length=1, max_length=120)
    toolkit_slug: str = Field(min_length=1, max_length=80)
    intent_text: str = Field(min_length=1, max_length=2000)


class PromoteSkillRequest(BaseModel):
    confirm: bool


class SkillDetailsDTO(BaseModel):
    """Full skill details including the SKILL.md content for the viewer."""

    package_id: str
    skill_id: str
    skill_name: str
    version: int
    state: str
    surface_kinds: list[str]
    skill_kind: str = "recorded"
    toolkit_slug: str | None = None
    signed_at: str
    signature_short: str | None
    validated_at: str | None = None
    promoted_at: str | None = None
    signing_method: str = "v1"
    # The SKILL.md content (None when the file does not exist on disk,
    # e.g. Composio skills have no file, or the home dir is absent in CI).
    instructions: str | None = None
    instructions_path: str | None = None
    created_at: str | None = None  # alias for signed_at, for frontend convenience


def _row_to_skill_dto(row) -> SkillPackageDTO:
    """Map a skill_packages_view row to SkillPackageDTO.

    Treats legacy 'signed' state as 'validated' in read path (plan.md §3).
    Tolerates missing columns for DBs created before spec-004 migrations.
    Rows from the JOIN query include toolkit_slug from composio_skills.
    """
    keys = row.keys()
    state = row["state"]
    if state == "signed":
        state = "validated"
    validated_at = row["validated_at"] if "validated_at" in keys else None
    promoted_at = row["promoted_at"] if "promoted_at" in keys else None
    toolkit_slug = row["toolkit_slug"] if "toolkit_slug" in keys else None
    signing_method = row["signing_method"] if "signing_method" in keys else "v1"
    skill_kind = "composio" if toolkit_slug else "recorded"
    surface_kinds_raw = row["surface_kinds"] if row["surface_kinds"] else ""
    return SkillPackageDTO(
        package_id=row["package_id"],
        skill_id=row["skill_id"],
        skill_name=row["skill_name"],
        version=int(row["version"]),
        state=state,
        surface_kinds=surface_kinds_raw.split(",") if surface_kinds_raw else [],
        skill_kind=skill_kind,
        toolkit_slug=toolkit_slug,
        signed_at=row["signed_at"],
        signature_short=row["signature_short"],
        validated_at=validated_at,
        promoted_at=promoted_at,
        signing_method=signing_method or "v1",
    )


def _row_to_skill_dto_from_dict(d: dict) -> SkillPackageDTO:
    """Map a SkillGovernanceService result dict to SkillPackageDTO."""
    return _dict_to_skill_dto(d)


def _dict_to_skill_dto(d: dict) -> SkillPackageDTO:
    """Map a daemon list_skills_native result dict to SkillPackageDTO.

    Handles both native skills (from SKILL.md frontmatter) and composio
    skills (from the DB via the daemon wiring). Coerces surface_kinds from
    CSV string or list. Derives skill_kind from toolkit_slug presence.
    """
    surface_kinds = d.get("surface_kinds") or []
    if isinstance(surface_kinds, str):
        surface_kinds = surface_kinds.split(",") if surface_kinds else []
    toolkit_slug = d.get("toolkit_slug")
    skill_kind = "composio" if toolkit_slug else "recorded"
    state = d.get("state") or "native"
    if state == "signed":
        state = "validated"
    return SkillPackageDTO(
        package_id=d["package_id"],
        skill_id=d["skill_id"],
        skill_name=d["skill_name"],
        version=int(d.get("version") or 1),
        state=state,
        surface_kinds=surface_kinds,
        skill_kind=skill_kind,
        toolkit_slug=toolkit_slug,
        signed_at=d.get("signed_at") or "",
        signature_short=d.get("signature_short"),
        validated_at=d.get("validated_at"),
        promoted_at=d.get("promoted_at"),
        signing_method=d.get("signing_method") or "none",
        teaching_origin=d.get("teaching_origin"),
    )


_DEFAULT_HERMES_HOME = "/var/lib/hermes/hermes-home"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME)


def _local_skill_governance(db_path: Path) -> "object | None":
    """Return a minimal SkillGovernanceService-compatible object for the fallback
    composio scan. Returns None if the DB doesn't exist (so _list_composio_skills
    stays fail-soft).
    """
    if not db_path.exists():
        return None
    try:
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
        )
        return SkillGovernanceService(db_path=db_path)
    except Exception:  # noqa: BLE001
        return None


def _skill_id_to_slug(skill_id: str) -> str:
    """skill_id is already the slug (produced by skill_synthesis.slugify)."""
    return skill_id


def _read_skill_instructions(skill_id: str) -> tuple[str | None, str | None]:
    """Read the SKILL.md for the given skill_id from $HERMES_HOME/skills/<slug>/.

    Returns (content, absolute_path_str) or (None, None) when absent.
    Never raises: missing file or unreadable path yields (None, None).
    """
    slug = _skill_id_to_slug(skill_id)
    skill_path = _hermes_home() / "skills" / slug / "SKILL.md"
    try:
        content = skill_path.read_text(encoding="utf-8")
        return content, str(skill_path)
    except OSError:
        return None, None


def create_audit_router(db_path: Path) -> APIRouter:
    init_schema(db_path)
    router = APIRouter(prefix="/api/v1", tags=["audit"])

    # ---------------- Audit ----------------
    @router.get("/audit", response_model=list[AuditEntryDTO])
    async def list_audit(limit: int = 200) -> list[AuditEntryDTO]:
        with _conn(db_path) as c:
            rows = c.execute(
                "SELECT * FROM audit_entries_view ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            AuditEntryDTO(
                entry_id=r["entry_id"],
                timestamp=r["timestamp"],
                actor=r["actor"],
                audit_kind=r["audit_kind"],
                category=r["category"],
                description=r["description"],
                signature_short=r["signature_short"],
            )
            for r in rows
        ]

    # ---------------- Skills ----------------
    @router.get("/skills", response_model=list[SkillPackageDTO])
    async def list_skills(request: Request) -> list[SkillPackageDTO]:
        """List all skills from the daemon's native skill registry.

        Primary source: daemon list_skills_native (reads $HERMES_HOME/skills/).
        This ensures agent-created skills (which only exist on disk) always
        appear — the old skill_packages_view missed them (BUG 3).
        Composio skills (no on-disk SKILL.md) come from the daemon as well
        (the wiring merges them in list_skills → list_skills_native + composio).

        Fallback: when the daemon is unreachable, reads $HERMES_HOME/skills/
        directly (same filesystem scan the daemon would perform). This keeps
        the endpoint functional when D-Bus is not available (unit tests, CI,
        container pre-boot), without introducing a second source of truth.

        Fail-soft: returns [] when the daemon is unreachable AND HERMES_HOME
        is not set or the skills dir does not exist.
        """
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            _list_composio_skills,
            _list_native_skills_primary,
        )
        from hermes.tasks.control_plane.domain.ports import AgentUnavailable  # noqa: PLC0415

        proxy = getattr(request.app.state, "dbus_proxy", None)
        raw: list[dict] = []
        if proxy is not None:
            try:
                raw = await proxy.call_list("list_skills_native")
            except AgentUnavailable:
                raw = []
            except Exception:  # noqa: BLE001
                raw = []

        if not raw:
            # Daemon unavailable — read the filesystem directly (same logic the
            # daemon executes in list_skills_native). Also include composio skills
            # from the local DB (no on-disk SKILL.md for those).
            raw = _list_native_skills_primary()
            composio = _list_composio_skills(_local_skill_governance(db_path))
            seen_names = {s["skill_name"] for s in raw}
            raw += [s for s in composio if s["skill_name"] not in seen_names]

        return [_dict_to_skill_dto(d) for d in raw]

    @router.get("/skills/{package_id}/details", response_model=SkillDetailsDTO)
    async def get_skill_details(package_id: str, request: Request) -> SkillDetailsDTO:
        """Return full skill metadata + SKILL.md content for the skill viewer.

        Looks up the skill by package_id from the daemon's native list.
        The instructions field contains the raw SKILL.md text read from
        $HERMES_HOME/skills/<slug>/SKILL.md. Composio skills have no on-disk
        document; instructions will be None for them.

        Returns 404 if the package_id is not found.
        """
        from hermes.tasks.control_plane.domain.ports import AgentUnavailable  # noqa: PLC0415

        proxy = getattr(request.app.state, "dbus_proxy", None)
        raw: list[dict] = []
        if proxy is not None:
            try:
                raw = await proxy.call_list("list_skills_native")
            except (AgentUnavailable, Exception):  # noqa: BLE001
                raw = []

        matched = next((d for d in raw if d.get("package_id") == package_id), None)
        if matched is None:
            raise HTTPException(404, "skill not found")

        base = _dict_to_skill_dto(matched)
        instructions, instructions_path = _read_skill_instructions(base.skill_id)
        return SkillDetailsDTO(
            package_id=base.package_id,
            skill_id=base.skill_id,
            skill_name=base.skill_name,
            version=base.version,
            state=base.state,
            surface_kinds=base.surface_kinds,
            skill_kind=base.skill_kind,
            toolkit_slug=base.toolkit_slug,
            signed_at=base.signed_at,
            signature_short=base.signature_short,
            validated_at=base.validated_at,
            promoted_at=base.promoted_at,
            signing_method=base.signing_method,
            instructions=instructions,
            instructions_path=instructions_path,
            created_at=base.signed_at,
        )

    @router.post("/skills/composio", response_model=SkillPackageDTO, status_code=201)
    async def create_composio_skill(
        req: CreateComposioSkillRequest,
    ) -> SkillPackageDTO:
        """Create a validated Composio skill (HTTP passthrough — P0-1).

        Validates toolkit connectivity (network check) then delegates signing
        and persistence to SkillGovernanceService (single source of truth).
        """
        from hermes.shell_server.skills.composio_skill_errors import (  # noqa: PLC0415
            ComposioCredentialMissing,
            ComposioSkillNameConflict,
            ComposioSkillValidationError,
            ComposioToolkitNotConnected,
        )
        from hermes.shell_server.skills.composio_skill_service import (  # noqa: PLC0415
            verify_toolkit_connected,
        )
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
        )

        try:
            await verify_toolkit_connected(
                db_path=db_path,
                toolkit_slug=req.toolkit_slug,
            )
        except ComposioCredentialMissing as exc:
            raise HTTPException(503, str(exc)) from exc
        except ComposioToolkitNotConnected as exc:
            raise HTTPException(400, f"toolkit_not_connected: {exc}") from exc

        governance = SkillGovernanceService(db_path=db_path)
        try:
            dto_dict = await governance.sign_composio_skill(
                skill_name=req.skill_name,
                toolkit_slug=req.toolkit_slug,
                intent_text=req.intent_text,
                author_uid=0,
            )
        except ComposioSkillValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ComposioSkillNameConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "hermes.skills.composio.create_failed skill=%s", req.skill_name
            )
            raise HTTPException(500, "skill_creation_failed") from exc

        logger.info(
            "hermes.skills.composio.created package=%s skill=%s toolkit=%s",
            dto_dict["package_id"],
            req.skill_name,
            req.toolkit_slug,
        )
        return SkillPackageDTO(**dto_dict)

    @router.post("/skills/{package_id}/deprecate", response_model=SkillPackageDTO)
    async def deprecate_skill(package_id: str) -> SkillPackageDTO:
        """Deprecate a skill (HTTP passthrough to SkillGovernanceService — P0-1)."""
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillNotFound,
        )
        from uuid import UUID as _UUID  # noqa: PLC0415

        governance = SkillGovernanceService(db_path=db_path)
        try:
            result = await governance.deprecate_skill(
                package_id=package_id,
                deprecated_by=_UUID(int=0),
            )
        except SkillNotFound as exc:
            raise HTTPException(404, "skill not found or already deprecated") from exc
        return _row_to_skill_dto_from_dict(result)

    @router.post("/skills/{package_id}/promote", response_model=SkillPackageDTO)
    async def promote_skill(package_id: str, req: PromoteSkillRequest) -> SkillPackageDTO:
        """Promote a validated skill to autonomous (HTTP passthrough — P0-1).

        Delegates to SkillGovernanceService (single source of truth).
        Returns 409 if the skill is not in 'validated' state.
        """
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillNotFound,
            SkillSignatureVerificationFailed,
            SkillStateTransitionForbidden,
        )
        from uuid import UUID as _UUID  # noqa: PLC0415

        if not req.confirm:
            raise HTTPException(400, "confirm must be true to promote")

        governance = SkillGovernanceService(db_path=db_path)
        try:
            result = await governance.promote_skill(
                package_id=package_id,
                promoted_by=_UUID(int=0),
            )
        except SkillNotFound:
            raise HTTPException(404, "skill not found") from None
        except SkillStateTransitionForbidden as exc:
            raise HTTPException(409, f"invalid_transition: {exc}") from exc
        except SkillSignatureVerificationFailed as exc:
            logger.warning(
                "skill_package.promote_rejected_bad_signature package_id=%s", package_id
            )
            raise HTTPException(403, f"signature_verification_failed: {exc}") from exc

        logger.info("skill_package.promoted package_id=%s", package_id)
        return _row_to_skill_dto_from_dict(result)

    # ---------------- Consents ----------------
    # Los consents NO se sirven aquí: la fuente única es el ConsentManager nativo
    # (D-Bus grant_consent/revoke_consent/list_consents → tabla consent_grants),
    # que es lo que consulta el gate del daemon. La app QML de seguridad ya habla
    # ese D-Bus directamente. La antigua tabla-store paralela `consents_view` +
    # sus endpoints REST se eliminaron: eran un segundo store que NO pasaba por la
    # cadena firmada ni por el gate → divergía de la gobernanza real.

    return router
