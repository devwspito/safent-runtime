from __future__ import annotations

import re

import pytest

from hermes import DefaultPIITokenizer
from hermes.tokenizer.pii import UnknownPlaceholderError


def test_tokenizes_nif() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize("Cliente NIF 12345678Z presenta 303")
    assert "12345678Z" not in out.sanitized
    assert "[[NIF_1]]" in out.sanitized
    assert out.mapping["[[NIF_1]]"] == "12345678Z"
    assert out.replaced == 1


def test_tokenizes_nie() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize("Trabajador NIE X1234567L")
    assert "X1234567L" not in out.sanitized
    assert "[[NIE_1]]" in out.sanitized


def test_tokenizes_cif() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize("Empresa A12345674 con sede en Madrid")
    assert "A12345674" not in out.sanitized
    assert "[[CIF_1]]" in out.sanitized


def test_tokenizes_iban() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize("Domicilia en ES9121000418450200051332")
    assert "ES9121000418450200051332" not in out.sanitized
    assert "[[IBAN_1]]" in out.sanitized


def test_tokenizes_email() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize("Contacto: pepe.perez@example.com")
    assert "pepe.perez@example.com" not in out.sanitized


def test_same_value_reuses_placeholder() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize(
        "NIF 12345678Z aparece dos veces: 12345678Z otra vez"
    )
    # solo se crea un placeholder distinto
    occurrences = out.sanitized.count("[[NIF_1]]")
    assert occurrences == 2
    assert len(out.mapping) == 1


def test_tokenizes_nested_dict() -> None:
    tk = DefaultPIITokenizer()
    payload = {
        "cliente": {"nif": "12345678Z", "iban": "ES9121000418450200051332"},
        "notas": ["pago de 12345678Z pendiente"],
    }
    out = tk.tokenize(payload)
    assert out.sanitized["cliente"]["nif"] == "[[NIF_1]]"
    assert out.sanitized["cliente"]["iban"] == "[[IBAN_1]]"
    # nested list value tokenized too
    assert "[[NIF_1]]" in out.sanitized["notas"][0]


def test_extra_patterns_per_vertical() -> None:
    tk = DefaultPIITokenizer(
        extra_patterns=(("MASCOTA", re.compile(r"MAS-\d{6}")),),
    )
    out = tk.tokenize("Mascota MAS-000123 vacunada")
    assert "MAS-000123" not in out.sanitized
    assert "[[MASCOTA_1]]" in out.sanitized


def test_rehydrate_restores_values() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize("NIF 12345678Z presenta 303")
    text_from_llm = "Borrador del [[NIF_1]] listo, importe 4812.33 EUR"
    rehydrated = tk.rehydrate(text_from_llm, out.mapping)
    assert rehydrated == "Borrador del 12345678Z listo, importe 4812.33 EUR"


def test_rehydrate_fails_on_unknown_placeholder() -> None:
    tk = DefaultPIITokenizer()
    out = tk.tokenize("NIF 12345678Z")
    with pytest.raises(UnknownPlaceholderError):
        tk.rehydrate("Borrador del [[NIF_9]] inventado", out.mapping)
