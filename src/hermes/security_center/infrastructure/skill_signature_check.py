"""SkillSignatureCheck — verifies a skill's signature with a SECRET key.

Red-team 2026-06-19 (HIGH): the previous scheme was forgeable and never enforced.

  expected = hmac.new(read_bytes("/var/lib/hermes/keys/skills.pub"), identifier)

Two fatal flaws:

  1. The HMAC was keyed by a *readable* file mislabeled "public key". HMAC is a
     SECRET-key MAC, not an asymmetric signature: whoever can read the key can
     forge a valid tag. `skills.pub` was world/owner-readable, so anyone (the
     caged agent, an injection, any uid that reaches the file) could mint a
     signature that this very check would accept. (CWE-321 / CWE-322.)
  2. The signed message was only `target.identifier` — the skill *content* was
     never bound to the signature, so a valid tag for "pdf-tools" blessed ANY
     SKILL.md placed under that slug. Tampering was invisible.

This module now verifies the **same v2 signature the minter produces**
(see shell_server.skills.skill_synthesis.register_skill_row /
 shell_server.training.persist.compile_and_persist):

    key       = SecretsVault.derive_subkey(label="skill-signing-v2")   # SECRET,
                HKDF-SHA256 from master.key (0600 hermes:hermes, per-install,
                never on disk in the clear, never logged)
    canonical = f"{skill_id}\n{version}\n{skill_md}"                    # binds
                the EXACT on-disk content + version to the signature
    signature = hmac.new(key, canonical, sha256).hexdigest()           # stored
                as skill_packages_view.signature_hex

The check reconstructs `canonical` from the row in skill_packages_view + the
SKILL.md on disk and recomputes the MAC with the secret subkey. A skill is
accepted ONLY if a matching v2 signature verifies. Anything else — no row, no
signature, content/version drift, the deprecated forgeable v1 method, or an
absent master.key — is a CRITICAL finding (fail-closed): a public end-user OS
must not execute a skill whose provenance it cannot prove.

The subkey material lives only in process memory; this module never reads,
writes, or logs the key bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
from pathlib import Path

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.skill_signature")

_SKILL_SIGNING_LABEL = "skill-signing-v2"
_DEFAULT_HERMES_HOME = "/var/lib/hermes/hermes-home"
# skill_packages_view lives in the shell-state DB (shared by shell-server +
# daemon). Honour HERMES_SHELL_DB so dev/test overrides resolve the same row the
# minter wrote (see shell_server.main._DB_PATH).
_DEFAULT_DB_PATH = "/var/lib/hermes/shell-state.db"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME)


def _default_db_path() -> Path:
    return Path(os.environ.get("HERMES_SHELL_DB") or _DEFAULT_DB_PATH)


def _slugify(name: str) -> str:
    import re  # noqa: PLC0415

    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s


class SkillSignatureCheck:
    """Verifies a skill's v2 signature against the daemon's SECRET signing subkey.

    Fail-closed: any skill whose v2 signature cannot be verified is reported as a
    CRITICAL risk so the scan→score→gate refuses to install/promote it. The
    skill content itself (SKILL.md) is bound into the signed message, so tampering
    after minting is detected too.
    """

    name = "signature"

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        hermes_home: Path | None = None,
    ) -> None:
        # db_path / hermes_home are injectable for tests; production uses the
        # well-known locations shared with the minter.
        self._db_path = db_path
        self._hermes_home = hermes_home

    async def scan(self, target: InstallTarget) -> list[Risk]:
        if target.kind not in ("skill", "mcp_server"):
            return []
        # MCP servers are signed/verified by the runner launcher + provenance,
        # not by the skill keystore; the skill signature scheme applies to skills.
        if target.kind == "mcp_server":
            return []
        return self._verify_skill(target)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def _verify_skill(self, target: InstallTarget) -> list[Risk]:
        skill_id = _slugify(target.identifier)
        if not skill_id:
            return [self._critical(
                "skill identifier could not be resolved to a slug — cannot verify",
                f"signature:no_slug:{target.identifier}",
            )]

        key = self._signing_key()
        if key is None:
            # No master.key → minting is impossible and verification cannot be
            # trusted. Fail closed: a public OS must not run unverifiable skills.
            return [self._critical(
                "skill signing key unavailable (master.key missing/corrupt) — "
                "cannot verify skill signature",
                "signature:no_master_key",
            )]

        row = self._load_row(skill_id)
        if row is None:
            return [self._critical(
                f"no signed skill record for '{skill_id}' — skill is unsigned, "
                "verification cannot proceed",
                f"signature:no_record:{skill_id}",
            )]

        signing_method = (row["signing_method"] or "v1").strip().lower()
        signature_hex = (row["signature_hex"] or "").strip()
        version = row["version"]

        if signing_method != "v2":
            # v1 = SHA-256(path) HMAC = publicly forgeable. Never trust it for
            # execution; require a re-sign under v2.
            return [self._critical(
                f"skill '{skill_id}' signed with deprecated forgeable method "
                f"'{signing_method}' — re-sign required before it can run",
                f"signature:legacy_method:{skill_id}",
            )]

        if not signature_hex:
            return [self._critical(
                f"skill '{skill_id}' has no signature — refusing to verify an "
                "unsigned skill",
                f"signature:no_signature:{skill_id}",
            )]

        skill_md = self._read_skill_md(skill_id)
        if skill_md is None:
            return [self._critical(
                f"SKILL.md for '{skill_id}' not found on disk — cannot bind "
                "signature to content",
                f"signature:no_content:{skill_id}",
            )]

        canonical = f"{skill_id}\n{version}\n{skill_md}".encode()
        expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature_hex):
            return []

        return [self._critical(
            f"skill '{skill_id}' signature mismatch — content/version was "
            "tampered with after signing, or signed with a foreign key",
            f"signature:mismatch:{skill_id}",
        )]

    # ------------------------------------------------------------------
    # I/O helpers (best-effort; failures fail-closed via the callers above)
    # ------------------------------------------------------------------

    def _signing_key(self) -> bytes | None:
        """Derive the SECRET v2 signing subkey from master.key, or None.

        Mirrors NativeKeyStoreAdapter / persist.resolve_signing_key: HKDF-SHA256
        subkey of the per-install master.key (0600). Never logged.
        """
        try:
            from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

            return SecretsVault().derive_subkey(label=_SKILL_SIGNING_LABEL)
        except Exception as exc:  # noqa: BLE001 — absence/corruption → fail-closed
            logger.warning("hermes.security.signature_key_unavailable: %s", exc)
            return None

    def _db(self) -> Path:
        return self._db_path or _default_db_path()

    def _load_row(self, skill_id: str) -> sqlite3.Row | None:
        db_path = self._db()
        if not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(
                    "SELECT version, signing_method, signature_hex "
                    "FROM skill_packages_view WHERE skill_id = ? "
                    "ORDER BY version DESC LIMIT 1",
                    (skill_id,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("hermes.security.signature_db_error: %s", exc)
            return None

    def _read_skill_md(self, skill_id: str) -> str | None:
        path = (self._hermes_home or _hermes_home()) / "skills" / skill_id / "SKILL.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    @staticmethod
    def _critical(message: str, evidence_ref: str) -> Risk:
        return Risk(
            category="signature",
            severity=Severity.CRITICAL,
            message=message,
            evidence_ref=evidence_ref,
        )
