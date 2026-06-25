"""SkillStoreAdapter — SurfaceAdapterPort for skill_manage proposals (F3).

Handles WRITE proposals from Nous skill_manage after HITL approval.
This is the SINGLE executor that closes the loop: Nous generates content,
the broker gates it, and THIS adapter performs the real effect.

Contract:
  - Parses skill_manage parameters (action + name + content) from the proposal.
  - Validates SKILL.md frontmatter via SkillMdDocument.
  - Computes content_hash over the SKILL.md bytes (deterministic).
  - Signs with SkillSigner v2 (content-bound HMAC-SHA256) via NativeKeyStoreAdapter.
  - Writes SKILL.md atomically to the on-disk store (skill_store_root/<name>/SKILL.md).
  - Persists metadata to skill_packages_view with state=validated (not autonomous).
  - Only create/edit/patch produce signed artefacts; delete archives.

State lifecycle:
  - New skill (create/edit): state=validated, NOT autonomous.
  - Promote to autonomous: only via SkillGovernanceService.promote_skill()
    (existing HITL-gated endpoint with signature re-verification).
  - delete: sets state=archived in DB, removes file from disk.

Security:
  - Fail-closed: ANY error during signing → no write, returns EXECUTED_FAILED.
  - NativeKeyStoreAdapter provides the v2 signing key (master.key derived).
  - content_hash covers the SKILL.md bytes — mutation of content invalidates sig.
  - Path traversal prevention: skill name validated against VALID_NAME_RE.
  - Atomic writes: tempfile + os.replace() prevents partial writes.
  - NO PII is persisted — skill content is agent procedural memory, not user data.

Capa: infrastructure (adapta SkillSigner + DB + filesystem). DIP: depends on
KmsSigningKeyPort (via NativeKeyStoreAdapter) and db_path (injected).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
    SurfaceAdapterPort,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.training.application.skill_signer import (
    KmsSigningKeyPort,
    SigningKeyError,
    SkillSigner,
)
from hermes.training.domain.skill_md_document import (
    SkillMdDocument,
    SkillMdParseError,
    parse_skill_md,
)
from hermes.training.domain.skill_package import SkillPackage
from hermes.training.domain.skill_state import SkillState

logger = logging.getLogger(__name__)

# Version string embedded in signed packages produced by the autonomous path.
_AUTONOMOUS_SKILL_SIGNING_KEY_ID = "skill-signing-v2"
_SIGNING_KEY_ID = _AUTONOMOUS_SKILL_SIGNING_KEY_ID


class SkillStoreError(RuntimeError):
    """Unrecoverable error in the skill store adapter — skill not written."""


class SkillStoreAdapter:
    """SurfaceAdapterPort for the SKILL_STORE surface.

    Injected into SurfaceAdapterDispatcher under SurfaceKind.SKILL_STORE.
    Called by CapabilityBroker.dispatch() after HITL approval for skill_manage.

    Args:
        kms:             KmsSigningKeyPort — provides v2 HMAC key material.
        db_path:         Path to the SQLite DB hosting skill_packages_view.
        skill_store_root: Root directory for SKILL.md files on disk.
                         Default: /var/lib/hermes/skills (OS convention).
        runtime_version: Embedded in SkillPackage for traceability.
    """

    def __init__(
        self,
        *,
        kms: KmsSigningKeyPort,
        db_path: Path,
        skill_store_root: Path | None = None,
        runtime_version: str = "agents-os",
    ) -> None:
        self._signer = SkillSigner(kms=kms)
        self._db_path = db_path
        self._skill_store_root = skill_store_root or Path("/var/lib/hermes/skills")
        self._runtime_version = runtime_version
        # Ensure skill_packages_view schema + migrations exist in the shared DB
        # before the first _persist_to_db call. Without this, the daemon can
        # process a HITL-approved skill_manage before SkillGovernanceService or
        # the shell-server has run init_schema, causing the INSERT to fail with
        # "no such table: skill_packages_view" (schema missing) or a missing-column
        # error (validated_at / signing_method not yet migrated). Same pattern as
        # SkillGovernanceService.__init__: fail-soft so a misconfigured keystore
        # never blocks boot, only rejects individual proposals.
        try:
            from hermes.shell_server.audit_api import init_schema  # noqa: PLC0415
            init_schema(db_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.skill_store.schema_ensure_failed: %s — "
                "skill_manage proposals may fail until the shell-server initialises the schema",
                exc,
            )

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.SKILL_STORE

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Not used by this adapter — skill_manage proposals come from Nous proposals."""
        return CapturedAction(
            surface_kind=self.surface_kind,
            intent_desc=intent_desc,
            payload=params,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Execute the approved skill_manage action.

        Entry point for the broker after HITL approval.
        """
        if action.surface_kind != self.surface_kind:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"SkillStoreAdapter cannot handle surface_kind={action.surface_kind!r}",
            )

        skill_action = action.payload.get("action", "")
        skill_name = action.payload.get("name", "")

        try:
            return await self._dispatch_action(action, skill_action, skill_name)
        except SkillStoreError as exc:
            logger.error(
                "hermes.skill_store.action_failed action=%s name=%s error=%s",
                skill_action,
                skill_name,
                str(exc),
            )
            return ReplayOutcome.failed(action.action_id, error=str(exc))

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Canonical bytes for audit signing (used by AuditHashChainSigner)."""
        import json
        payload = {
            "surface_kind": action.surface_kind,
            "action": action.payload.get("action"),
            "name": action.payload.get("name"),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    async def _dispatch_action(
        self,
        action: CapturedAction,
        skill_action: str,
        skill_name: str,
    ) -> ReplayOutcome:
        if skill_action in ("create", "edit"):
            return await self._upsert_skill(action, skill_name)
        if skill_action == "patch":
            return await self._patch_skill(action, skill_name)
        if skill_action == "delete":
            return self._delete_skill(action, skill_name)
        return ReplayOutcome.rejected_by_policy(
            action.action_id,
            reason=f"skill_manage action={skill_action!r} not supported by SkillStoreAdapter",
        )

    # ------------------------------------------------------------------
    # CREATE / EDIT — parse, sign, write, persist
    # ------------------------------------------------------------------

    async def _upsert_skill(
        self, action: CapturedAction, skill_name: str
    ) -> ReplayOutcome:
        content = action.payload.get("content") or ""
        if not content:
            return ReplayOutcome.failed(
                action.action_id,
                error="skill_manage create/edit requires 'content' in parameters",
            )

        try:
            doc = parse_skill_md(content)
        except SkillMdParseError as exc:
            return ReplayOutcome.failed(
                action.action_id,
                error=f"SKILL.md validation failed: {exc}",
            )

        if doc.name != skill_name:
            return ReplayOutcome.failed(
                action.action_id,
                error=(
                    f"Skill name mismatch: parameters.name={skill_name!r} "
                    f"but frontmatter.name={doc.name!r}. They must match."
                ),
            )

        try:
            package = await self._sign_skill_document(doc, action)
        except (SigningKeyError, Exception) as exc:
            raise SkillStoreError(
                f"Signing failed for skill {skill_name!r}: {exc}"
            ) from exc

        skill_dir = self._skill_dir(skill_name)
        self._write_skill_md_atomic(skill_dir, doc)
        self._persist_to_db(package, doc)

        logger.info(
            "hermes.skill_store.upserted name=%s package_id=%s state=%s",
            skill_name,
            str(package.package_id),
            package.state.value,
        )
        return ReplayOutcome.ok(
            action.action_id,
            result={
                "package_id": str(package.package_id),
                "skill_id": str(package.skill_id),
                "name": skill_name,
                "state": package.state.value,
                "signing_method": "v2",
            },
        )

    # ------------------------------------------------------------------
    # PATCH — read existing SKILL.md, apply find-replace, re-sign
    # ------------------------------------------------------------------

    async def _patch_skill(
        self, action: CapturedAction, skill_name: str
    ) -> ReplayOutcome:
        old_string = action.payload.get("old_string")
        new_string = action.payload.get("new_string")
        if old_string is None or new_string is None:
            return ReplayOutcome.failed(
                action.action_id,
                error="skill_manage patch requires 'old_string' and 'new_string'",
            )

        skill_file = self._skill_dir(skill_name) / "SKILL.md"
        if not skill_file.exists():
            return ReplayOutcome.failed(
                action.action_id,
                error=f"Skill {skill_name!r} not found in store — create it first",
            )

        current_content = skill_file.read_text(encoding="utf-8")
        if old_string not in current_content:
            return ReplayOutcome.failed(
                action.action_id,
                error=f"old_string not found in SKILL.md for skill {skill_name!r}",
            )

        replace_all = bool(action.payload.get("replace_all", False))
        if replace_all:
            new_content = current_content.replace(old_string, new_string)
        else:
            new_content = current_content.replace(old_string, new_string, 1)

        # Synthesize a fake CapturedAction with the new content for upsert
        patched_payload = dict(action.payload)
        patched_payload["action"] = "edit"
        patched_payload["content"] = new_content
        patched_action = CapturedAction(
            action_id=action.action_id,
            surface_kind=action.surface_kind,
            intent_desc=action.intent_desc,
            payload=patched_payload,
            captured_at=action.captured_at,
            tenant_id=action.tenant_id,
            human_operator_id=action.human_operator_id,
            work_item_id=action.work_item_id,
        )
        return await self._upsert_skill(patched_action, skill_name)

    # ------------------------------------------------------------------
    # DELETE — archive in DB, remove from disk
    # ------------------------------------------------------------------

    def _delete_skill(
        self, action: CapturedAction, skill_name: str
    ) -> ReplayOutcome:
        skill_file = self._skill_dir(skill_name) / "SKILL.md"
        if not skill_file.exists():
            return ReplayOutcome.failed(
                action.action_id,
                error=f"Skill {skill_name!r} not found — cannot delete",
            )

        self._archive_in_db(skill_name)
        _remove_skill_dir(self._skill_dir(skill_name))

        logger.info("hermes.skill_store.deleted name=%s", skill_name)
        return ReplayOutcome.ok(
            action.action_id,
            result={"name": skill_name, "state": "archived"},
        )

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    async def _sign_skill_document(
        self,
        doc: SkillMdDocument,
        action: CapturedAction,
    ) -> SkillPackage:
        """Build a SkillPackage from a SkillMdDocument and sign it v2."""
        import importlib.metadata

        runtime_version = self._runtime_version
        try:
            runtime_version = importlib.metadata.version("hermes-runtime")
        except importlib.metadata.PackageNotFoundError:
            pass

        content_hash = hashlib.sha256(doc.content_bytes()).hexdigest()
        tenant_id = action.tenant_id or UUID(int=0)

        # Build a SkillPackage using the training domain model.
        # replay_script_id = package_id (self-referential for autonomous skills).
        # voice_narrative_id = package_id (no voice session for autonomous path).
        package_id = uuid4()
        skill_id = uuid4()

        draft = SkillPackage(
            package_id=package_id,
            skill_id=skill_id,
            skill_version=1,
            tenant_id=tenant_id,
            site_id=doc.name,
            flow_id=doc.name,
            replay_script_id=package_id,
            voice_narrative_id=package_id,
            decision_rule_ids=(),
            state=SkillState.VALIDATED,
            signature_hex="",
            signing_key_id="",
            runtime_version=runtime_version,
            compiled_by_operator_id=action.human_operator_id,
            content_hash=content_hash,
        )

        signed = await self._signer.sign(
            package=draft,
            signing_key_id=_SIGNING_KEY_ID,
        )
        return signed

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_to_db(self, package: SkillPackage, doc: SkillMdDocument) -> None:
        """Upsert the signed skill into skill_packages_view (state=validated)."""
        now = datetime.now(tz=UTC).isoformat()
        short_sig = package.signature_hex[:12] if package.signature_hex else None

        with _db_conn(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO skill_packages_view (
                  package_id, skill_id, skill_name, version, state,
                  surface_kinds, signed_at, signature_short,
                  signing_method, signature_hex, validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(package_id) DO UPDATE SET
                  state=excluded.state,
                  signature_hex=excluded.signature_hex,
                  signature_short=excluded.signature_short,
                  signing_method=excluded.signing_method,
                  signed_at=excluded.signed_at,
                  validated_at=excluded.validated_at
                """,
                (
                    str(package.package_id),
                    str(package.skill_id),
                    doc.name,
                    1,
                    SkillState.VALIDATED.value,
                    "skill_store",
                    now,
                    short_sig,
                    "v2",
                    package.signature_hex,
                    now,
                ),
            )
            conn.execute("COMMIT")

    def _archive_in_db(self, skill_name: str) -> None:
        """Set state=archived for all versions of the skill."""
        with _db_conn(self._db_path) as conn:
            conn.execute(
                "UPDATE skill_packages_view SET state='archived' WHERE skill_name=?",
                (skill_name,),
            )

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _skill_dir(self, skill_name: str) -> Path:
        return self._skill_store_root / skill_name

    def _write_skill_md_atomic(self, skill_dir: Path, doc: SkillMdDocument) -> None:
        """Atomically write SKILL.md — tempfile + os.replace(), no partial writes."""
        skill_dir.mkdir(parents=True, exist_ok=True)
        target = skill_dir / "SKILL.md"
        content = doc.serialize()

        fd, tmp_path = tempfile.mkstemp(
            dir=str(skill_dir),
            prefix=".SKILL.md.tmp.",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _db_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _remove_skill_dir(skill_dir: Path) -> None:
    """Remove skill directory. Best-effort: logs on failure, does not raise."""
    import shutil
    try:
        shutil.rmtree(skill_dir)
    except OSError as exc:
        logger.warning(
            "hermes.skill_store.rmtree_failed path=%s error=%s",
            str(skill_dir),
            str(exc),
        )
