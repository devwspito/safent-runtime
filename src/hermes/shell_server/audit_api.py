"""Audit + Skills + Consent endpoints — leen estructuras del agents_os.

Audit: lista audit entries firmadas del AuditHashChainSigner.
Skills: lista SkillPackages firmadas (state, version, surface_kinds).
Consents: lista capabilities concedidas + revocadas.

Por ahora MOCK con datos sintéticos al boot (porque hermes-runtime.service
no expone DBus todavía — F11). Cuando F11 esté listo, las queries van
contra la DB compartida con el runtime.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes.agents_os.application.consent_manager import Capability, ConsentScope

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

CREATE TABLE IF NOT EXISTS skill_packages_view (
  package_id         TEXT PRIMARY KEY,
  skill_id           TEXT NOT NULL,
  skill_name         TEXT NOT NULL,
  version            INTEGER NOT NULL,
  state              TEXT NOT NULL,
  surface_kinds      TEXT NOT NULL,
  signed_at          TEXT NOT NULL,
  signature_short    TEXT,
  validated_at       TEXT,
  validated_by       TEXT,
  promoted_at        TEXT,
  promoted_by        TEXT,
  signing_method     TEXT NOT NULL DEFAULT 'v1'
);
CREATE INDEX IF NOT EXISTS skill_state_idx
  ON skill_packages_view (state, signed_at DESC);

CREATE TABLE IF NOT EXISTS composio_skills (
  package_id   TEXT PRIMARY KEY,
  toolkit_slug TEXT NOT NULL,
  intent_text  TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents_view (
  consent_id         TEXT PRIMARY KEY,
  capability         TEXT NOT NULL,
  scope              TEXT NOT NULL,
  granted_at         TEXT NOT NULL,
  granted_through    TEXT NOT NULL,
  expires_at         TEXT,
  revoked_at         TEXT,
  revoked_reason     TEXT
);
"""

# Idempotent migrations for skill_packages_view (spec 004 / US3 + P0-4).
_SKILL_PACKAGES_MIGRATIONS = [
    "ALTER TABLE skill_packages_view ADD COLUMN validated_at TEXT",
    "ALTER TABLE skill_packages_view ADD COLUMN validated_by TEXT",
    "ALTER TABLE skill_packages_view ADD COLUMN promoted_at TEXT",
    "ALTER TABLE skill_packages_view ADD COLUMN promoted_by TEXT",
    # Backfill: treat legacy 'signed' state as 'validated' (plan.md §3).
    "UPDATE skill_packages_view SET state = 'validated' WHERE state = 'signed'",
    # P0-4: signing_method column — 'v1'=path-HMAC (legacy), 'v2'=native keystore.
    # Default 'v1' applies to all rows that pre-date this migration.
    "ALTER TABLE skill_packages_view ADD COLUMN signing_method TEXT NOT NULL DEFAULT 'v1'",
    # Security hardening: full 64-char HMAC-SHA256 hex stored for verification
    # at promotion time (promote_skill re-verifies before AUTONOMOUS transition).
    # NULL for rows written before this migration (treated as unverifiable → v1).
    "ALTER TABLE skill_packages_view ADD COLUMN signature_hex TEXT",
]


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


