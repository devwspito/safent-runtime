"""Application-layer service: create a Composio skill.

A Composio skill is a SkillPackage with:
  - surface_kinds = {API_CALL}            (no browser, no recording)
  - replay_script_id = None               (no screen recording)
  - state = VALIDATED                     (no recording to review)
  - composio_skills row = {toolkit_slug, intent_text}

Lifecycle decision (documented here, not just in code review):
  The DRAFT state exists to hold skills pending human review of a captured
  recording.  A Composio skill has no recording — the operator's intent text
  IS the review artifact, supplied at creation time.  We therefore persist
  directly as VALIDATED (matching the terminal output of the
  compile_and_persist → sign flow for recorded skills).

Execution seam (v1):
  WHAT RUNS: skill is created, signed, persisted to skill_packages_view +
    composio_skills.  Existing POST /skills/{id}/promote moves it
    VALIDATED → AUTONOMOUS.  get_composio_skill_detail() retrieves
    (toolkit_slug, intent_text) so a future agent loop can enqueue a task
    whose instruction = intent_text scoped to that toolkit.
  TODO (v2): wire get_composio_skill_detail() into the agent loop trigger so
    that when a AUTONOMOUS Composio skill fires, the run_cycle receives a
    WorkItem with instruction=intent_text and the ComposioClient pre-scoped
    to toolkit_slug.  The tools are already available globally via
    composio_tool_specs.py; the wire-up is a loop concern, not a skill concern.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hermes.shell_server.skills.composio_skill_errors import (
    ComposioCredentialMissing,
    ComposioSkillNameConflict,
    ComposioSkillValidationError,
    ComposioToolkitNotConnected,
)

logger = logging.getLogger(__name__)

# Composio surface is always API_CALL.
_SURFACE_KINDS = "api_call"

# Input caps (prevent DB and log bloat; not security-critical here).
_MAX_NAME_LEN = 120
_MAX_SLUG_LEN = 80
_MAX_INTENT_LEN = 2000

# Reject intent strings containing ASCII control characters (0x00–0x1F except
# common whitespace: \t \n \r).  These can indicate injection or encoding bugs.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_composio_skills_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS composio_skills (
          package_id   TEXT PRIMARY KEY,
          toolkit_slug TEXT NOT NULL,
          intent_text  TEXT NOT NULL,
          created_at   TEXT NOT NULL
        )
        """
    )


def _validate_inputs(skill_name: str, toolkit_slug: str, intent_text: str) -> None:
    if not skill_name or not skill_name.strip():
        raise ComposioSkillValidationError("skill_name must not be empty")
    if len(skill_name) > _MAX_NAME_LEN:
        raise ComposioSkillValidationError(
            f"skill_name exceeds {_MAX_NAME_LEN} characters"
        )
    if not toolkit_slug or not toolkit_slug.strip():
        raise ComposioSkillValidationError("toolkit_slug must not be empty")
    if len(toolkit_slug) > _MAX_SLUG_LEN:
        raise ComposioSkillValidationError(
            f"toolkit_slug exceeds {_MAX_SLUG_LEN} characters"
        )
    if not intent_text or not intent_text.strip():
        raise ComposioSkillValidationError("intent_text must not be empty")
    if len(intent_text) > _MAX_INTENT_LEN:
        raise ComposioSkillValidationError(
            f"intent_text exceeds {_MAX_INTENT_LEN} characters"
        )
    if _CONTROL_CHAR_RE.search(intent_text):
        raise ComposioSkillValidationError(
            "intent_text contains disallowed control characters"
        )


def _resolve_signing_key(db_path: Path) -> tuple[bytes, str]:
    """Return (key_bytes, 'v2') for signing a NEW Composio skill — fail-closed.

    Delegates to resolve_signing_key() from persist.py (single source of truth).
    Raises SigningKeyError if master.key is absent — no v1 fallback.
    """
    from hermes.shell_server.training.persist import resolve_signing_key  # noqa: PLC0415

    return resolve_signing_key(db_path)


def build_composio_canonical_payload(
    *,
    package_id: str,
    skill_id: str,
    skill_name: str,
    version: int,
    toolkit_slug: str,
    intent_text: str,
    signed_at: str,
) -> bytes:
    """Canonical payload bytes for a Composio skill HMAC.

    Covers all identifying + executable fields. Exposed so that the governance
    service can reconstruct the payload at promotion time for re-verification.
    """
    payload = (
        f"{package_id}|{skill_id}|{skill_name}|{version}|"
        f"{toolkit_slug}|{intent_text}|{signed_at}|api_call"
    )
    return payload.encode("utf-8")


def _sign_composio_skill(
    *,
    package_id: str,
    skill_id: str,
    skill_name: str,
    version: int,
    toolkit_slug: str,
    intent_text: str,
    signed_at: str,
    signing_key: bytes,
) -> str:
    """Return a 64-char HMAC-SHA256 hex string over the canonical payload."""
    payload = build_composio_canonical_payload(
        package_id=package_id,
        skill_id=skill_id,
        skill_name=skill_name,
        version=version,
        toolkit_slug=toolkit_slug,
        intent_text=intent_text,
        signed_at=signed_at,
    )
    return hmac.new(signing_key, payload, hashlib.sha256).hexdigest()


