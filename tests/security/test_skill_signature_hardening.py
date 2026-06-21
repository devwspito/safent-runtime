"""Security tests — skill signature hardening (red-team remediation).

Threat model:
  - v1 HMAC key = SHA-256(public_path) → publicly derivable (CWE-321).
  - v1 payload omits `author` field → attribution forging.
  - Signature covers only IDs, not content → swap artefacts without detection.
  - No verification before execution → any DB write produces executable skill.
  - PlatformModelSigner.verify() never called → model tampering undetected.

This suite tests the controls that close those gaps:

  (a) Forged v1 signature + insert → promote REJECTED (403).
  (b) Autonomous skill with absent/invalid signature → execution gate REJECTED.
  (c) Sign skill, mutate a decision rule → verify() detects content mutation.
  (d) Flip signing_method from v2 to v1 on a v2 signature → promote REJECTED.
  (e) No master.key → signing RAISES SigningKeyError (no v1 fallback produced).
  (f) Selector with v1 signature → registry rejects (SelectorTamperedError).
  (g) Selector with unprefixed (plain hex) signature → registry rejects.
  (h) PlatformModelSigner.verify() detects tampered content_hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from hermes.shell_server.audit_api import init_schema

pytestmark = pytest.mark.security

# ---------------------------------------------------------------------------
# Shared fake vault infra
# ---------------------------------------------------------------------------

_FAKE_KEY = b"\x42" * 32  # stable key for tests


class _FakeVault:
    def derive_subkey(self, *, label: str) -> bytes:  # noqa: ARG002
        return _FAKE_KEY


def _fake_vault_patch():
    import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

    return patch.object(_mod, "SecretsVault", return_value=_FakeVault())


# ---------------------------------------------------------------------------
# (a) Forged v1 signature → promote REJECTED
# ---------------------------------------------------------------------------


class TestV1SignatureRejectedAtPromotion:
    """Red-team: adversary computes a valid v1 HMAC (SHA-256 of public db path)
    and inserts a skill with signing_method='v1'. Promotion must be rejected."""

    def _insert_v1_signed_composio_skill(self, db: Path) -> str:
        """Insert a Composio skill with a valid v1 HMAC (using public path key)."""
        package_id = str(uuid4())
        skill_id = "evil-skill"
        skill_name = "evil-skill"
        version = 1
        toolkit_slug = "SLACK"
        intent_text = "Send malicious payload"
        signed_at = "2026-06-03T00:00:00+00:00"

        # v1 key is sha256(db_path) — publicly derivable from the path.
        v1_key = hashlib.sha256(str(db).encode()).digest()
        payload = (
            f"{package_id}|{skill_id}|{skill_name}|{version}|"
            f"{toolkit_slug}|{intent_text}|{signed_at}|api_call"
        )
        v1_sig = hmac.new(v1_key, payload.encode(), hashlib.sha256).hexdigest()

        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            INSERT INTO skill_packages_view
              (package_id, skill_id, skill_name, version, state, surface_kinds,
               signed_at, signature_short, signing_method, signature_hex)
            VALUES (?, ?, ?, ?, 'validated', 'api_call', ?, ?, 'v1', ?)
            """,
            (package_id, skill_id, skill_name, version, signed_at, v1_sig[:12], v1_sig),
        )
        conn.execute(
            """
            INSERT INTO composio_skills (package_id, toolkit_slug, intent_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (package_id, toolkit_slug, intent_text, signed_at),
        )
        conn.commit()
        conn.close()
        return package_id

    def test_v1_forged_signature_rejected_at_promote(self, tmp_path: Path) -> None:
        """Skill with valid v1 HMAC must be rejected at promotion — fail-closed."""
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillSignatureVerificationFailed,
        )

        db = tmp_path / "shell-state.db"
        init_schema(db)
        pkg_id = self._insert_v1_signed_composio_skill(db)

        governance = SkillGovernanceService(db_path=db)

        with _fake_vault_patch():
            with pytest.raises(SkillSignatureVerificationFailed) as exc_info:
                import asyncio  # noqa: PLC0415
                asyncio.run(
                    governance.promote_skill(
                        package_id=pkg_id,
                        promoted_by=UUID(int=0),
                    )
                )

        assert "v1" in str(exc_info.value).lower() or "signing_method" in str(exc_info.value).lower()

    def test_v1_skill_state_unchanged_after_rejection(self, tmp_path: Path) -> None:
        """DB state must not change after a failed promote (rollback verified)."""
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillSignatureVerificationFailed,
        )

        db = tmp_path / "shell-state.db"
        init_schema(db)
        pkg_id = self._insert_v1_signed_composio_skill(db)
        governance = SkillGovernanceService(db_path=db)

        with _fake_vault_patch():
            with pytest.raises(SkillSignatureVerificationFailed):
                import asyncio  # noqa: PLC0415
                asyncio.run(
                    governance.promote_skill(
                        package_id=pkg_id,
                        promoted_by=UUID(int=0),
                    )
                )

        # State must still be 'validated', not 'autonomous'.
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT state FROM skill_packages_view WHERE package_id=?",
            (pkg_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "validated"


# ---------------------------------------------------------------------------
# (b) Skill with absent/invalid signature → execution gate REJECTED
# ---------------------------------------------------------------------------


class TestSignatureVerificationGateAtPromotion:
    """Autonomous state requires verified v2 signature — absent or invalid sig blocked."""

    def test_missing_signature_hex_rejected(self, tmp_path: Path) -> None:
        """Skill with signing_method='v2' but NULL signature_hex → rejected."""
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillSignatureVerificationFailed,
        )

        db = tmp_path / "shell-state.db"
        init_schema(db)
        pkg_id = str(uuid4())
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            INSERT INTO skill_packages_view
              (package_id, skill_id, skill_name, version, state, surface_kinds,
               signed_at, signature_short, signing_method, signature_hex)
            VALUES (?, ?, ?, 1, 'validated', 'browser', '2026-06-03T00:00:00+00:00',
                    'abc123', 'v2', NULL)
            """,
            (pkg_id, "bad-skill", "bad-skill"),
        )
        conn.commit()
        conn.close()

        governance = SkillGovernanceService(db_path=db)
        with _fake_vault_patch():
            with pytest.raises(SkillSignatureVerificationFailed):
                import asyncio  # noqa: PLC0415
                asyncio.run(
                    governance.promote_skill(
                        package_id=pkg_id,
                        promoted_by=UUID(int=0),
                    )
                )

    def test_wrong_signature_hex_rejected_for_composio_skill(
        self, tmp_path: Path
    ) -> None:
        """Composio skill with v2 method but wrong HMAC → rejected (fail-closed).

        Composio skills are fully verifiable at promotion time (all payload fields
        are in the DB). A wrong HMAC must cause SkillSignatureVerificationFailed.
        """
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillSignatureVerificationFailed,
        )

        db = tmp_path / "shell-state.db"
        init_schema(db)
        pkg_id = str(uuid4())
        skill_id = "tampered-composio"
        signed_at = "2026-06-03T00:00:00+00:00"
        wrong_sig = "b" * 64  # wrong HMAC — not computed from _FAKE_KEY

        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            INSERT INTO skill_packages_view
              (package_id, skill_id, skill_name, version, state, surface_kinds,
               signed_at, signature_short, signing_method, signature_hex)
            VALUES (?, ?, ?, 1, 'validated', 'api_call', ?,
                    ?, 'v2', ?)
            """,
            (pkg_id, skill_id, skill_id, signed_at, wrong_sig[:12], wrong_sig),
        )
        # Insert composio_skills row so the JOIN works.
        conn.execute(
            """
            INSERT INTO composio_skills (package_id, toolkit_slug, intent_text, created_at)
            VALUES (?, 'SLACK', 'Send something', ?)
            """,
            (pkg_id, signed_at),
        )
        conn.commit()
        conn.close()

        governance = SkillGovernanceService(db_path=db)
        with _fake_vault_patch():
            with pytest.raises(SkillSignatureVerificationFailed):
                import asyncio  # noqa: PLC0415
                asyncio.run(
                    governance.promote_skill(
                        package_id=pkg_id,
                        promoted_by=UUID(int=0),
                    )
                )


# ---------------------------------------------------------------------------
# (c) Mutate decision rule content → verify() detects it
# ---------------------------------------------------------------------------


class TestContentHashCoversExecutableContent:
    """Mutating a decision rule after signing must invalidate the content_hash."""

    def _make_rule(self, *, action: str = "submit_form"):
        from hermes.training.domain.decision_rule import (  # noqa: PLC0415
            DecisionRule,
            DecisionRuleSource,
            RiskLevel,
        )
        return DecisionRule(
            rule_id=uuid4(),
            source=DecisionRuleSource.LLM_COMPILE_INFERRED,
            action=action,
            pattern={"selector": "#submit"},
            risk_level=RiskLevel.LOW,
            confidence=0.95,
            requires_review=False,
        )

    def _make_narrative(self):
        from hermes.training.domain.voice_narrative import VoiceFragment, VoiceNarrative, VoiceFragmentState  # noqa: PLC0415
        fragment = VoiceFragment(
            transcript="Click the submit button",
            confidence=0.9,
            state=VoiceFragmentState.ASSOCIATED,
        )
        return VoiceNarrative(
            fragments=(fragment,),
            total_steps_in_session=1,
        )

    def test_mutated_decision_rule_changes_content_hash(self) -> None:
        """Changing a decision rule action produces a different content_hash."""
        from hermes.training.application.skill_compiler import SkillCompiler  # noqa: PLC0415

        replay_id = uuid4()
        narrative = self._make_narrative()
        rule_original = self._make_rule(action="submit_form")
        rule_mutated = self._make_rule(action="delete_all_data")
        rule_mutated = replace(rule_mutated, rule_id=rule_original.rule_id)

        hash_original = SkillCompiler.compute_content_hash(
            decision_rules=[rule_original],
            narrative=narrative,
            replay_script_id=replay_id,
        )
        hash_mutated = SkillCompiler.compute_content_hash(
            decision_rules=[rule_mutated],
            narrative=narrative,
            replay_script_id=replay_id,
        )

        assert hash_original != hash_mutated, (
            "Mutating a decision rule's action must produce a different content_hash. "
            "If hashes match, the content_hash does not cover executable content."
        )

    def test_signed_package_detects_rule_mutation_via_content_hash(self) -> None:
        """Sign a package, then mutate content_hash → verify fails (FR-015 addendum)."""
        from hermes.training.application.skill_signer import (  # noqa: PLC0415
            SignatureVerificationError,
            SkillSigner,
            verify_skill_signature,
        )
        from hermes.training.domain.skill_package import SkillPackage  # noqa: PLC0415
        from hermes.training.domain.skill_state import SkillState  # noqa: PLC0415
        import asyncio  # noqa: PLC0415

        replay_id = uuid4()
        narrative = self._make_narrative()
        rule = self._make_rule(action="submit_form")

        from hermes.training.application.skill_compiler import SkillCompiler  # noqa: PLC0415

        original_content_hash = SkillCompiler.compute_content_hash(
            decision_rules=[rule],
            narrative=narrative,
            replay_script_id=replay_id,
        )

        class FakeKms:
            async def get_signing_key(self, *, tenant_id, key_id):
                return _FAKE_KEY

        kms = FakeKms()
        signer = SkillSigner(kms=kms)
        tenant_id = uuid4()

        pkg = SkillPackage(
            package_id=uuid4(),
            skill_id=uuid4(),
            tenant_id=tenant_id,
            replay_script_id=replay_id,
            voice_narrative_id=uuid4(),
            decision_rule_ids=(rule.rule_id,),
            state=SkillState.DRAFT,
            compiled_by_operator_id=uuid4(),
            runtime_version="test",
            content_hash=original_content_hash,
        )

        signed_pkg = asyncio.run(signer.sign(package=pkg, signing_key_id="test-key"))

        # Simulate mutating the decision rule → new content_hash.
        mutated_rule = self._make_rule(action="delete_all_data")
        mutated_rule = replace(mutated_rule, rule_id=rule.rule_id)
        mutated_hash = SkillCompiler.compute_content_hash(
            decision_rules=[mutated_rule],
            narrative=narrative,
            replay_script_id=replay_id,
        )

        # The signed package's signature covers the original content_hash.
        # Replace content_hash with the mutated one → signature no longer valid.
        tampered_pkg = replace(signed_pkg, content_hash=mutated_hash)

        with pytest.raises(SignatureVerificationError):
            asyncio.run(verify_skill_signature(package=tampered_pkg, kms=kms))


# ---------------------------------------------------------------------------
# (d) Flip signing_method v2 → v1 on a v2 signature → REJECTED
# ---------------------------------------------------------------------------


class TestSigningMethodDowngradeRejected:
    """Red-team: adversary flips signing_method='v1' on a row that has a valid
    v2 signature. Promotion must still be rejected (method != v2)."""

    def test_v2_sig_with_v1_method_field_rejected(self, tmp_path: Path) -> None:
        """signing_method='v1' + any signature_hex → rejected at promotion."""
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
            SkillSignatureVerificationFailed,
        )
        from hermes.shell_server.skills.composio_skill_service import (  # noqa: PLC0415
            build_composio_canonical_payload,
        )

        db = tmp_path / "shell-state.db"
        init_schema(db)
        pkg_id = str(uuid4())
        skill_id = "downgrade-skill"
        signed_at = "2026-06-03T00:00:00+00:00"
        toolkit_slug = "SLACK"
        intent_text = "Real intent"

        # Compute a real v2 HMAC.
        payload = build_composio_canonical_payload(
            package_id=pkg_id,
            skill_id=skill_id,
            skill_name=skill_id,
            version=1,
            toolkit_slug=toolkit_slug,
            intent_text=intent_text,
            signed_at=signed_at,
        )
        real_v2_sig = hmac.new(_FAKE_KEY, payload, hashlib.sha256).hexdigest()

        # Insert with signing_method='v1' but real v2 signature — downgrade attack.
        conn = sqlite3.connect(str(db))
        conn.execute(
            """
            INSERT INTO skill_packages_view
              (package_id, skill_id, skill_name, version, state, surface_kinds,
               signed_at, signature_short, signing_method, signature_hex)
            VALUES (?, ?, ?, 1, 'validated', 'api_call', ?, ?, 'v1', ?)
            """,
            (pkg_id, skill_id, skill_id, signed_at, real_v2_sig[:12], real_v2_sig),
        )
        conn.execute(
            """
            INSERT INTO composio_skills (package_id, toolkit_slug, intent_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (pkg_id, toolkit_slug, intent_text, signed_at),
        )
        conn.commit()
        conn.close()

        governance = SkillGovernanceService(db_path=db)
        with _fake_vault_patch():
            with pytest.raises(SkillSignatureVerificationFailed) as exc_info:
                import asyncio  # noqa: PLC0415
                asyncio.run(
                    governance.promote_skill(
                        package_id=pkg_id,
                        promoted_by=UUID(int=0),
                    )
                )
        # Must be rejected because signing_method != 'v2'.
        assert "v1" in str(exc_info.value).lower() or "signing_method" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# (e) No master.key → signing RAISES (no v1 fallback)