def _seed_demo_data(db_path: Path) -> None:
    """Inserta unas entries demo para que la UI tenga qué mostrar."""
    with _conn(db_path) as c:
        existing = c.execute(
            "SELECT COUNT(*) AS n FROM audit_entries_view"
        ).fetchone()
        if existing["n"] > 0:
            return
        now = datetime.now(tz=UTC)

        # Audit entries demo (las que de verdad emite el runtime).
        for i, (kind, actor, desc) in enumerate(
            [
                (
                    "node_install_created",
                    "wizard",
                    "Nodo creado: personal-desktop arm64",
                ),
                (
                    "tenant_bound",
                    "wizard",
                    "Tenant bound: default",
                ),
                (
                    "consent_granted",
                    "hermes-user",
                    "Capability documents concedida (session)",
                ),
                (
                    "ota_queued",
                    "system",
                    "OTA v0.4.0 → v0.4.1 queued (channel stable)",
                ),
            ]
        ):
            c.execute(
                """
                INSERT INTO audit_entries_view (
                  entry_id, timestamp, actor, audit_kind, category,
                  description, signature_short
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    (now - timedelta(minutes=10 - i)).isoformat(),
                    actor,
                    kind,
                    None,
                    desc,
                    "abcd1234…",
                ),
            )


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


class CreateComposioSkillRequest(BaseModel):
    skill_name: str = Field(min_length=1, max_length=120)
    toolkit_slug: str = Field(min_length=1, max_length=80)
    intent_text: str = Field(min_length=1, max_length=2000)


class PromoteSkillRequest(BaseModel):
    confirm: bool


class ConsentDTO(BaseModel):
    consent_id: str
    capability: str
    scope: str
    granted_at: str
    granted_through: str
    expires_at: str | None
    revoked_at: str | None
    revoked_reason: str | None


class GrantConsentRequest(BaseModel):
    """Validated request body for POST /consents.

    FastAPI rejects unknown or empty capability values with 422 before the
    handler executes, ensuring only known OS capabilities are persisted.
    """

    capability: Capability
    scope: ConsentScope = ConsentScope.SESSION
    granted_through: str = "hermes_shell"


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
    surface_kinds = d.get("surface_kinds") or []
    if isinstance(surface_kinds, str):
        surface_kinds = surface_kinds.split(",") if surface_kinds else []
    toolkit_slug = d.get("toolkit_slug")
    skill_kind = "composio" if toolkit_slug else "recorded"
    return SkillPackageDTO(
        package_id=d["package_id"],
        skill_id=d["skill_id"],
        skill_name=d["skill_name"],
        version=int(d["version"]),
        state=d["state"],
        surface_kinds=surface_kinds,
        skill_kind=skill_kind,
        toolkit_slug=toolkit_slug,
        signed_at=d["signed_at"],
        signature_short=d.get("signature_short"),
        validated_at=d.get("validated_at"),
        promoted_at=d.get("promoted_at"),
        signing_method=d.get("signing_method", "v1"),
    )


def create_audit_router(db_path: Path) -> APIRouter:
    init_schema(db_path)
    _seed_demo_data(db_path)
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
    async def list_skills() -> list[SkillPackageDTO]:
        with _conn(db_path) as c:
            rows = c.execute(
                """
                SELECT spv.*, cs.toolkit_slug
                  FROM skill_packages_view spv
                  LEFT JOIN composio_skills cs ON cs.package_id = spv.package_id
                 ORDER BY spv.signed_at DESC
                """
            ).fetchall()
        return [_row_to_skill_dto(r) for r in rows]

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
    @router.get("/consents", response_model=list[ConsentDTO])
    async def list_consents(include_revoked: bool = False) -> list[ConsentDTO]:
        sql = "SELECT * FROM consents_view"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY granted_at DESC"
        with _conn(db_path) as c:
            rows = c.execute(sql).fetchall()
        return [
            ConsentDTO(
                consent_id=r["consent_id"],
                capability=r["capability"],
                scope=r["scope"],
                granted_at=r["granted_at"],
                granted_through=r["granted_through"],
                expires_at=r["expires_at"],
                revoked_at=r["revoked_at"],
                revoked_reason=r["revoked_reason"],
            )
            for r in rows
        ]

    @router.post("/consents", response_model=ConsentDTO, status_code=201)
    async def grant_consent(req: GrantConsentRequest) -> ConsentDTO:
        consent_id = str(uuid4())
        now = datetime.now(tz=UTC).isoformat()
        with _conn(db_path) as c:
            c.execute(
                """
                INSERT INTO consents_view (
                  consent_id, capability, scope, granted_at, granted_through
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    consent_id,
                    req.capability.value,
                    req.scope.value,
                    now,
                    req.granted_through,
                ),
            )
        return ConsentDTO(
            consent_id=consent_id,
            capability=req.capability.value,
            scope=req.scope.value,
            granted_at=now,
            granted_through=req.granted_through,
            expires_at=None,
            revoked_at=None,
            revoked_reason=None,
        )

    @router.delete("/consents/{consent_id}", status_code=204)
    async def revoke_consent(consent_id: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        with _conn(db_path) as c:
            res = c.execute(
                "UPDATE consents_view SET revoked_at = ?, "
                "revoked_reason = 'user_revoked' WHERE consent_id = ? "
                "AND revoked_at IS NULL",
                (now, consent_id),
            )
            if res.rowcount == 0:
                raise HTTPException(404, "consent not found or revoked")

    return router
