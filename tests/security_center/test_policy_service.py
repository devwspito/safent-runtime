"""Unit tests for PolicyService."""

from __future__ import annotations

import pytest

from hermes.security_center.application.policy_service import PolicyService, PolicyValidationError
from hermes.security_center.domain.policy import SecurityPolicy

pytestmark = pytest.mark.unit


class _InMemoryPolicyRepo:
    def __init__(self) -> None:
        self._policy = SecurityPolicy.default()

    def load(self) -> SecurityPolicy:
        return self._policy

    def save(self, policy: SecurityPolicy) -> None:
        self._policy = policy


def _make_service() -> PolicyService:
    return PolicyService(policy_repo=_InMemoryPolicyRepo())


# ---------------------------------------------------------------------------
# get_policy
# ---------------------------------------------------------------------------

def test_get_policy_returns_default():
    svc = _make_service()
    p = svc.get_policy()
    assert p.auto_block_fail is True
    assert p.require_approval_warn is True
    assert p.scanner_weights["cve"] == 35


# ---------------------------------------------------------------------------
# set_policy — valid patches
# ---------------------------------------------------------------------------

def test_set_policy_flips_auto_block():
    svc = _make_service()
    updated = svc.set_policy('{"auto_block_fail": false}', operator_uid=1000)
    assert updated.auto_block_fail is False
    assert svc.get_policy().auto_block_fail is False


def test_set_policy_updates_weights():
    svc = _make_service()
    weights = '{"scanner_weights": {"cve": 40, "mcp_lint": 25, "provenance": 20, "signature": 15}}'
    updated = svc.set_policy(weights, operator_uid=1000)
    assert updated.scanner_weights["cve"] == 40


def test_set_policy_updates_trusted_orgs():
    svc = _make_service()
    updated = svc.set_policy('{"trusted_orgs": ["example.com"]}', operator_uid=1000)
    assert "example.com" in updated.trusted_orgs


def test_set_policy_preserves_unpatched_fields():
    svc = _make_service()
    svc.set_policy('{"auto_block_fail": false}', operator_uid=1000)
    p = svc.get_policy()
    assert p.require_approval_warn is True  # unchanged default


# ---------------------------------------------------------------------------
# set_policy — invalid input
# ---------------------------------------------------------------------------

def test_set_policy_invalid_json_raises():
    svc = _make_service()
    with pytest.raises(PolicyValidationError):
        svc.set_policy("not-json", operator_uid=1000)


def test_set_policy_non_object_raises():
    svc = _make_service()
    with pytest.raises(PolicyValidationError):
        svc.set_policy("[1, 2, 3]", operator_uid=1000)


def test_set_policy_weights_not_sum_100_raises():
    svc = _make_service()
    bad = '{"scanner_weights": {"cve": 10, "mcp_lint": 10, "provenance": 10, "signature": 10}}'
    with pytest.raises(PolicyValidationError):
        svc.set_policy(bad, operator_uid=1000)


def test_set_policy_weights_not_dict_raises():
    svc = _make_service()
    with pytest.raises(PolicyValidationError):
        svc.set_policy('{"scanner_weights": "invalid"}', operator_uid=1000)


def test_set_policy_trusted_orgs_not_list_raises():
    svc = _make_service()
    with pytest.raises(PolicyValidationError):
        svc.set_policy('{"trusted_orgs": "invalid"}', operator_uid=1000)