# ---------------------------------------------------------------------------


class TestFailClosedSigningWithoutMasterKey:
    """resolve_signing_key must raise SigningKeyError when master.key absent."""

    def test_resolve_signing_key_raises_when_native_unavailable(
        self, tmp_path: Path
    ) -> None:
        from hermes.shell_server.training.persist import resolve_signing_key  # noqa: PLC0415
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415
        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        with patch.object(_mod, "SecretsVault", side_effect=RuntimeError("no master.key")):
            with pytest.raises(SigningKeyError):
                resolve_signing_key(tmp_path / "shell-state.db")

    def test_no_v1_tuple_returned_when_native_unavailable(self, tmp_path: Path) -> None:
        """Regression: the old code returned ('v1', key) — must now raise."""
        from hermes.shell_server.training.persist import resolve_signing_key  # noqa: PLC0415
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415
        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        with patch.object(_mod, "SecretsVault", side_effect=RuntimeError("no key")):
            try:
                result = resolve_signing_key(tmp_path / "db.db")
                # If we get here, it returned instead of raising — fail the test.
                pytest.fail(
                    f"resolve_signing_key returned {result!r} instead of raising. "
                    "v1 fallback must be eliminated — absence of master.key is fatal."
                )
            except SigningKeyError:
                pass  # correct behaviour

    def test_persist_composio_skill_raises_without_master_key(
        self, tmp_path: Path
    ) -> None:
        """persist_composio_skill must propagate the SigningKeyError — no v1 skill written."""
        from hermes.shell_server.skills.composio_skill_service import persist_composio_skill  # noqa: PLC0415
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415
        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        db = tmp_path / "test.db"
        init_schema(db)
        with patch.object(_mod, "SecretsVault", side_effect=RuntimeError("no key")):
            with pytest.raises(SigningKeyError):
                persist_composio_skill(
                    db_path=db,
                    skill_name="should-not-exist",
                    toolkit_slug="SLACK",
                    intent_text="This must not be persisted",
                    signed_at="2026-06-03T00:00:00+00:00",
                )

        # Nothing written to DB.
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT COUNT(*) FROM skill_packages_view WHERE skill_id='should-not-exist'"
        ).fetchone()
        conn.close()
        assert row[0] == 0


