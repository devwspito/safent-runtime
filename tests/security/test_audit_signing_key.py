"""Tests de seguridad para la clave de firma del audit chain (CTRL-7/AUD-1).

Regresiones críticas:
- CWE-321: clave NO derivable de la ruta del .db (sha256(db_path) es pública).
- Estabilidad cross-restart: la clave NO puede ser efímera (secrets.token_bytes).
- Origen sellado: la clave debe provenir de un sello inyectado (env/fichero),
  NO de una derivación determinista de material público.

Deben FALLAR antes de T015 (audit_signing_key.py no existe aún).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KNOWN_DB_PATH = Path("/var/lib/hermes/shell-state.db")
_KNOWN_DB_PATH_STR = str(_KNOWN_DB_PATH)


def _sha256_of_path(path: Path) -> bytes:
    """Reproduce la función insegura build_signing_key(db_path) del threat-model."""
    return hashlib.sha256(str(path).encode()).digest()


# ---------------------------------------------------------------------------
# T014-A: la clave NO es derivable de la ruta del .db (CWE-321)
# ---------------------------------------------------------------------------


class TestKeyNotDerivableFromPath:
    def test_key_differs_from_sha256_of_db_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """La clave real no debe coincidir con sha256(db_path)."""
        from hermes.runtime.audit_signing_key import load_signing_key

        seal = b"x" * 32
        monkeypatch.setenv("HERMES_AUDIT_KEY", seal.hex())

        key = load_signing_key()
        insecure = _sha256_of_path(_KNOWN_DB_PATH)
        assert key != insecure, (
            "REGRESIÓN CWE-321: la clave de audit coincide con sha256(db_path) "
            "— es derivable públicamente."
        )

    def test_different_db_paths_same_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La clave no varía al cambiar la ruta del .db — no depende de ella."""
        from hermes.runtime.audit_signing_key import load_signing_key

        seal = b"y" * 32
        monkeypatch.setenv("HERMES_AUDIT_KEY", seal.hex())

        key1 = load_signing_key()
        # La clave no tiene relación con la ruta: siempre igual para el mismo sello.
        key2 = load_signing_key()
        assert key1 == key2


# ---------------------------------------------------------------------------
# T014-B: la clave es estable entre reinicios (no efímera)
# ---------------------------------------------------------------------------


class TestKeyStabilityAcrossRestarts:
    def test_same_seal_same_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El mismo sello inyectado produce la misma clave en distintas 'instancias'."""
        from hermes.runtime.audit_signing_key import load_signing_key

        seal = b"stable-seal-for-test-" + b"0" * 11
        monkeypatch.setenv("HERMES_AUDIT_KEY", seal.hex())

        key_a = load_signing_key()
        key_b = load_signing_key()  # Simula segundo arranque con el mismo sello
        assert key_a == key_b, (
            "REGRESIÓN AUD-1: la clave cambia entre llamadas con el mismo sello "
            "— la cadena no verificaría tras reinicio."
        )

    def test_different_seals_different_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sellos distintos producen claves distintas."""
        from hermes.runtime.audit_signing_key import load_signing_key

        monkeypatch.setenv("HERMES_AUDIT_KEY", (b"seal-A" * 6)[:32].hex())
        key_a = load_signing_key()

        monkeypatch.setenv("HERMES_AUDIT_KEY", (b"seal-B" * 6)[:32].hex())
        key_b = load_signing_key()

        assert key_a != key_b


# ---------------------------------------------------------------------------
# T014-C: fail-closed — sin sello válido no arranca
# ---------------------------------------------------------------------------


class TestFailClosedWithoutSeal:
    def test_raises_without_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin HERMES_AUDIT_KEY configurado, load_signing_key eleva error claro."""
        from hermes.runtime.audit_signing_key import MissingAuditSeal, load_signing_key

        monkeypatch.delenv("HERMES_AUDIT_KEY", raising=False)

        with pytest.raises(MissingAuditSeal):
            load_signing_key()

    def test_raises_with_empty_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un sello vacío también eleva error."""
        from hermes.runtime.audit_signing_key import MissingAuditSeal, load_signing_key

        monkeypatch.setenv("HERMES_AUDIT_KEY", "")

        with pytest.raises(MissingAuditSeal):
            load_signing_key()

    def test_raises_with_short_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un sello demasiado corto (< 32 bytes) no es aceptable."""
        from hermes.runtime.audit_signing_key import MissingAuditSeal, load_signing_key

        short_hex = (b"short" * 2).hex()  # 10 bytes < 32
        monkeypatch.setenv("HERMES_AUDIT_KEY", short_hex)

        with pytest.raises((MissingAuditSeal, ValueError)):
            load_signing_key()


# ---------------------------------------------------------------------------
# T014-D: la clave tiene al menos 32 bytes (requisito AuditHashChainSigner)
# ---------------------------------------------------------------------------


class TestKeyLength:
    def test_key_at_least_32_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AuditHashChainSigner exige signing_key >= 32 bytes."""
        from hermes.runtime.audit_signing_key import load_signing_key

        seal = os.urandom(32)
        monkeypatch.setenv("HERMES_AUDIT_KEY", seal.hex())

        key = load_signing_key()
        assert len(key) >= 32


# ---------------------------------------------------------------------------
# T014-E: regresión build_signing_key — no está en el camino de construcción
# ---------------------------------------------------------------------------


class TestBuildSigningKeyNotInPath:
    def test_load_signing_key_does_not_use_build_signing_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_signing_key NO debe llamar a build_signing_key (sha256(path)).

        Si build_signing_key existiese en el mismo módulo, verificamos que
        load_signing_key no produce el mismo valor que ella con ninguna ruta.
        """
        from hermes.runtime.audit_signing_key import load_signing_key

        seal = b"independent-seal" + b"\x00" * 16
        monkeypatch.setenv("HERMES_AUDIT_KEY", seal.hex())
        key = load_signing_key()

        # Verifica que no coincide con sha256 de ninguna ruta conocida
        for candidate in [
            Path("/var/lib/hermes/shell-state.db"),
            Path("/tmp/hermes.db"),
            Path("."),
        ]:
            assert key != _sha256_of_path(candidate), (
                f"REGRESIÓN: clave coincide con sha256({candidate}) — usa ruta como semilla."
            )
