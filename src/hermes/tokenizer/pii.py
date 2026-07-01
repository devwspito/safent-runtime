"""PIITokenizer: enmascara PII antes de salir al LLM provider externo.

Defensas que aporta:
  - RGPD: minimiza PII enviada a sub-procesador (Anthropic/OpenAI/Azure/Gemini).
  - Anti prompt injection: un NIF "12345678A" en el contexto se convierte en
    `[[NIF_1]]`; si el LLM "imagina" un NIF distinto al rehidratar fallara.
    Usamos `[[...]]` (no `<...>`) para no colisionar con el escape HTML que
    aplica `DefaultPromptBuilder` sobre el untrusted content.
  - Audit: el mapping {placeholder -> valor real} queda solo en process memory
    para esta llamada. Nunca se persiste.

Cada vertical puede:
  - Usar `DefaultPIITokenizer` (cubre NIF/CIF espanoles, IBAN, email, telefono ES).
  - Pasar `extra_patterns` para anadir su propio tipo PII.
  - Sustituirlo entero implementando el Protocol PIITokenizer.

Diseno:
  1. `tokenize(payload)`   -> (`payload_safe`, mapping).
  2. (LLM responde con texto que puede contener placeholders).
  3. `rehydrate(text, mapping)` -> texto con valores reales reinsertados.
  4. Si el LLM "inventa" un placeholder no presente en mapping -> error
     `UnknownPlaceholderError` (fail-closed).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


def actionable_pii_exclusions() -> frozenset[str]:
    """Default pattern names NOT to tokenize — ACTIONABLE identifiers the user
    hands the agent to act on (recipient email / phone). Tokenizing them fights
    the agent's ability to use them (a weak model may not carry the placeholder
    into tool args and would message the wrong target). Financial/ID PII stays
    tokenized. Override via HERMES_PII_UNTOKENIZED (comma-separated names).
    """
    return frozenset(
        p.strip().upper()
        for p in os.environ.get("HERMES_PII_UNTOKENIZED", "EMAIL,TEL").split(",")
        if p.strip()
    )


class UnknownPlaceholderError(ValueError):
    """El LLM emitio un placeholder que no esta en el mapping."""


@dataclass(frozen=True, slots=True)
class TokenizedPayload:
    """Resultado de tokenizar un payload.

    Atributos:
        sanitized:   estructura igual al input pero con valores PII enmascarados.
        mapping:     placeholder -> valor original. NO persistir.
        replaced:    numero de reemplazos efectuados (metrics).
    """

    sanitized: Any
    mapping: dict[str, str] = field(default_factory=dict)
    replaced: int = 0


class PIITokenizer(Protocol):
    """Interfaz para tokenizers de PII."""

    def tokenize(self, payload: Any) -> TokenizedPayload: ...

    def rehydrate(self, text: str, mapping: dict[str, str]) -> str: ...


@dataclass(frozen=True, slots=True)
class _Pattern:
    name: str
    regex: re.Pattern[str]


class DefaultPIITokenizer:
    """Tokenizer con patrones default ES + extensible per-vertical.

    Defaults cubren:
      - NIF / NIE espanoles (incluye control DNI).
      - CIF empresarial espanol.
      - IBAN (ES, internacional 15-34 alfanumerico tras codigo).
      - email (RFC 5321 simplificado).
      - telefonos ES (+34 / 6xx / 9xx).

    Para anadir patrones (usar raw strings o character classes):
        DefaultPIITokenizer(
            extra_patterns=(
                ("MASCOTA_ID", re.compile(r"MAS-[0-9]{6}")),
                ("LICENCIA_VET", re.compile(r"VET-[A-Z]{2}[0-9]{4}")),
            ),
        )

    Para SUSTITUIR patrones default, pasar `default_patterns_enabled=False`.
    """

    # NIF/NIE espanol: 8 digitos + letra control. NIE empieza por X/Y/Z + 7 digitos + letra.
    _NIF_RE = re.compile(r"\b\d{8}[A-HJ-NP-TV-Z]\b", re.IGNORECASE)
    _NIE_RE = re.compile(r"\b[XYZ]\d{7}[A-HJ-NP-TV-Z]\b", re.IGNORECASE)
    # CIF: letra empresa + 7 digitos + digito/letra control.
    _CIF_RE = re.compile(r"\b[ABCDEFGHJKLMNPQRSUVW]\d{7}[0-9A-J]\b", re.IGNORECASE)
    # IBAN: 2 letras pais + 2 digitos + 11-30 alfanumericos.
    _IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
    # Email: simplificado.
    _EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    # Telefono ES: +34 6xx xxx xxx / 9xx xxx xxx / 7xx xxx xxx.
    _PHONE_ES_RE = re.compile(
        r"(?:\+34[\s.-]?)?(?:[679]\d{2})[\s.-]?\d{3}[\s.-]?\d{3}\b"
    )

    def __init__(
        self,
        *,
        extra_patterns: tuple[tuple[str, re.Pattern[str]], ...] = (),
        default_patterns_enabled: bool = True,
        exclude_patterns: frozenset[str] = frozenset(),
    ) -> None:
        """PII tokenizer.

        exclude_patterns: names of default patterns to SKIP. Use for ACTIONABLE
        identifiers the user explicitly hands the agent to act on (e.g.
        {"EMAIL", "TEL"}): tokenizing a recipient the user provided fights the
        agent's ability to use it — a weak model may not carry the placeholder
        faithfully into tool args and would message the wrong target. Financial/ID
        PII (NIE/NIF/CIF/IBAN) stays tokenized by default (protective; rehydrated
        at the external-dispatch boundary if the agent uses it in a tool arg).
        """
        patterns: list[_Pattern] = []
        if default_patterns_enabled:
            patterns.extend(
                p
                for p in (
                    _Pattern("NIE", self._NIE_RE),
                    _Pattern("NIF", self._NIF_RE),
                    _Pattern("CIF", self._CIF_RE),
                    _Pattern("IBAN", self._IBAN_RE),
                    _Pattern("EMAIL", self._EMAIL_RE),
                    _Pattern("TEL", self._PHONE_ES_RE),
                )
                if p.name not in exclude_patterns
            )
        for name, regex in extra_patterns:
            patterns.append(_Pattern(name, regex))
        self._patterns: tuple[_Pattern, ...] = tuple(patterns)

    # ------------------------------------------------------------------
    # Tokenize
    # ------------------------------------------------------------------

    def tokenize(self, payload: Any) -> TokenizedPayload:
        state: dict[str, dict[str, str]] = {}  # type -> {value -> placeholder}
        counter: dict[str, int] = {}
        mapping: dict[str, str] = {}

        def _tok_value(value: Any) -> Any:
            if isinstance(value, str):
                return self._replace_in_str(value, state, counter, mapping)
            if isinstance(value, dict):
                return {k: _tok_value(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_tok_value(v) for v in value]
            if isinstance(value, tuple):
                return tuple(_tok_value(v) for v in value)
            return value

        sanitized = _tok_value(payload)
        return TokenizedPayload(
            sanitized=sanitized,
            mapping=mapping,
            replaced=sum(counter.values()),
        )

    def _replace_in_str(
        self,
        text: str,
        state: dict[str, dict[str, str]],
        counter: dict[str, int],
        mapping: dict[str, str],
    ) -> str:
        out = text
        for pattern in self._patterns:
            type_state = state.setdefault(pattern.name, {})

            def _sub(match: re.Match[str], _name: str = pattern.name) -> str:
                value = match.group(0)
                ts = state[_name]
                if value in ts:
                    return ts[value]
                counter[_name] = counter.get(_name, 0) + 1
                placeholder = f"[[{_name}_{counter[_name]}]]"
                ts[value] = placeholder
                mapping[placeholder] = value
                return placeholder

            out = pattern.regex.sub(_sub, out)
            state[pattern.name] = type_state
        return out

    # ------------------------------------------------------------------
    # Rehydrate
    # ------------------------------------------------------------------

    _PLACEHOLDER_RE = re.compile(r"\[\[[A-Z_]+_[0-9]+\]\]")

    def rehydrate(self, text: str, mapping: dict[str, str]) -> str:
        """Restaura los valores reales. Falla si encuentra un placeholder inventado."""

        def _sub(match: re.Match[str]) -> str:
            placeholder = match.group(0)
            if placeholder not in mapping:
                raise UnknownPlaceholderError(
                    f"LLM emitio placeholder {placeholder!r} que no estaba en el mapping. "
                    "Posible alucinacion o intento de inyeccion."
                )
            return mapping[placeholder]

        return self._PLACEHOLDER_RE.sub(_sub, text)