def _next_version_for_skill(conn: sqlite3.Connection, skill_id: str) -> int:
    """Read MAX(version)+1 inside the caller's transaction."""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM skill_packages_view WHERE skill_id = ?",
        (skill_id,),
    ).fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    return current + 1


async def verify_toolkit_connected(
    *,
    db_path: Path,
    toolkit_slug: str,
) -> None:
    """Assert that toolkit_slug is in the user's ACTIVE Composio accounts.

    Raises:
        ComposioCredentialMissing: if no Composio API key is configured.
        ComposioToolkitNotConnected: if the toolkit is not ACTIVE.
    """
    from hermes.integrations.composio.composio_client import (  # noqa: PLC0415
        ComposioClient,
    )
    from hermes.shell_server.integrations.domain import IntegrationNotFound  # noqa: PLC0415
    from hermes.shell_server.integrations.repo import (  # noqa: PLC0415
        SQLiteIntegrationsRepository,
    )
    from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

    try:
        repo = SQLiteIntegrationsRepository(db_path=db_path, vault=SecretsVault())
        api_key = repo.reveal_api_key(kind="composio")
    except IntegrationNotFound:
        api_key = None

    if not api_key:
        raise ComposioCredentialMissing(
            "Composio API key not configured. "
            "POST /api/v1/integrations/composio/key first."
        )

    integration = repo.get_or_none(kind="composio")
    entity_id = integration.entity_id if integration else "default"

    client = ComposioClient(api_key=api_key)
    accounts = await client.list_connected_accounts(entity_id)
    connected_slugs = {a.toolkit_slug.upper() for a in accounts}

    if toolkit_slug.upper() not in connected_slugs:
        raise ComposioToolkitNotConnected(toolkit_slug)


def persist_composio_skill(
    *,
    db_path: Path,
    skill_name: str,
    toolkit_slug: str,
    intent_text: str,
    signed_at: str,
) -> dict:
    """Create and persist a validated Composio SkillPackage.

    Returns a dict matching the SkillPackageDTO shape (used by the HTTP route).

    Raises:
        ComposioSkillValidationError: on bad inputs.
        ComposioSkillNameConflict: if the same version already exists.
    """
    _validate_inputs(skill_name, toolkit_slug, intent_text)

    skill_id = skill_name  # mirrors compile_and_persist: skill_id == skill_name
    package_id = str(uuid4())
    signing_key, signing_method = _resolve_signing_key(db_path)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_composio_skills_table(conn)

        version = _next_version_for_skill(conn, skill_id)

        signature_hex = _sign_composio_skill(
            package_id=package_id,
            skill_id=skill_id,
            skill_name=skill_name,
            version=version,
            toolkit_slug=toolkit_slug,
            intent_text=intent_text,
            signed_at=signed_at,
            signing_key=signing_key,
        )
        signature_short = signature_hex[:12]

        try:
            conn.execute(
                """
                INSERT INTO skill_packages_view (
                  package_id, skill_id, skill_name, version, state,
                  surface_kinds, signed_at, signature_short, validated_at,
                  signing_method, signature_hex
                ) VALUES (?, ?, ?, ?, 'validated', ?, ?, ?, ?, ?, ?)
                """,
                (
                    package_id,
                    skill_id,
                    skill_name,
                    version,
                    _SURFACE_KINDS,
                    signed_at,
                    signature_short,
                    signed_at,  # validated_at == created_at for Composio skills
                    signing_method,
                    signature_hex,  # full 64-char hex for verification at promote
                ),
            )
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise ComposioSkillNameConflict(skill_name, version) from exc

        conn.execute(
            """
            INSERT INTO composio_skills (package_id, toolkit_slug, intent_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (package_id, toolkit_slug, intent_text, signed_at),
        )
        conn.execute("COMMIT")
    except (ComposioSkillNameConflict, ComposioSkillValidationError):
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        logger.exception(
            "composio_skill.persist_failed skill=%s toolkit=%s",
            skill_name,
            toolkit_slug,
        )
        raise
    finally:
        conn.close()

    logger.info(
        "composio_skill.persisted package=%s skill=%s version=%s toolkit=%s",
        package_id,
        skill_id,
        version,
        toolkit_slug,
    )

    return {
        "package_id": package_id,
        "skill_id": skill_id,
        "skill_name": skill_name,
        "version": version,
        "state": "validated",
        "surface_kinds": [_SURFACE_KINDS],
        "skill_kind": "composio",
        "toolkit_slug": toolkit_slug,
        "signed_at": signed_at,
        "signature_short": signature_short,
        "validated_at": signed_at,
        "promoted_at": None,
        "signing_method": signing_method,
    }


def get_composio_skill_detail(
    *,
    db_path: Path,
    package_id: str,
) -> dict | None:
    """Return {package_id, toolkit_slug, intent_text, created_at} or None.

    Used by the agent loop (v2 TODO) to retrieve execution parameters for an
    AUTONOMOUS Composio skill.  Today this is the storage + retrieval half of
    the execution seam; enqueuing a WorkItem with instruction=intent_text is
    a loop concern wired in specs/005.
    """
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM composio_skills WHERE package_id = ?",
            (package_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "package_id": row["package_id"],
        "toolkit_slug": row["toolkit_slug"],
        "intent_text": row["intent_text"],
        "created_at": row["created_at"],
    }