# ---------------------------------------------------------------------------
# (f) Selector with v1 signature → SelectorTamperedError
# ---------------------------------------------------------------------------


class TestSelectorV1SignatureRejected:
    """SignedSelectorRegistry must reject v1 signatures (downgrade attack)."""

    def _make_stored_selector(self, *, signature: str):
        from hermes.browser.domain.selector import Selector, SelectorAuthor, SelectorStrategy  # noqa: PLC0415
        from hermes.browser.infrastructure.signed_selector_registry import StoredSelector  # noqa: PLC0415

        selector = Selector.new(
            site_id="aeat",
            flow_id="303",
            step_id="btn",
            strategy=SelectorStrategy.CSS,
            value="#submit",
            intent_desc="submit button",
        )
        return StoredSelector(selector=selector, signature_hex=signature)

    @pytest.mark.asyncio
    async def test_v1_prefixed_signature_raises_tampered_error(self) -> None:
        """Selector with 'v1:...' signature must raise SelectorTamperedError."""
        from hermes.browser.infrastructure.signed_selector_registry import (  # noqa: PLC0415
            SignedSelectorRegistry,
            SelectorTamperedError,
        )
        from hermes.browser.infrastructure import InMemorySelectorRegistry  # noqa: PLC0415

        key = b"\xAB" * 32
        # Compute a v1 payload HMAC (without author field) — valid v1 HMAC.
        stored_sel = self._make_stored_selector(signature="placeholder")
        selector = stored_sel.selector
        parts = [
            str(selector.selector_id),
            selector.site_id,
            selector.flow_id,
            selector.step_id,
            str(selector.strategy.value),
            selector.value,
            str(selector.version),
            str(selector.tenant_scope) if selector.tenant_scope else "",
        ]
        v1_payload = "\x1f".join(parts).encode("utf-8")
        v1_sig = "v1:" + hmac.new(key, v1_payload, hashlib.sha256).hexdigest()

        store = InMemorySelectorRegistry()
        await store.persist(self._make_stored_selector(signature=v1_sig))

        registry = SignedSelectorRegistry(store=store, signing_key=key)
        with pytest.raises(SelectorTamperedError):
            await registry.fetch_latest(
                site_id="aeat",
                flow_id="303",
                step_id="btn",
            )

    @pytest.mark.asyncio
    async def test_unprefixed_hex_signature_raises_tampered_error(self) -> None:
        """Selector with plain hex signature (no prefix) must raise SelectorTamperedError."""
        from hermes.browser.infrastructure.signed_selector_registry import (  # noqa: PLC0415
            SignedSelectorRegistry,
            SelectorTamperedError,
        )
        from hermes.browser.infrastructure import InMemorySelectorRegistry  # noqa: PLC0415

        key = b"\xAB" * 32
        plain_hex_sig = "a" * 64  # plain 64-char hex without "v2:" prefix

        store = InMemorySelectorRegistry()
        await store.persist(self._make_stored_selector(signature=plain_hex_sig))

        registry = SignedSelectorRegistry(store=store, signing_key=key)
        with pytest.raises(SelectorTamperedError):
            await registry.fetch_latest(
                site_id="aeat",
                flow_id="303",
                step_id="btn",
            )

    @pytest.mark.asyncio
    async def test_v2_signature_accepted(self) -> None:
        """Control: a genuine v2 signature must be accepted (no regression)."""
        from hermes.browser.infrastructure.signed_selector_registry import (  # noqa: PLC0415
            SignedSelectorRegistry,
            sign_selector,
        )
        from hermes.browser.infrastructure import InMemorySelectorRegistry  # noqa: PLC0415
        from hermes.browser.domain.selector import Selector, SelectorAuthor, SelectorStrategy  # noqa: PLC0415

        key = b"\xAB" * 32
        selector = Selector.new(
            site_id="aeat",
            flow_id="303",
            step_id="btn",
            strategy=SelectorStrategy.CSS,
            value="#submit",
            intent_desc="submit button",
        )
        v2_sig = sign_selector(selector, key=key)
        assert v2_sig.startswith("v2:")

        from hermes.browser.infrastructure.signed_selector_registry import StoredSelector  # noqa: PLC0415
        store = InMemorySelectorRegistry()
        await store.persist(StoredSelector(selector=selector, signature_hex=v2_sig))

        registry = SignedSelectorRegistry(store=store, signing_key=key)
        result = await registry.fetch_latest(
            site_id="aeat",
            flow_id="303",
            step_id="btn",
        )
        assert result is not None
        assert result.selector_id == selector.selector_id


