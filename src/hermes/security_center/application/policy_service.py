"""PolicyService — read and update the SecurityPolicy."""

from __future__ import annotations

import json
import logging

from hermes.security_center.application.ports import IPolicyRepo
from hermes.security_center.domain.policy import SecurityPolicy

logger = logging.getLogger("hermes.security_center.policy_service")


class PolicyValidationError(ValueError):
    """Raised when a policy update payload is invalid."""


class PolicyService:
    """Read and mutate the daemon's SecurityPolicy.

    Mutations require an authorized operator_uid (caller verifies this at the
    D-Bus layer before invoking set_policy).
    """

    def __init__(self, *, policy_repo: IPolicyRepo) -> None:
        self._repo = policy_repo

    def get_policy(self) -> SecurityPolicy:
        return self._repo.load()

    def set_policy(self, policy_json: str, *, operator_uid: int) -> SecurityPolicy:
        """Parse, validate, and persist a new policy.

        Raises PolicyValidationError on invalid input.
        """
        try:
            raw = json.loads(policy_json)
        except json.JSONDecodeError as exc:
            raise PolicyValidationError(f"policy_json inválido: {exc}") from exc

        if not isinstance(raw, dict):
            raise PolicyValidationError("policy_json debe ser un objeto JSON")

        current = self._repo.load()
        updated = self._apply_patch(current, raw)
        self._repo.save(updated)
        logger.info(
            "hermes.security.policy_updated",
            extra={"by_uid": operator_uid},
        )
        return updated

    @staticmethod
    def _apply_patch(current: SecurityPolicy, patch: dict) -> SecurityPolicy:
        auto_block_fail = bool(patch.get("auto_block_fail", current.auto_block_fail))
        require_approval_warn = bool(
            patch.get("require_approval_warn", current.require_approval_warn)
        )
        weights_raw = patch.get("scanner_weights")
        if weights_raw is not None:
            if not isinstance(weights_raw, dict):
                raise PolicyValidationError("scanner_weights debe ser un objeto JSON")
            weights = {str(k): int(v) for k, v in weights_raw.items()}
        else:
            weights = dict(current.scanner_weights)

        trusted_raw = patch.get("trusted_orgs")
        if trusted_raw is not None:
            if not isinstance(trusted_raw, list):
                raise PolicyValidationError("trusted_orgs debe ser una lista JSON")
            trusted = frozenset(str(x) for x in trusted_raw)
        else:
            trusted = current.trusted_orgs

        try:
            return SecurityPolicy(
                auto_block_fail=auto_block_fail,
                require_approval_warn=require_approval_warn,
                scanner_weights=weights,
                trusted_orgs=trusted,
            )
        except ValueError as exc:
            raise PolicyValidationError(str(exc)) from exc
