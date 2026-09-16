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
  - Embeds governance metadata (state, signature_hex, etc.) into the SKILL.md frontmatter.
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
from hermes.capabilities.application.skill_signer import (
    KmsSigningKeyPort,
    SigningKeyError,
    SkillSigner,
)
from hermes.capabilities.domain.skill_md_document import (
    SkillMdDocument,
    SkillMdParseError,
)
from hermes.capabilities.domain.skill_package import SkillPackage
from hermes.capabilities.domain.skill_state import SkillState
from hermes.capabilities.infrastructure.skill_md_codec import (
    parse_skill_md,
    serialize_skill_md,
    skill_md_content_bytes,
)

logger = logging.getLogger(__name__)

# Version string embedded in signed packages produced by the autonomous path.
_AUTONOMOUS_SKILL_SIGNING_KEY_ID = "skill-signing-v2"
_SIGNING_KEY_ID = _AUTONOMOUS_SKILL_SIGNING_KEY_ID

# Hard bound on agent-authored SKILL.md size (security-review 2026-06-26): the content
# scan runs synchronously on the daemon path and its split-dropper correlation is
# super-linear, so an unbounded blob is a DoS vector. 256 KiB is far above any real
# skill while keeping the scan bounded.
_MAX_SKILL_CONTENT_BYTES = 256 * 1024


class SkillStoreError(RuntimeError):
    """Unrecoverable error in the skill store adapter — skill not written."""


