"""ReplayScript domain value object: programa determinista firmado.

Implementación del contrato definido en
specs/001-stack-browser-brutal/contracts/replay_script.py.

Constitución II: replay no elude HITL para HIGH.
Constitución IV: firma inválida → fail-closed → discovery.

La canonicalización usa json.dumps(sort_keys=True, separators=(',', ':'),
ensure_ascii=False) sobre un dict que excluye signature_hex y created_at.
Esto garantiza bytes deterministas independientemente del orden en que los
campos fueron construidos.

`signature_hex` format: "{version}:{hex_hmac}", e.g. "v1:abcdef123...".
Si no cumple el formato, verify() levanta ReplayScriptInvalidSignature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ReplayScriptError(RuntimeError):
    """Base de errores de replay."""


class ReplayScriptInvalidSignature(ReplayScriptError):
    """HMAC no valida — script tampered, key incorrecta o formato inválido."""


class ReplayScriptDowngradeRejected(ReplayScriptError):
    """Versión de firma menor que min_accepted_version — downgrade attack."""


class ReplayInvalidationReason(StrEnum):
    """Razón por la que un ReplayScript fue invalidado."""

    SELECTOR_DEPRECATED = "selector_deprecated"
    SELECTOR_NOT_RESOLVED = "selector_not_resolved"
    SITE_CHANGED = "site_changed"
    SIGNATURE_INVALID = "signature_invalid"
    MANUAL = "manual"


_VERSION_RE = re.compile(r"^v(\d+):.+$")


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """Step determinista: selector_id identifica el localizador firmado."""

    selector_id: str
    selector_version: int
    action: str  # "click" | "fill" | "navigate" | "extract" | "select"
    payload_template: dict[str, Any]  # placeholders [[NIF_1]] etc.
    risk: str  # "low" | "medium" | "high"


@dataclass(frozen=True, slots=True)
class ReplayScript:
    """Programa replay firmado. Identidad = script_id.

    signature_hex cubre el resto del documento canonicalizado.
    Verificación constant-time (hmac.compare_digest).
    """

    script_id: UUID
    site_id: str
    flow_id: str
    tenant_scope: UUID | None
    runtime_version: str
    steps: tuple[ReplayStep, ...]
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    invalidated_at: datetime | None = None
    invalidation_reason: ReplayInvalidationReason | None = None
    signature_hex: str = ""

    @property
    def is_active(self) -> bool:
        return self.invalidated_at is None

    def canonical_bytes_for_signing(self) -> bytes:
        """Serialización determinista para HMAC.

        Excluye signature_hex y created_at del payload firmado.
        sort_keys=True garantiza orden estable en dicts anidados.
        ensure_ascii=False preserva Unicode sin escaping.
        """
        payload: dict[str, Any] = {
            "script_id": str(self.script_id),
            "site_id": self.site_id,
            "flow_id": self.flow_id,
            "tenant_scope": str(self.tenant_scope) if self.tenant_scope else None,
            "runtime_version": self.runtime_version,
            "steps": [
                {
                    "selector_id": step.selector_id,
                    "selector_version": step.selector_version,
                    "action": step.action,
                    "payload_template": step.payload_template,
                    "risk": step.risk,
                }
                for step in self.steps
            ],
        }
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def verify(self, *, key: bytes, min_accepted_version: str = "v1") -> None:
        """Verifica la firma HMAC del script.

        Raises:
            ReplayScriptInvalidSignature: si la firma no valida, el formato
                es incorrecto o la key es incorrecta.
            ReplayScriptDowngradeRejected: si la versión de firma es menor
                que min_accepted_version (downgrade protection, threat-model S1
                superficie 3).
        """
        if not self.signature_hex:
            raise ReplayScriptInvalidSignature(
                "signature_hex está vacío — script sin firmar o tampered"
            )

        match = _VERSION_RE.match(self.signature_hex)
        if not match:
            raise ReplayScriptInvalidSignature(
                f"signature_hex no cumple el formato 'vN:<hex>': {self.signature_hex!r}"
            )

        sig_version_n = int(match.group(1))
        min_match = _VERSION_RE.match(f"{min_accepted_version}:x")
        if min_match is None:
            raise ValueError(
                f"min_accepted_version no cumple el formato 'vN': {min_accepted_version!r}"
            )
        min_version_n = int(min_match.group(1))

        if sig_version_n < min_version_n:
            raise ReplayScriptDowngradeRejected(
                f"Firma con versión v{sig_version_n} rechazada: "
                f"min_accepted_version={min_accepted_version}"
            )

        _, hex_part = self.signature_hex.split(":", 1)
        version_prefix = f"v{sig_version_n}"
        expected_hex = _compute_hmac(self.canonical_bytes_for_signing(), key=key)
        expected_with_version = f"{version_prefix}:{expected_hex}"

        if not hmac.compare_digest(self.signature_hex, expected_with_version):
            raise ReplayScriptInvalidSignature(
                f"Firma HMAC inválida para script {self.script_id}"
            )


def sign_replay_script(script: ReplayScript, *, key: bytes, version: str = "v1") -> ReplayScript:
    """Devuelve una copia del script con signature_hex rellenado."""
    hex_mac = _compute_hmac(script.canonical_bytes_for_signing(), key=key)
    return ReplayScript(
        script_id=script.script_id,
        site_id=script.site_id,
        flow_id=script.flow_id,
        tenant_scope=script.tenant_scope,
        runtime_version=script.runtime_version,
        steps=script.steps,
        created_at=script.created_at,
        invalidated_at=script.invalidated_at,
        invalidation_reason=script.invalidation_reason,
        signature_hex=f"{version}:{hex_mac}",
    )


def _compute_hmac(data: bytes, *, key: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()
