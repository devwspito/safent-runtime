"""Disposable image: real engine factory + native SDK + real Enterprise admission.

Mount runtime at /review and Enterprise source at /review/enterprise-src. Uses
the existing HTTPS clean-bootstrap fixture. Only the constant production gate
is substituted in the worker, never the factory/resolver/SDK/admission service.
No live gateway or service is started; the upstream response is a loopback stub.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import managed_lifecycle_bootstrap_smoke as fixture

sys.path.insert(0, "/review/enterprise-src")
_SCOPED = "fictional-scoped-inference"
_UPSTREAM = "fictional-company-key-not-for-community"
_STATE = None
_CHECKS = set()


def factory(binding, _native, _model):
    from hermes.prompts.persona import PersonaSpec
    from hermes.runtime import nous_engine
    from hermes.runtime.model_config import ManagedProviderUnavailableError

    engine = nous_engine.NousReasoningEngine(
        persona=PersonaSpec(
            name="Fixture", role="assistant", language="en", register="", primary_mission="Reply OK"
        ),
        enabled_toolsets=[],
    )
    loop = asyncio.new_event_loop()
    config = replace(binding, max_iterations=1, max_tokens=16)
    try:
        try:
            engine._build_governed_agent(config, "Reply OK.", loop, UUID(int=1))
        except ManagedProviderUnavailableError:
            pass
        else:
            raise AssertionError("Production managed execution gate unexpectedly opened")
        # Explicit, fixture-process-only replacement; cannot be supplied through
        # a profile/tool/env flag. All authority/bootstrap checks stay real.
        nous_engine._assert_managed_execution_ready = lambda: None
        return engine._build_governed_agent(config, "Reply OK.", loop, UUID(int=1))
    finally:
        loop.close()


def enterprise_state():
    global _STATE  # noqa: PLW0603 - isolated fixture owns one temporary server state
    if _STATE is not None:
        return _STATE
    from safent_control.application.inference_service import InferenceService
    from safent_control.domain.entities import (
        AgentTemplate,
        Employee,
        Instance,
        InstanceState,
        License,
        LicenseState,
        ProviderConfig,
        Tenant,
    )
    from safent_control.domain.inference import InferenceGrant
    from safent_control.infrastructure.provider_key_crypto import encrypt_api_key
    from safent_control.infrastructure.repository import ControlPlaneRepository, hash_secret

    root = tempfile.TemporaryDirectory(prefix="enterprise-factory-contract-")
    repo = ControlPlaneRepository(db_path=Path(root.name) / "enterprise.sqlite")
    repo.save_tenant(Tenant("org", "Fixture", 3))
    repo.save_employee(Employee("employee", "org", "fixture@example.invalid", "Fixture"))
    repo.save_agent_template(
        AgentTemplate(
            "template",
            "employee",
            "org",
            "Fixture",
            provider_alias="company",
            providers=[{"alias": "company"}],
        )
    )
    repo.save_provider(
        ProviderConfig(
            "provider",
            "org",
            "company",
            "openai",
            "https://api.openai.com/v1",
            "company",
            encrypt_api_key(_UPSTREAM),
        )
    )
    now = datetime.now(UTC)
    for case in ("success", "revoke"):
        instance_id = "instance-" + case
        repo.save_instance(
            Instance(instance_id, "org", "template", case, state=InstanceState.ACTIVE),
            instance_secret_hash=hash_secret("pairing-only"),
        )
        repo.save_license(
            License(
                "license-" + case,
                "org",
                state=LicenseState.ASSIGNED,
                assigned_instance_id=instance_id,
            )
        )
        repo.save_inference_grant(
            InferenceGrant(
                case,
                "org",
                instance_id,
                "provider",
                "company",
                hash_secret(_SCOPED),
                encrypt_api_key(_SCOPED),
                (now + timedelta(minutes=10)).isoformat(),
                now.isoformat(),
            )
        )
    _STATE = root, repo, InferenceService(repo)
    return _STATE


def validate(path, authorization, body):
    from safent_control.domain.inference import InferenceError

    _, repo, service = enterprise_state()
    grant_id = path.split("/v1/inference/", 1)[1].split("/", 1)[0]
    assert authorization == "Bearer " + _SCOPED
    assert _UPSTREAM not in str(body) and "chat_template_kwargs" not in body
    assert "extra_body" not in body and body["model"] == "company"
    prepared = service.prepare(grant_id, authorization, body)
    assert prepared[2].get("max_tokens") == 16
    repo.settle_inference_request(prepared[3], "org", usage_json="{}", uncertain=False)
    _CHECKS.add("real-factory-payload")

    def denied(auth, candidate, expected):
        try:
            service.prepare(grant_id, auth, candidate)
        except InferenceError as exc:
            assert exc.code == expected, exc.code
        else:
            raise AssertionError("Enterprise accepted a forbidden factory-payload variation")
        _CHECKS.add(expected)

    denied(authorization, {**body, "max_tokens": 4097}, "inference_token_limit_invalid")
    denied(authorization, {**body, "max_tokens": True}, "inference_token_limit_invalid")
    denied(authorization, {**body, "model": "personal"}, "inference_model_forbidden")
    denied("Bearer pairing-only", body, "inference_credential_invalid")
    denied(
        authorization,
        {**body, "chat_template_kwargs": {"enable_thinking": False}},
        "inference_request_unsupported",
    )
    grant = prepared[0]
    repo.save_inference_grant(replace(grant, revoked=1))
    denied(authorization, body, "inference_credential_invalid")
    repo.save_inference_grant(grant)

    # No explicit native cap: official OpenAI gets the centrally capped modern
    # field; runtime must not inject a local Qwen or unbounded default.
    default_body = {k: v for k, v in body.items() if k != "max_tokens"}
    default = service.prepare(grant_id, authorization, default_body)
    assert default[2]["max_completion_tokens"] == 4096
    repo.settle_inference_request(default[3], "org", usage_json="{}", uncertain=False)
    _CHECKS.add("central-default-token-cap")


if __name__ == "__main__":
    fixture.__file__ = __file__  # fixture exec launches this same diagnostic entrypoint
    fixture.construct_fixture_agent = factory
    fixture.validate_gateway_fixture = validate
    if "--worker" in sys.argv:
        fixture.worker()
    else:
        try:
            fixture.main()
            assert len(_CHECKS) == 6, _CHECKS
            print("FACTORY_GATEWAY_PASS:" + ",".join(sorted(_CHECKS)))
        finally:
            if _STATE is not None:
                _STATE[0].cleanup()