class SkillStoreAdapter:
    """SurfaceAdapterPort for the SKILL_STORE surface.

    Injected into SurfaceAdapterDispatcher under SurfaceKind.SKILL_STORE.
    Called by CapabilityBroker.dispatch() after HITL approval for skill_manage.

    Writes signed SKILL.md files to the Neus native skills directory so that
    both cage-approved skills and agent-created skills live in the same place.
    Governance fields (state, signing_method, signature_hex, package_id, etc.)
    are embedded in the SKILL.md frontmatter.metadata block — no separate DB
    table is used for the skill list. The composio_skills table is kept for
    Composio-origin skills (they have no on-disk SKILL.md).

    Args:
        kms:             KmsSigningKeyPort — provides v2 HMAC key material.
        db_path:         Path to the SQLite DB (used only for composio_skills).
        skill_store_root: Root directory for SKILL.md files on disk.
                         Default: $HERMES_HOME/skills (Neus native dir).
                         Falls back to /var/lib/hermes/hermes-home/skills.
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
        self._skill_store_root = skill_store_root or _neus_skills_root()
        self._runtime_version = runtime_version
        # Ensure the governance schema (skill_packages_view + composio_skills) exists
        # so that promote/deprecate operations work without the shell-server having
        # pre-created the DB. Fail-soft: schema errors are non-fatal at init.
        try:
            from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
                SkillGovernanceService,
            )
            SkillGovernanceService(db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.skill_store.governance_schema_init_failed db=%s: %s", db_path, exc
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

    @staticmethod
    def _content_scan_blocking(content: str) -> list[str]:
        """Scan agent-authored SKILL.md for trojan patterns; return blocking messages.

        EVERY skill the agent creates/edits/patches passes through the SAME domain
        scanner (agents_os.domain.skill_content_scan) the hub-mint endpoint enforces.
        A skill the agent writes is turned into a signed, auto-loadable artifact —
        the strict MINTING surface — so block on HIGH+ (droppers, reverse shells,
        obfuscated exec, persistence, priv-esc, destructive), not only CRITICAL.
        Empty list = safe to persist. FAIL-CLOSED (security-review 2026-06-26): the
        domain scanner is documented best-effort, but if it EVER raises we BLOCK (never
        silently allow). Patch routes through _upsert_skill, so this single chokepoint
        covers create + edit + patch; the caller scans BOTH the raw input AND the
        canonical serialized form (escape-smuggling defense). The execution-time cage
        (egress jail, install-gate, broker HITL, signature verify) still applies.
        """
        try:
            from hermes.agents_os.domain.skill_content_scan import (  # noqa: PLC0415
                ContentSeverity,
                has_high_or_critical_finding,
                scan_skill_markdown,
                scan_skill_text,
            )

            findings = scan_skill_markdown(content) + scan_skill_text(content)
            if not has_high_or_critical_finding(findings):
                return []
            blocking = (ContentSeverity.HIGH, ContentSeverity.CRITICAL)
            seen: set[str] = set()
            out: list[str] = []
            for f in findings:
                if f.severity in blocking and f.message not in seen:
                    seen.add(f.message)
                    out.append(f.message)
        except Exception as exc:  # noqa: BLE001 — fail-closed: a scanner crash must BLOCK
            logger.warning("hermes.skill_store.content_scan_error: %s", exc)
            return ["el escáner de contenido falló — bloqueado por seguridad (fail-closed)"]
        return out

    async def _upsert_skill(
        self, action: CapturedAction, skill_name: str
    ) -> ReplayOutcome:
        content = action.payload.get("content") or ""
        if not content:
            return ReplayOutcome.failed(
                action.action_id,
                error="skill_manage create/edit requires 'content' in parameters",
            )

        # Bound the input BEFORE the (synchronous, super-linear) content scan.
        if len(content.encode("utf-8", "ignore")) > _MAX_SKILL_CONTENT_BYTES:
            return ReplayOutcome.failed(
                action.action_id,
                error=(
                    f"SKILL.md excede el tamaño máximo "
                    f"({_MAX_SKILL_CONTENT_BYTES} bytes)."
                ),
            )

        # Security Center content gate — fail-closed BEFORE signing/writing. Todo
        # skill (incl. las que Neus crea sola) pasa por el scanner; HIGH+ no se firma.
        blocking = self._content_scan_blocking(content)
        if blocking:
            logger.warning(
                "hermes.skill_store.content_blocked name=%s patterns=%s",
                skill_name,
                blocking,
            )
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=(
                    "Skill bloqueada por el Centro de Seguridad: contenido peligroso "
                    "detectado — " + "; ".join(blocking[:3])
                ),
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

        # Re-scan the CANONICAL serialized form actually written + loaded — not just the
        # raw input (security-review 2026-06-26). parse→serialize can normalize escape
        # sequences / quoting, so a payload that slips the raw scan but resolves on
        # serialize is caught here BEFORE signing.
        blocking_serialized = self._content_scan_blocking(serialize_skill_md(doc))
        if blocking_serialized:
            logger.warning(
                "hermes.skill_store.content_blocked_serialized name=%s patterns=%s",
                skill_name,
                blocking_serialized,
            )
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=(
                    "Skill bloqueada por el Centro de Seguridad: contenido peligroso "
                    "tras normalizar — " + "; ".join(blocking_serialized[:3])
                ),
            )

        try:
            package = await self._sign_skill_document(doc, action)
        except (SigningKeyError, Exception) as exc:
            raise SkillStoreError(
                f"Signing failed for skill {skill_name!r}: {exc}"
            ) from exc

        now = datetime.now(tz=UTC).isoformat()
        doc_with_governance = self._embed_governance(doc, package, now)
        skill_dir = self._skill_dir(skill_name)
        self._write_skill_md_atomic(skill_dir, doc_with_governance)

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

        self._archive_skill_md(skill_name)
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

        content_hash = hashlib.sha256(skill_md_content_bytes(doc)).hexdigest()
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
    # Governance persistence — frontmatter, not a separate DB table
    # ------------------------------------------------------------------

    def _embed_governance(
        self,
        doc: SkillMdDocument,
        package: SkillPackage,
        signed_at: str,
    ) -> SkillMdDocument:
        """Return a new SkillMdDocument with governance fields in metadata.

        Merges the existing metadata with cage-issued governance fields so the
        SKILL.md file is the single source of truth for both skill instructions
        and provenance. list_skills_native() reads these back at query time.

        All fields used by build_canonical_payload() are stored here so that
        _skill_md_to_dto() can reconstruct the payload and re-verify the HMAC
        without a DB lookup (Finding #1 / CWE-345).
        """
        from uuid import UUID as _UUID  # noqa: PLC0415
        governance = {
            "package_id": str(package.package_id),
            "skill_id": str(package.skill_id),
            "state": SkillState.VALIDATED.value,
            "signing_method": "v2",
            "signature_hex": package.signature_hex or "",
            "signed_at": signed_at,
            "validated_at": signed_at,
            "surface_kinds": ["skill_store"],
            "version": 1,
            # Fields required for HMAC re-verification (Finding #1 / CWE-345).
            # build_canonical_payload() covers all of these.
            "content_hash": package.content_hash or "",
            "tenant_id": str(package.tenant_id) if package.tenant_id else str(_UUID(int=0)),
            "compiled_by_operator_id": str(package.compiled_by_operator_id) if package.compiled_by_operator_id else str(_UUID(int=0)),
            "created_at": package.created_at.isoformat() if package.created_at else signed_at,
            "runtime_version": package.runtime_version or "",
            "replay_script_id": str(package.replay_script_id) if package.replay_script_id else str(package.package_id),
            "voice_narrative_id": str(package.voice_narrative_id) if package.voice_narrative_id else str(package.package_id),
            "decision_rule_ids": [str(r) for r in (package.decision_rule_ids or [])],
        }
        merged_meta = {**doc.metadata, **governance}
        return SkillMdDocument(
            name=doc.name,
            description=doc.description,
            version=doc.version,
            body=doc.body,
            metadata=merged_meta,
        )

    def _archive_skill_md(self, skill_name: str) -> None:
        """Update state=archived in the SKILL.md frontmatter.metadata."""
        skill_file = self._skill_dir(skill_name) / "SKILL.md"
        if not skill_file.exists():
            return
        try:
            content = skill_file.read_text(encoding="utf-8")
            doc = parse_skill_md(content)
            archived_meta = {**doc.metadata, "state": "archived"}
            archived_doc = SkillMdDocument(
                name=doc.name,
                description=doc.description,
                version=doc.version,
                body=doc.body,
                metadata=archived_meta,
            )
            self._write_skill_md_atomic(skill_file.parent, archived_doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.skill_store.archive_frontmatter_failed name=%s: %s",
                skill_name,
                exc,
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
        content = serialize_skill_md(doc)

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


def _neus_skills_root() -> Path:
    """Return the Neus native skills root: $HERMES_HOME/skills/.

    Falls back to /var/lib/hermes/hermes-home/skills when HERMES_HOME is unset
    (matches the daemon's production value).
    """
    hermes_home = os.environ.get("HERMES_HOME") or "/var/lib/hermes/hermes-home"
    return Path(hermes_home) / "skills"


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
