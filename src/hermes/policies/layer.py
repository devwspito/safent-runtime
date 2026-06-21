"""PolicyLayer: validacion de ToolCallProposals antes de la HITL queue del consumidor.

Se ejecuta DESPUES de que el LLM proponga via tool_call y ANTES de devolver la
propuesta al consumer.

Razones:
  1. El LLM no es de confianza para tomar decisiones por si solo: sus args pueden
     venir de prompt injection en untrusted content.
  2. La HITL queue del consumer presentara la propuesta al operador humano.
     Mejor descartar lo absurdo aqui (evitamos ruido + ataques de social
     engineering sobre el gestor).
  3. Audit: las rejected_by_policy quedan en `CycleOutput` para forense.

Reglas standard:
  - tenant_scope_enforced:    el `entity_id` debe ser del mismo tenant que el contexto.
                              (El consumer suele inyectar un callable que valida en DB.)
  - importe_bounds:           si la tool tiene un parametro 'importe' / 'amount' /
                              'monto', verificar que entra en bounds per-tool.
  - url_allowlist:            si la tool tiene 'url' / 'target_url', verificar
                              contra allowlist por dominio (relevante en browser-tools).
  - placeholder_consistency:  si la propuesta menciona placeholders PII, todos
                              deben estar en el mapping (no inventados).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse

from hermes.domain.proposal import ToolCallProposal


class VerdictKind(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class PolicyVerdict:  # noqa: PLW1641
    kind: VerdictKind
    reason: str = ""
    policy_name: str = ""

    @classmethod
    def accept(cls) -> PolicyVerdict:
        return cls(kind=VerdictKind.ACCEPT)

    @classmethod
    def reject(cls, *, reason: str, policy_name: str) -> PolicyVerdict:
        return cls(kind=VerdictKind.REJECT, reason=reason, policy_name=policy_name)


class PolicyLayer(Protocol):
    """Interfaz para policy layers."""

    def evaluate(self, proposal: ToolCallProposal) -> PolicyVerdict: ...


TenantValidator = Callable[[str, str, str], bool]
"""Firma del validador tenant: (tenant_id_str, entity_type, entity_id) -> belongs?"""


@dataclass(frozen=True, slots=True)
class ImporteBound:
    """Limites para un campo de importe en una tool concreta."""

    tool_name: str
    field_name: str
    min_value: float = 0.0
    max_value: float = 1_000_000.0
    currency_field: str | None = None


@dataclass(frozen=True, slots=True)
class UrlAllowlistRule:
    """Allowlist de hosts para tools que invocan URLs (browser, fetch)."""

    tool_name: str
    field_name: str
    allowed_hosts: tuple[str, ...]


class DefaultPolicyLayer:
    """PolicyLayer con reglas standard + extensibles por la vertical.

    La vertical pasa:
      - tenant_validator: callable que dice si (tenant_id, entity_type, entity_id)
                          pertenece al tenant. Si None, se omite la regla (no recomendado).
      - importe_bounds:   tupla de ImporteBound por tool.
      - url_allowlist:    tupla de UrlAllowlistRule por tool.
      - placeholder_mapping: mapping del tokenizer activo (para detectar invenciones).
    """

    def __init__(
        self,
        *,
        tenant_validator: TenantValidator | None = None,
        importe_bounds: Sequence[ImporteBound] = (),
        url_allowlist: Sequence[UrlAllowlistRule] = (),
        placeholder_mapping: dict[str, str] | None = None,
    ) -> None:
        self._tenant_validator = tenant_validator
        self._importe_bounds: dict[str, list[ImporteBound]] = {}
        for bound in importe_bounds:
            self._importe_bounds.setdefault(bound.tool_name, []).append(bound)
        self._url_allowlist: dict[str, list[UrlAllowlistRule]] = {}
        for rule in url_allowlist:
            self._url_allowlist.setdefault(rule.tool_name, []).append(rule)
        self._mapping = placeholder_mapping or {}

    def evaluate(self, proposal: ToolCallProposal) -> PolicyVerdict:
        for check in (
            self._check_tenant_scope,
            self._check_importe_bounds,
            self._check_url_allowlist,
            self._check_placeholders,
        ):
            verdict = check(proposal)
            if verdict.kind == VerdictKind.REJECT:
                return verdict
        return PolicyVerdict.accept()

    # ------------------------------------------------------------------
    # checks individuales
    # ------------------------------------------------------------------

    def _check_tenant_scope(self, proposal: ToolCallProposal) -> PolicyVerdict:
        if self._tenant_validator is None:
            return PolicyVerdict.accept()
        belongs = self._tenant_validator(
            str(proposal.tenant_id), proposal.entity_type, proposal.entity_id
        )
        if not belongs:
            return PolicyVerdict.reject(
                reason=(
                    f"entity {proposal.entity_type}/{proposal.entity_id} no pertenece "
                    f"al tenant {proposal.tenant_id}"
                ),
                policy_name="tenant_scope",
            )
        return PolicyVerdict.accept()

    def _check_importe_bounds(self, proposal: ToolCallProposal) -> PolicyVerdict:
        rules = self._importe_bounds.get(proposal.tool_name, [])
        for rule in rules:
            value = proposal.parameters.get(rule.field_name)
            if value is None:
                continue
            try:
                amount = float(value)
            except (TypeError, ValueError):
                return PolicyVerdict.reject(
                    reason=(
                        f"campo {rule.field_name!r} de {proposal.tool_name} no es numerico"
                    ),
                    policy_name="importe_bounds",
                )
            if amount < rule.min_value or amount > rule.max_value:
                return PolicyVerdict.reject(
                    reason=(
                        f"{rule.field_name}={amount} fuera de [{rule.min_value}, "
                        f"{rule.max_value}] para {proposal.tool_name}"
                    ),
                    policy_name="importe_bounds",
                )
        return PolicyVerdict.accept()

    def _check_url_allowlist(self, proposal: ToolCallProposal) -> PolicyVerdict:
        rules = self._url_allowlist.get(proposal.tool_name, [])
        for rule in rules:
            url = proposal.parameters.get(rule.field_name)
            if not isinstance(url, str) or not url:
                continue
            host = urlparse(url).hostname or ""
            if not _host_matches_allowlist(host, rule.allowed_hosts):
                return PolicyVerdict.reject(
                    reason=f"host {host!r} no esta en allowlist de {proposal.tool_name}",
                    policy_name="url_allowlist",
                )
        return PolicyVerdict.accept()

    def _check_placeholders(self, proposal: ToolCallProposal) -> PolicyVerdict:
        if not self._mapping:
            return PolicyVerdict.accept()
        seen: set[str] = set()
        _collect_placeholders(proposal.parameters, seen)
        _collect_placeholders(proposal.justification, seen)
        for placeholder in seen:
            if placeholder not in self._mapping:
                return PolicyVerdict.reject(
                    reason=(
                        f"placeholder {placeholder!r} no esta en el mapping del tokenizer; "
                        "posible alucinacion o injection"
                    ),
                    policy_name="placeholder_consistency",
                )
        return PolicyVerdict.accept()


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _host_matches_allowlist(host: str, allowed: tuple[str, ...]) -> bool:
    host_lower = host.lower()
    for entry in allowed:
        entry_lower = entry.lower().lstrip(".")
        if host_lower == entry_lower or host_lower.endswith("." + entry_lower):
            return True
    return False


_PLACEHOLDER_TOKEN = re.compile(r"\[\[[A-Z_]+_[0-9]+\]\]")


def _collect_placeholders(value: object, out: set[str]) -> None:
    if isinstance(value, str):
        for match in _PLACEHOLDER_TOKEN.finditer(value):
            out.add(match.group(0))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_placeholders(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_placeholders(v, out)
