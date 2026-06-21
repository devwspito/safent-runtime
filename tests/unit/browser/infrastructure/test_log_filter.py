"""Tests del PII redactor de structlog (T1001).

Verifica que ningún NIF/IBAN/email/password escapa al log tras el filtro.
Constitución III: PII jamás en logs.
Threat-model I1 superficie 1.
"""

from __future__ import annotations

from hermes.browser.infrastructure.log_filter import pii_redactor


class TestPiiRedactorSensitiveKeys:
    """Claves con nombre sensible → <<REDACTED>> sin mirar el valor."""

    def _run(self, event_dict: dict) -> dict:
        return pii_redactor(None, "info", event_dict)

    def test_nif_key_redacted(self) -> None:
        out = self._run({"nif": "12345678Z", "msg": "ok"})
        assert out["nif"] == "<<REDACTED>>"
        assert out["msg"] == "ok"

    def test_iban_key_redacted(self) -> None:
        out = self._run({"iban": "ES9121000418450200051332"})
        assert out["iban"] == "<<REDACTED>>"

    def test_password_key_redacted(self) -> None:
        out = self._run({"password": "s3cr3t"})
        assert out["password"] == "<<REDACTED>>"

    def test_token_key_redacted(self) -> None:
        out = self._run({"token": "abc123"})
        assert out["token"] == "<<REDACTED>>"

    def test_email_key_redacted(self) -> None:
        out = self._run({"email": "user@example.com"})
        assert out["email"] == "<<REDACTED>>"

    def test_dni_key_redacted(self) -> None:
        out = self._run({"dni": "12345678Z"})
        assert out["dni"] == "<<REDACTED>>"

    def test_case_insensitive_key(self) -> None:
        out = self._run({"NIF": "12345678Z"})
        assert out["NIF"] == "<<REDACTED>>"


class TestPiiRedactorRegexSweep:
    """Valores en texto libre con patrones PII → redactados."""

    def _run(self, event_dict: dict) -> dict:
        return pii_redactor(None, "info", event_dict)

    def test_nif_pattern_in_value_redacted(self) -> None:
        out = self._run({"msg": "procesando cliente 12345678Z"})
        assert "12345678Z" not in out["msg"]
        assert "<<NIF_REDACTED>>" in out["msg"]

    def test_iban_es_in_value_redacted(self) -> None:
        out = self._run({"msg": "cuenta ES9121000418450200051332"})
        assert "ES9121000418450200051332" not in out["msg"]
        assert "<<IBAN_REDACTED>>" in out["msg"]

    def test_email_in_value_redacted(self) -> None:
        out = self._run({"msg": "enviado a user@example.com"})
        assert "user@example.com" not in out["msg"]
        assert "<<EMAIL_REDACTED>>" in out["msg"]

    def test_nie_pattern_redacted(self) -> None:
        out = self._run({"msg": "NIE X1234567L"})
        assert "X1234567L" not in out["msg"]
        assert "<<NIE_REDACTED>>" in out["msg"]

    def test_clean_value_unchanged(self) -> None:
        out = self._run({"msg": "operacion completada", "step": 3})
        assert out["msg"] == "operacion completada"
        assert out["step"] == 3


class TestPiiRedactorNestedStructures:
    """Redacción recursiva en dicts y listas."""

    def _run(self, event_dict: dict) -> dict:
        return pii_redactor(None, "info", event_dict)

    def test_nested_dict_key_redacted(self) -> None:
        out = self._run({"context": {"nif": "12345678Z", "ok": True}})
        assert out["context"]["nif"] == "<<REDACTED>>"
        assert out["context"]["ok"] is True

    def test_list_of_strings_swept(self) -> None:
        out = self._run({"items": ["cliente 12345678Z", "no-pii-here"]})
        assert "12345678Z" not in out["items"][0]
        assert out["items"][1] == "no-pii-here"

    def test_non_string_values_unchanged(self) -> None:
        out = self._run({"count": 42, "ratio": 0.9, "flag": True})
        assert out["count"] == 42
        assert out["ratio"] == 0.9
        assert out["flag"] is True
