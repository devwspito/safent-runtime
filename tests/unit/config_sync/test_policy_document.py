"""Tests for PolicyBundle parsing, canonical_bytes, and signing_bytes.

Includes a committed test-vector for signing_bytes so the cloud team can
verify byte-for-byte that their signing implementation produces the same
sequence (signing drift is a silent misconfiguration).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hermes.config_sync.policy_document import (
    AccessScopeSpec,
    AgentSpec,
    LicenseSpec,
    PolicyBundle,
    PolicyPayload,
    canonical_bytes,
    signing_bytes,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_payload_dict() -> dict:
    return {
        "agents": [],
        "providers": [],
        "integrations": [],
        "mcp": [],
        "skills": [],
        "egress": {"allow_domains": []},
        "consents": [],
        "features": {"views": []},
        "license": {
            "plan": "starter",
            "max_agents": 5,
            "expires_at": "2027-01-01",
            "views": [],
        },
    }


def _bundle_dict(**overrides: object) -> dict:
    base = {
        "version": 1,
        "tenant_id": "tenant-abc",
        "issued_at": "2026-06-26T10:00:00Z",
        "signature_hex": "a" * 128,
        "payload": _minimal_payload_dict(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# PolicyBundle parsing
# ---------------------------------------------------------------------------


class TestPolicyBundleParsing:
    def test_valid_minimal_bundle_parses(self) -> None:
        bundle = PolicyBundle.model_validate(_bundle_dict())
        assert bundle.version == 1
        assert bundle.tenant_id == "tenant-abc"
        assert bundle.payload.agents == []
        assert bundle.payload.license.plan == "starter"

    def test_bundle_with_agent_spec_parses(self) -> None:
        payload = _minimal_payload_dict()
        payload["agents"] = [
            {
                "agent_id": "cloud-agent-1",
                "name": "Support",
                "role": "customer support",
                "provider_alias": "openai-gpt4",
                "autonomy_level": "balanced",
            }
        ]
        bundle = PolicyBundle.model_validate(_bundle_dict(payload=payload))
        agent = bundle.payload.agents[0]
        assert agent.agent_id == "cloud-agent-1"
        assert agent.provider_alias == "openai-gpt4"

    def test_bundle_with_features_parses(self) -> None:
        payload = _minimal_payload_dict()
        payload["features"] = {"views": ["calendar", "office"]}
        bundle = PolicyBundle.model_validate(_bundle_dict(payload=payload))
        assert bundle.payload.features.views == ["calendar", "office"]

    def test_bundle_with_egress_domains_parses(self) -> None:
        payload = _minimal_payload_dict()
        payload["egress"] = {"allow_domains": ["api.acme.com", "cdn.example.org"]}
        bundle = PolicyBundle.model_validate(_bundle_dict(payload=payload))
        assert "api.acme.com" in bundle.payload.egress.allow_domains

    def test_defaults_applied_for_missing_optional_fields(self) -> None:
        payload = _minimal_payload_dict()
        bundle = PolicyBundle.model_validate(_bundle_dict(payload=payload))
        assert bundle.payload.egress.allow_domains == []
        assert bundle.payload.consents == []

    def test_negative_version_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(version=-1))

    def test_empty_tenant_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(tenant_id=""))

    def test_signature_too_short_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(signature_hex="deadbeef"))

    def test_agent_with_empty_id_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["agents"] = [{"agent_id": "", "name": "Bad"}]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_agent_name_too_long_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["agents"] = [{"agent_id": "a1", "name": "X" * 121}]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_license_negative_max_agents_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["license"] = {
            "plan": "starter",
            "max_agents": -1,
            "expires_at": "",
            "views": [],
        }
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))


# ---------------------------------------------------------------------------
# P1-3: Cardinality caps
# ---------------------------------------------------------------------------


class TestCardinalityCaps:
    def test_agents_over_200_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["agents"] = [{"agent_id": f"a{i}", "name": f"Agent {i}"} for i in range(201)]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_agents_exactly_200_accepted(self) -> None:
        payload = _minimal_payload_dict()
        payload["agents"] = [{"agent_id": f"a{i}", "name": f"Agent {i}"} for i in range(200)]
        bundle = PolicyBundle.model_validate(_bundle_dict(payload=payload))
        assert len(bundle.payload.agents) == 200

    def test_providers_over_50_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["providers"] = [
            {"alias": f"p{i}", "kind": "openai", "default_model": "gpt-4"} for i in range(51)
        ]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_mcp_over_100_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["mcp"] = [{"server_id": f"m{i}", "argv": ["npx", "x"]} for i in range(101)]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_skills_over_200_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["skills"] = [{"identifier": f"skill-{i}"} for i in range(201)]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_consents_over_200_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["consents"] = [{"capability": f"cap-{i}"} for i in range(201)]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_egress_domains_over_500_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["egress"] = {"allow_domains": [f"d{i}.example.com" for i in range(501)]}
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))

    def test_mcp_env_over_100_keys_rejected(self) -> None:
        payload = _minimal_payload_dict()
        payload["mcp"] = [{"server_id": "m1", "env": {f"K{i}": "v" for i in range(101)}}]
        with pytest.raises(ValidationError):
            PolicyBundle.model_validate(_bundle_dict(payload=payload))


# ---------------------------------------------------------------------------
# canonical_bytes
# ---------------------------------------------------------------------------


class TestCanonicalBytes:
    def test_same_payload_produces_same_bytes(self) -> None:
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b1 = canonical_bytes(payload)
        b2 = canonical_bytes(payload)
        assert b1 == b2

    def test_key_order_is_stable(self) -> None:
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        raw = json.loads(canonical_bytes(payload))
        keys = list(raw.keys())
        assert keys == sorted(keys)

    def test_no_extra_whitespace(self) -> None:
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b = canonical_bytes(payload)
        text = b.decode("ascii")
        assert " " not in text

    def test_different_payloads_produce_different_bytes(self) -> None:
        p1 = PolicyPayload.model_validate(_minimal_payload_dict())
        pd2 = _minimal_payload_dict()
        pd2["license"]["plan"] = "enterprise"
        p2 = PolicyPayload.model_validate(pd2)
        assert canonical_bytes(p1) != canonical_bytes(p2)

    def test_signature_field_not_included(self) -> None:
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b = canonical_bytes(payload)
        assert b"signature" not in b

    def test_ascii_safe(self) -> None:
        pd = _minimal_payload_dict()
        pd["agents"] = [{"agent_id": "x1", "name": "José", "autonomy_level": "balanced"}]
        payload = PolicyPayload.model_validate(pd)
        canonical_bytes(payload).decode("ascii")  # must not raise


# ---------------------------------------------------------------------------
# P0-1: signing_bytes — envelope includes version + tenant_id + issued_at + payload
# ---------------------------------------------------------------------------


class TestSigningBytes:
    def test_signing_bytes_includes_envelope_fields(self) -> None:
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b = signing_bytes(
            version=7,
            tenant_id="tenant-xyz",
            issued_at="2026-06-26T10:00:00Z",
            payload=payload,
        )
        parsed = json.loads(b)
        assert parsed["version"] == 7
        assert parsed["tenant_id"] == "tenant-xyz"
        assert parsed["issued_at"] == "2026-06-26T10:00:00Z"
        assert "payload" in parsed

    def test_signing_bytes_keys_sorted(self) -> None:
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-01-01T00:00:00Z", payload=payload)
        parsed = json.loads(b)
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_signing_bytes_ascii_no_spaces(self) -> None:
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-01-01T00:00:00Z", payload=payload)
        assert " " not in b.decode("ascii")

    def test_mutating_version_invalidates_match(self) -> None:
        """Different version → different bytes → would invalidate any signature over v1."""
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b1 = signing_bytes(version=1, tenant_id="t", issued_at="2026-01-01T00:00:00Z", payload=payload)
        b2 = signing_bytes(version=2, tenant_id="t", issued_at="2026-01-01T00:00:00Z", payload=payload)
        assert b1 != b2

    def test_mutating_tenant_id_invalidates_match(self) -> None:
        """Different tenant_id → different bytes → cross-tenant replay blocked."""
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b1 = signing_bytes(version=1, tenant_id="tenant-a", issued_at="2026-01-01T00:00:00Z", payload=payload)
        b2 = signing_bytes(version=1, tenant_id="tenant-b", issued_at="2026-01-01T00:00:00Z", payload=payload)
        assert b1 != b2

    def test_mutating_issued_at_invalidates_match(self) -> None:
        """Different issued_at → different bytes → timestamp rollback blocked."""
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b1 = signing_bytes(version=1, tenant_id="t", issued_at="2026-01-01T00:00:00Z", payload=payload)
        b2 = signing_bytes(version=1, tenant_id="t", issued_at="2026-01-02T00:00:00Z", payload=payload)
        assert b1 != b2

    def test_signing_bytes_differs_from_canonical_bytes(self) -> None:
        """signing_bytes must NOT equal canonical_bytes(payload) — it wraps the envelope."""
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        sb = signing_bytes(version=1, tenant_id="t", issued_at="2026-01-01T00:00:00Z", payload=payload)
        cb = canonical_bytes(payload)
        assert sb != cb

    # ------------------------------------------------------------------
    # Committed test vector (P0-1)
    # ------------------------------------------------------------------
    # This vector pins the exact byte sequence so that the cloud signing
    # implementation and the associate verifier cannot drift independently.
    #
    # To regenerate:
    #   PYTHONPATH=src python3 -c "
    #   from hermes.config_sync.policy_document import PolicyPayload, signing_bytes
    #   p = PolicyPayload()
    #   b = signing_bytes(version=1, tenant_id='test-tenant',
    #                     issued_at='2026-06-26T10:00:00Z', payload=p)
    #   print(b.decode())
    #   "
    # ------------------------------------------------------------------

    _VECTOR_VERSION = 1
    _VECTOR_TENANT = "test-tenant"
    _VECTOR_ISSUED_AT = "2026-06-26T10:00:00Z"

    def _vector_payload(self) -> PolicyPayload:
        return PolicyPayload.model_validate(
            {
                "agents": [],
                "providers": [],
                "integrations": [],
                "mcp": [],
                "skills": [],
                "egress": {"allow_domains": []},
                "consents": [],
                "features": {"views": []},
                "license": {"plan": "starter", "max_agents": 5, "expires_at": "", "views": []},
            }
        )

    def test_vector_is_deterministic(self) -> None:
        """Same inputs must produce identical bytes across runs."""
        payload = self._vector_payload()
        b1 = signing_bytes(
            version=self._VECTOR_VERSION,
            tenant_id=self._VECTOR_TENANT,
            issued_at=self._VECTOR_ISSUED_AT,
            payload=payload,
        )
        b2 = signing_bytes(
            version=self._VECTOR_VERSION,
            tenant_id=self._VECTOR_TENANT,
            issued_at=self._VECTOR_ISSUED_AT,
            payload=payload,
        )
        assert b1 == b2

    def test_vector_expected_shape(self) -> None:
        """The envelope shape is exactly four sorted top-level keys.

        If this test fails, the signing_bytes format changed.  Update the
        cloud signing code to match before deploying — a mismatch here means
        ALL signatures from the cloud will be rejected by the associate.
        """
        payload = self._vector_payload()
        b = signing_bytes(
            version=self._VECTOR_VERSION,
            tenant_id=self._VECTOR_TENANT,
            issued_at=self._VECTOR_ISSUED_AT,
            payload=payload,
        )
        parsed = json.loads(b)

        assert list(parsed.keys()) == ["issued_at", "payload", "tenant_id", "version"]
        assert parsed["version"] == 1
        assert parsed["tenant_id"] == "test-tenant"
        assert parsed["issued_at"] == "2026-06-26T10:00:00Z"

        payload_keys = list(parsed["payload"].keys())
        assert payload_keys == sorted(payload_keys)

        assert parsed["payload"]["agents"] == []
        assert parsed["payload"]["providers"] == []
        assert parsed["payload"]["license"]["plan"] == "starter"
        assert parsed["payload"]["license"]["max_agents"] == 5


# ---------------------------------------------------------------------------
# AccessScopeSpec — Enterprise Fase 2 Phase 3 (per-agent access scope on AgentSpec)
# ---------------------------------------------------------------------------


class TestAccessScopeSpecDefaults:
    def test_defaults(self) -> None:
        scope = AccessScopeSpec()
        assert scope.enforced is False
        assert scope.cerebro_unrestricted is True
        assert scope.native_tools == []
        assert scope.policy_overlay == {}
        assert scope.views == []


class TestAccessScopeSpecCardinalityCaps:
    def test_native_tools_over_256_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AccessScopeSpec(native_tools=[f"tool{i}" for i in range(257)])

    def test_native_tools_exactly_256_accepted(self) -> None:
        scope = AccessScopeSpec(native_tools=[f"tool{i}" for i in range(256)])
        assert len(scope.native_tools) == 256

    def test_views_over_256_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AccessScopeSpec(views=[f"view{i}" for i in range(257)])

    def test_policy_overlay_over_256_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AccessScopeSpec(
                policy_overlay={f"tool{i}": {"enabled": True} for i in range(257)}
            )

    def test_policy_overlay_exactly_256_keys_accepted(self) -> None:
        scope = AccessScopeSpec(
            policy_overlay={f"tool{i}": {"enabled": True} for i in range(256)}
        )
        assert len(scope.policy_overlay) == 256

    def test_policy_overlay_non_bool_enabled_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AccessScopeSpec(policy_overlay={"terminal": {"enabled": ["not", "a", "bool"]}})


class TestAgentSpecAccessScopeRoundTrip:
    def _scope(self) -> AccessScopeSpec:
        return AccessScopeSpec(
            enforced=True,
            cerebro_unrestricted=False,
            native_tools=["execute_code", "terminal"],
            policy_overlay={"send_message": {"enabled": False}},
            views=["calendar"],
        )

    def test_build_signing_bytes_parse_round_trips(self) -> None:
        agent = AgentSpec(agent_id="cloud-agent-1", name="Support", access_scope=self._scope())
        payload = PolicyPayload(agents=[agent])

        b = signing_bytes(
            version=1, tenant_id="test-tenant", issued_at="2026-06-26T10:00:00Z", payload=payload
        )
        parsed_payload = PolicyPayload.model_validate(json.loads(b)["payload"])

        assert parsed_payload.agents[0].access_scope is not None
        assert parsed_payload.agents[0].access_scope.model_dump() == self._scope().model_dump()

    def test_agent_with_access_scope_none_explicit_parses_none(self) -> None:
        agent = AgentSpec.model_validate({"agent_id": "a1", "name": "X", "access_scope": None})
        assert agent.access_scope is None


class TestAgentSpecAccessScopeBackCompat:
    """A bundle without access_scope must parse + sign BYTE-IDENTICALLY to
    before this field existed — `access_scope` must never appear (not even as
    null) in the serialized dict when it is unset."""

    def test_agent_without_access_scope_parses_to_none(self) -> None:
        agent = AgentSpec.model_validate({"agent_id": "a1", "name": "X"})
        assert agent.access_scope is None

    def test_access_scope_key_absent_from_dump_when_none(self) -> None:
        agent = AgentSpec(agent_id="a1", name="X")
        payload = PolicyPayload(agents=[agent])
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-06-26T10:00:00Z", payload=payload)
        parsed_agent = json.loads(b)["payload"]["agents"][0]
        assert "access_scope" not in parsed_agent

    def test_signing_bytes_unaffected_by_the_new_field_existing(self) -> None:
        """Same agent dict (no access_scope key), signed before/after the field
        was added, must produce the exact same bytes — the model addition is
        purely additive for callers that never populate it."""
        agent_dict = {"agent_id": "a1", "name": "Support", "provider_alias": "gpt4"}
        payload = PolicyPayload.model_validate({"agents": [agent_dict]})
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-06-26T10:00:00Z", payload=payload)
        parsed_agent = json.loads(b)["payload"]["agents"][0]
        assert "access_scope" not in parsed_agent
        assert parsed_agent["provider_alias"] == "gpt4"


# ---------------------------------------------------------------------------
# Committed test vector — AgentSpec.access_scope signing bytes (Fase 2 Phase 3)
# ---------------------------------------------------------------------------
#
# This vector pins the exact AccessScopeSpec wire shape so the cloud signing
# implementation and the associate verifier cannot drift independently.
#
# To regenerate:
#   PYTHONPATH=src python3 -c "
#   from hermes.config_sync.policy_document import (
#       AccessScopeSpec, AgentSpec, PolicyPayload, signing_bytes)
#   scope = AccessScopeSpec(
#       enforced=True, cerebro_unrestricted=False,
#       native_tools=['execute_code', 'terminal'],
#       policy_overlay={'send_message': {'enabled': False}},
#       views=['calendar'])
#   agent = AgentSpec(agent_id='cloud-agent-1', name='Support', access_scope=scope)
#   payload = PolicyPayload(agents=[agent])
#   b = signing_bytes(version=1, tenant_id='test-tenant',
#                      issued_at='2026-06-26T10:00:00Z', payload=payload)
#   print(b.decode())
#   "


class TestAccessScopeSigningVector:
    _VECTOR_VERSION = 1
    _VECTOR_TENANT = "test-tenant"
    _VECTOR_ISSUED_AT = "2026-06-26T10:00:00Z"

    def _vector_payload(self) -> PolicyPayload:
        scope = AccessScopeSpec(
            enforced=True,
            cerebro_unrestricted=False,
            native_tools=["execute_code", "terminal"],
            policy_overlay={"send_message": {"enabled": False}},
            views=["calendar"],
        )
        agent = AgentSpec(agent_id="cloud-agent-1", name="Support", access_scope=scope)
        return PolicyPayload(agents=[agent])

    def _vector_bytes(self) -> bytes:
        return signing_bytes(
            version=self._VECTOR_VERSION,
            tenant_id=self._VECTOR_TENANT,
            issued_at=self._VECTOR_ISSUED_AT,
            payload=self._vector_payload(),
        )

    def test_vector_is_deterministic(self) -> None:
        assert self._vector_bytes() == self._vector_bytes()

    def test_vector_expected_access_scope_shape(self) -> None:
        parsed = json.loads(self._vector_bytes())
        agent = parsed["payload"]["agents"][0]

        assert list(agent.keys()) == sorted(agent.keys())
        assert agent["access_scope"] == {
            "cerebro_unrestricted": False,
            "enforced": True,
            "native_tools": ["execute_code", "terminal"],
            "policy_overlay": {"send_message": {"enabled": False}},
            "views": ["calendar"],
        }


# ---------------------------------------------------------------------------
# LicenseSpec.remote_approval_enabled — Fase 2 Phase 4b (Enterprise remote
# approval tenant gate). Mirrors AgentSpec.access_scope's drop-when-default
# back-compat pattern exactly.
# ---------------------------------------------------------------------------


class TestLicenseSpecRemoteApprovalEnabledRoundTrip:
    def test_true_round_trips_through_signing_bytes(self) -> None:
        license_spec = LicenseSpec(plan="enterprise", remote_approval_enabled=True)
        payload = PolicyPayload(license=license_spec)

        b = signing_bytes(
            version=1, tenant_id="test-tenant", issued_at="2026-06-26T10:00:00Z", payload=payload
        )
        parsed_payload = PolicyPayload.model_validate(json.loads(b)["payload"])

        assert parsed_payload.license.remote_approval_enabled is True

    def test_default_false_parses_false(self) -> None:
        license_spec = LicenseSpec.model_validate({"plan": "starter"})
        assert license_spec.remote_approval_enabled is False

    def test_explicit_false_parses_false(self) -> None:
        license_spec = LicenseSpec.model_validate(
            {"plan": "starter", "remote_approval_enabled": False}
        )
        assert license_spec.remote_approval_enabled is False


class TestLicenseSpecRemoteApprovalEnabledBackCompat:
    """A bundle without remote_approval_enabled must parse + sign BYTE-
    IDENTICALLY to before this field existed — it must never appear (not even
    as false) in the serialized dict when it is unset/False."""

    def test_key_absent_from_dump_when_false(self) -> None:
        license_spec = LicenseSpec(plan="starter")
        payload = PolicyPayload(license=license_spec)
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-06-26T10:00:00Z", payload=payload)
        parsed_license = json.loads(b)["payload"]["license"]
        assert "remote_approval_enabled" not in parsed_license

    def test_key_present_and_true_when_enabled(self) -> None:
        license_spec = LicenseSpec(plan="enterprise", remote_approval_enabled=True)
        payload = PolicyPayload(license=license_spec)
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-06-26T10:00:00Z", payload=payload)
        parsed_license = json.loads(b)["payload"]["license"]
        assert parsed_license["remote_approval_enabled"] is True

    def test_signing_bytes_unaffected_by_the_new_field_existing(self) -> None:
        """Same license dict (no remote_approval_enabled key), signed before/
        after the field was added, must produce the exact same bytes."""
        payload = PolicyPayload.model_validate(
            {"license": {"plan": "starter", "max_agents": 3}}
        )
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-06-26T10:00:00Z", payload=payload)
        parsed_license = json.loads(b)["payload"]["license"]
        assert "remote_approval_enabled" not in parsed_license
        assert parsed_license["plan"] == "starter"
        assert parsed_license["max_agents"] == 3


# ---------------------------------------------------------------------------
# Fase 3 — department-scoped visibility (AccessScopeSpec.visibility_scope +
# PolicyPayload.directory). Wire-contract only here: consumption (roster
# filter / OfficeView / delegation-adapter) is a SEPARATE later task.
# ---------------------------------------------------------------------------


class TestVisibilityScopeRoundTrip:
    def test_default_is_all(self) -> None:
        assert AccessScopeSpec().visibility_scope == "all"

    def test_department_round_trips_through_signing_bytes(self) -> None:
        scope = AccessScopeSpec(visibility_scope="department")
        agent = AgentSpec(agent_id="a1", name="Agent", access_scope=scope)
        payload = PolicyPayload(agents=[agent])

        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-06T00:00:00Z", payload=payload)
        parsed_payload = PolicyPayload.model_validate(json.loads(b)["payload"])

        assert parsed_payload.agents[0].access_scope.visibility_scope == "department"

    def test_none_round_trips_through_signing_bytes(self) -> None:
        scope = AccessScopeSpec(visibility_scope="none")
        agent = AgentSpec(agent_id="a1", name="Agent", access_scope=scope)
        payload = PolicyPayload(agents=[agent])

        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-06T00:00:00Z", payload=payload)
        parsed_payload = PolicyPayload.model_validate(json.loads(b)["payload"])

        assert parsed_payload.agents[0].access_scope.visibility_scope == "none"


class TestVisibilityScopeBackCompat:
    """A scope with visibility_scope="all" (the default) must parse + sign
    BYTE-IDENTICALLY to before this field existed — it must never appear
    (not even as "all") in the serialized dict when unset/default."""

    def test_key_absent_from_dump_when_all(self) -> None:
        scope = AccessScopeSpec(enforced=True)
        agent = AgentSpec(agent_id="a1", name="Agent", access_scope=scope)
        payload = PolicyPayload(agents=[agent])
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-06T00:00:00Z", payload=payload)
        parsed_scope = json.loads(b)["payload"]["agents"][0]["access_scope"]
        assert "visibility_scope" not in parsed_scope

    def test_key_present_when_department(self) -> None:
        scope = AccessScopeSpec(visibility_scope="department")
        agent = AgentSpec(agent_id="a1", name="Agent", access_scope=scope)
        payload = PolicyPayload(agents=[agent])
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-06T00:00:00Z", payload=payload)
        parsed_scope = json.loads(b)["payload"]["agents"][0]["access_scope"]
        assert parsed_scope["visibility_scope"] == "department"

    def test_signing_bytes_unaffected_by_the_new_field_existing(self) -> None:
        """The committed TestAccessScopeSigningVector vector (no
        visibility_scope key at all) must sign to the exact same bytes now
        that the field exists — it never appears when "all"."""
        scope = AccessScopeSpec(
            enforced=True, cerebro_unrestricted=False,
            native_tools=["execute_code", "terminal"],
            policy_overlay={"send_message": {"enabled": False}},
            views=["calendar"],
        )
        agent = AgentSpec(agent_id="cloud-agent-1", name="Support", access_scope=scope)
        payload = PolicyPayload(agents=[agent])
        b = signing_bytes(
            version=1, tenant_id="test-tenant", issued_at="2026-06-26T10:00:00Z", payload=payload,
        )
        parsed = json.loads(b)
        assert parsed["payload"]["agents"][0]["access_scope"] == {
            "cerebro_unrestricted": False,
            "enforced": True,
            "native_tools": ["execute_code", "terminal"],
            "policy_overlay": {"send_message": {"enabled": False}},
            "views": ["calendar"],
        }


class TestIntegrationToolkitsRoundTrip:
    """Cerebro Enterprise Increment 0 (R-B): AccessScopeSpec.integration_toolkits
    — per-agent composio toolkit allow-list. Wire-contract only here:
    enforcement (_expand_composio / composio_skill_service) is a SEPARATE
    later task."""

    def test_default_is_empty_list(self) -> None:
        assert AccessScopeSpec().integration_toolkits == []

    def test_round_trips_through_signing_bytes(self) -> None:
        scope = AccessScopeSpec(integration_toolkits=["gmail", "googledrive"])
        agent = AgentSpec(agent_id="a1", name="Agent", access_scope=scope)
        payload = PolicyPayload(agents=[agent])

        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-07T00:00:00Z", payload=payload)
        parsed_payload = PolicyPayload.model_validate(json.loads(b)["payload"])

        assert parsed_payload.agents[0].access_scope.integration_toolkits == ["gmail", "googledrive"]

    def test_sorted_and_deduplicated(self) -> None:
        scope = AccessScopeSpec(integration_toolkits=["slack", "gmail", "slack", "googledrive"])
        assert scope.integration_toolkits == ["gmail", "googledrive", "slack"]

    def test_over_256_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AccessScopeSpec(integration_toolkits=[f"toolkit{i}" for i in range(257)])


class TestIntegrationToolkitsBackCompat:
    """A scope with integration_toolkits=[] (the default) must parse + sign
    BYTE-IDENTICALLY to before this field existed — it must never appear
    (not even as []) in the serialized dict when unset/empty."""

    def test_key_absent_from_dump_when_empty(self) -> None:
        scope = AccessScopeSpec(enforced=True)
        agent = AgentSpec(agent_id="a1", name="Agent", access_scope=scope)
        payload = PolicyPayload(agents=[agent])
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-07T00:00:00Z", payload=payload)
        parsed_scope = json.loads(b)["payload"]["agents"][0]["access_scope"]
        assert "integration_toolkits" not in parsed_scope

    def test_key_present_when_non_empty(self) -> None:
        scope = AccessScopeSpec(integration_toolkits=["gmail"])
        agent = AgentSpec(agent_id="a1", name="Agent", access_scope=scope)
        payload = PolicyPayload(agents=[agent])
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-07T00:00:00Z", payload=payload)
        parsed_scope = json.loads(b)["payload"]["agents"][0]["access_scope"]
        assert parsed_scope["integration_toolkits"] == ["gmail"]

    def test_signing_bytes_unaffected_by_the_new_field_existing(self) -> None:
        """The committed TestAccessScopeSigningVector vector (no
        integration_toolkits key at all) must sign to the exact same bytes
        now that the field exists — it never appears when empty."""
        scope = AccessScopeSpec(
            enforced=True, cerebro_unrestricted=False,
            native_tools=["execute_code", "terminal"],
            policy_overlay={"send_message": {"enabled": False}},
            views=["calendar"],
        )
        agent = AgentSpec(agent_id="cloud-agent-1", name="Support", access_scope=scope)
        payload = PolicyPayload(agents=[agent])
        b = signing_bytes(
            version=1, tenant_id="test-tenant", issued_at="2026-06-26T10:00:00Z", payload=payload,
        )
        parsed = json.loads(b)
        assert parsed["payload"]["agents"][0]["access_scope"] == {
            "cerebro_unrestricted": False,
            "enforced": True,
            "native_tools": ["execute_code", "terminal"],
            "policy_overlay": {"send_message": {"enabled": False}},
            "views": ["calendar"],
        }


class TestDirectorySpecRoundTrip:
    def test_entries_round_trip_through_signing_bytes(self) -> None:
        from hermes.config_sync.policy_document import DirectoryEntrySpec, DirectorySpec

        directory = DirectorySpec(entries=[
            DirectoryEntrySpec(employee_id="e2", agent_id="a2", name="Ana", department="Ventas"),
        ])
        payload = PolicyPayload(directory=directory)

        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-06T00:00:00Z", payload=payload)
        parsed_payload = PolicyPayload.model_validate(json.loads(b)["payload"])

        assert parsed_payload.directory == directory

    def test_explicit_empty_entries_still_serializes(self) -> None:
        """visibility_scope="none" -> DirectorySpec(entries=[]) must still
        appear on the wire (distinct from no directory pushed at all) —
        only `directory is None` is dropped."""
        from hermes.config_sync.policy_document import DirectorySpec

        payload = PolicyPayload(directory=DirectorySpec(entries=[]))
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-06T00:00:00Z", payload=payload)
        parsed = json.loads(b)["payload"]
        assert parsed["directory"] == {"entries": []}


class TestDirectorySpecBackCompat:
    """A payload with directory=None (the default) must parse + sign BYTE-
    IDENTICALLY to before this field existed — the key must be ABSENT, never
    "directory":null."""

    def test_key_absent_from_dump_when_none(self) -> None:
        payload = PolicyPayload()
        b = signing_bytes(version=1, tenant_id="t", issued_at="2026-07-06T00:00:00Z", payload=payload)
        parsed_payload = json.loads(b)["payload"]
        assert "directory" not in parsed_payload

    def test_signing_bytes_unaffected_by_the_new_field_existing(self) -> None:
        """The committed TestSigningBytes vectors (built before `directory`
        existed) must sign to the exact same bytes now that the field
        exists on PolicyPayload."""
        payload = PolicyPayload.model_validate(_minimal_payload_dict())
        b = signing_bytes(
            version=1, tenant_id="tenant-abc", issued_at="2026-06-26T10:00:00Z", payload=payload,
        )
        parsed_payload = json.loads(b)["payload"]
        assert "directory" not in parsed_payload
