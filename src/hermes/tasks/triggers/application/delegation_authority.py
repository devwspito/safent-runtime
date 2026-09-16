"""Revalidate a stored signed request against the current local pairing.

The proof never comes from an approval form. No legacy row is retroactively
trusted. The final guard shares the pairing writers' lock and must surround a
synchronous queue commit only, never inference, networking or an await.
"""

from contextlib import contextmanager

from hermes.config_sync.delegation_inbox import (
    _extract_envelope,
    _freshness_ok,
    delegation_signing_bytes,
)
from hermes.config_sync.signature import verify_bundle
from hermes.security.configuration_lock import configuration_lock


class DelegationAuthorityError(PermissionError):
    """The original request no longer proves permission for local admission."""


class DelegationAdmissionAuthority:
    def __init__(self, *, association_store, pending_repo):
        self._store = association_store
        self._pending = pending_repo

    def capture(self, envelope):
        store = self._store
        assoc = store.get() if store is not None else None
        if assoc is None or assoc.state != "active":
            raise DelegationAuthorityError("not_associated")
        signature = envelope.get("signature_hex")
        if not isinstance(signature, str) or not signature:
            raise DelegationAuthorityError("missing_signature")
        plain = _extract_envelope(envelope)
        if plain is None or set(envelope) != set(plain) | {"signature_hex"}:
            raise DelegationAuthorityError("invalid_envelope_shape")
        if plain["kind"] != "request" or plain["to_instance_id"] != assoc.instance_id:
            raise DelegationAuthorityError("wrong_destination")
        if not _freshness_ok(plain["issued_at"]):
            raise DelegationAuthorityError("stale_request")
        if not verify_bundle(
            payload_canonical=delegation_signing_bytes(plain),
            signature_hex=signature,
            pubkey_hex=assoc.signing_pubkey_hex,
        ):
            raise DelegationAuthorityError("bad_signature")
        return {
            "version": 1,
            "pairing": {
                key: getattr(assoc, key)
                for key in (
                    "instance_id",
                    "tenant_id",
                    "paired_at",
                    "cloud_endpoint",
                    "signing_pubkey_hex",
                )
            },
            "envelope": {**plain, "signature_hex": signature},
        }

    def validate(self, message_id):
        proof = self._pending.admission_proof(message_id=message_id)
        if proof is None or proof.get("version") != 1:
            raise DelegationAuthorityError("request_unverified")
        envelope = proof.get("envelope")
        if not isinstance(envelope, dict) or self.capture(envelope) != proof:
            raise DelegationAuthorityError("pairing_changed")
        row = self._pending.fetch(message_id=message_id)
        if (
            row is None
            or row.status != "pending"
            or any(
                getattr(row, key) != envelope[key]
                for key in (
                    "message_id",
                    "correlation_id",
                    "from_employee_id",
                    "from_agent_id",
                    "from_instance_id",
                    "to_employee_id",
                    "to_agent_id",
                    "to_instance_id",
                    "body",
                    "issued_at",
                )
            )
        ):
            raise DelegationAuthorityError("request_changed")

    @contextmanager
    def guard(self, message_id):
        if self._store is None:
            raise DelegationAuthorityError("not_associated")
        with configuration_lock(self._store.db_path):
            self.validate(message_id)
            yield