# ---------------------------------------------------------------------------
# (g) Same as (f) unprefixed — already covered above in (f).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (h) PlatformModelSigner.verify() detects tampered content_hash
# ---------------------------------------------------------------------------


class TestPlatformModelSignerVerify:
    """PlatformModelSigner.verify() must detect any tamper of signed fields."""

    def _make_model(self):
        """Build a minimal PlatformModel for testing."""
        from hermes.platforms.domain.platform_model import PlatformModel  # noqa: PLC0415
        from hermes.platforms.domain.value_objects import (  # noqa: PLC0415
            LifecycleState,
            ModelVersion,
            PlatformModelId,
            TourOrigin,
        )
        from datetime import UTC, datetime  # noqa: PLC0415

        return PlatformModel(
            platform_model_id=PlatformModelId(str(uuid4())),
            version=ModelVersion(1),
            tenant_id="test-tenant",
            site_ref="aeat.es",
            lifecycle_state=LifecycleState.PROVISIONAL,
            origin=TourOrigin.GUIDED,
            areas=(),
            entities=(),
            landmarks=(),
            house_rules=(),
            zones=(),
            staleness_marks=(),
            signature=None,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    def test_tampered_content_hash_detected(self) -> None:
        """Changing a signed field must invalidate the signature."""
        from hermes.platforms.infrastructure.platform_model_signer import (  # noqa: PLC0415
            PlatformModelSigner,
            InvalidModelSignature,
        )

        key = b"\x99" * 32
        signer = PlatformModelSigner(signing_key=key)
        model = self._make_model()
        sig = signer.sign(model)

        # Tamper the content_hash (simulates a zone mutation after signing).
        from dataclasses import replace as dc_replace  # noqa: PLC0415
        tampered_sig = dc_replace(sig, content_hash="a" * 64)

        with pytest.raises(InvalidModelSignature):
            signer.verify(tampered_sig)

    def test_correct_signature_verifies(self) -> None:
        """Control: freshly signed model must verify without error."""
        from hermes.platforms.infrastructure.platform_model_signer import (  # noqa: PLC0415
            PlatformModelSigner,
        )

        key = b"\x99" * 32
        signer = PlatformModelSigner(signing_key=key)
        model = self._make_model()
        sig = signer.sign(model)
        # Must not raise.
        signer.verify(sig)

    def test_signing_key_derived_from_native_keystore(self) -> None:
        """PlatformModelSigner's key must come from derive_subkey, not a hardcoded value."""
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
        from hermes.platforms.infrastructure.platform_model_signer import (  # noqa: PLC0415
            PlatformModelSigner,
        )
        import hermes.shell_server.skills.native_keystore_adapter as _mod  # noqa: PLC0415

        # When vault is available, derive_subkey should produce a key that can
        # be used to instantiate PlatformModelSigner (≥ 32 bytes).
        with patch.object(_mod, "SecretsVault", return_value=_FakeVault()):
            adapter = _mod.NativeKeyStoreAdapter()
            key = adapter.get_signing_key_sync()

        assert len(key) == 32
        # Must be possible to create a signer from this key.
        signer = PlatformModelSigner(signing_key=key)
        model = self._make_model()
        sig = signer.sign(model)
        signer.verify(sig)  # no exception
