"""Tests de integridad del audit chain + SqliteAuditRepository (CTRL-7/CTRL-9/AUD-1).

Regresiones cubiertas:
- append persiste de forma append-only (no mutación de entradas anteriores).
- head_hash_hex() devuelve el hash de la última entrada.
- load_chain() devuelve la cadena en orden para verify_chain.
- Siembra de _last_hash al boot mantiene la cadena cross-restart (regresión AUD-1).
- La cadena verifica después de reinicio (siembra correcta).
- Mutación detectada por verify_chain (AuditChainCorrupted).

Los tests deben FALLAR antes de T017 (SqliteAuditRepository no existe aún).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditChainCorrupted,
    AuditHashChainSigner,
    AuditKind,
)

pytestmark = pytest.mark.unit

_SEAL = os.urandom(32)


def _signer(*, seed_hash: bytes | None = None) -> AuditHashChainSigner:
    signer = AuditHashChainSigner(signing_key=_SEAL)
    if seed_hash is not None:
        # Simula la siembra del head desde la DB al reiniciar
        object.__setattr__(signer, "_last_hash", seed_hash)
    return signer


def _append_entry(
    signer: AuditHashChainSigner,
    description: str = "test entry",
) -> object:
    return signer.append(
        audit_kind=AuditKind.TASK_ENQUEUED,
        actor="test",
        description=description,
        payload={"key": description},
        tenant_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# Fixture: SqliteAuditRepository con DB temporal
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_factory(tmp_path: Path):
    """Devuelve una factory de SqliteAuditRepository sobre un tmp_path."""

    def _make(db_path: Path | None = None) -> object:
        from hermes.agents_os.infrastructure.sqlite_audit_repository import (
            SqliteAuditRepository,
        )

        return SqliteAuditRepository(db_path=db_path or tmp_path / "audit.db")

    return _make


# ---------------------------------------------------------------------------
# append: persist append-only
# ---------------------------------------------------------------------------


class TestAppendPersistsAppendOnly:
    @pytest.mark.integration
    async def test_append_persists_entry(self, repo_factory) -> None:
        repo = repo_factory()
        signer = _signer()
        entry = _append_entry(signer)
        await repo.append(entry)

        chain = await repo.load_chain()
        assert len(chain) == 1

    @pytest.mark.integration
    async def test_append_multiple_entries_ordered(self, repo_factory) -> None:
        repo = repo_factory()
        signer = _signer()
        e1 = _append_entry(signer, "first")
        e2 = _append_entry(signer, "second")
        await repo.append(e1)
        await repo.append(e2)

        chain = await repo.load_chain()
        assert len(chain) == 2
        # Orden ascendente de inserción
        assert chain[0].entry_id == e1.entry_id
        assert chain[1].entry_id == e2.entry_id

    @pytest.mark.integration
    async def test_append_only_no_overwrite(self, repo_factory) -> None:
        """No se puede sobreescribir una entrada — PK constraint."""
        repo = repo_factory()
        signer = _signer()
        entry = _append_entry(signer)
        await repo.append(entry)

        # Intentar insertar de nuevo el mismo entry_id debe ser no-op o raise
        # (el repo elige INSERT OR IGNORE — append-only, no falla ruidosamente
        # en tests pero tampoco muta)
        await repo.append(entry)
        chain = await repo.load_chain()
        assert len(chain) == 1  # sigue siendo 1 — no duplicado


# ---------------------------------------------------------------------------
# head_hash_hex: devuelve el hash de la última entrada
# ---------------------------------------------------------------------------


class TestHeadHashHex:
    @pytest.mark.integration
    async def test_head_hash_empty_returns_none(self, repo_factory) -> None:
        repo = repo_factory()
        head = await repo.head_hash_hex()
        assert head is None

    @pytest.mark.integration
    async def test_head_hash_matches_last_entry(self, repo_factory) -> None:
        repo = repo_factory()
        signer = _signer()
        e1 = _append_entry(signer, "first")
        e2 = _append_entry(signer, "second")
        await repo.append(e1)
        await repo.append(e2)

        head = await repo.head_hash_hex()
        assert head == e2.signed_payload_hash_hex

    @pytest.mark.integration
    async def test_head_hash_single_entry(self, repo_factory) -> None:
        repo = repo_factory()
        signer = _signer()
        entry = _append_entry(signer)
        await repo.append(entry)

        head = await repo.head_hash_hex()
        assert head == entry.signed_payload_hash_hex


# ---------------------------------------------------------------------------
# load_chain: ordena para verify_chain
# ---------------------------------------------------------------------------


class TestLoadChainOrdering:
    @pytest.mark.integration
    async def test_load_chain_empty(self, repo_factory) -> None:
        repo = repo_factory()
        chain = await repo.load_chain()
        assert chain == []

    @pytest.mark.integration
    async def test_load_chain_verify_chain_passes(self, repo_factory) -> None:
        repo = repo_factory()
        signer = _signer()
        for i in range(5):
            entry = _append_entry(signer, f"entry-{i}")
            await repo.append(entry)

        chain = await repo.load_chain()
        assert len(chain) == 5

        # verify_chain debe pasar sin lanzar AuditChainCorrupted
        verifier = AuditHashChainSigner(signing_key=_SEAL)
        verifier.verify_chain(chain)  # No raise

    @pytest.mark.integration
    async def test_load_chain_by_tenant(self, repo_factory) -> None:
        repo = repo_factory()
        signer = _signer()
        tenant_a = uuid4()
        tenant_b = uuid4()

        ea = signer.append(
            audit_kind=AuditKind.TASK_ENQUEUED,
            actor="test",
            description="tenant A",
            payload={},
            tenant_id=tenant_a,
        )
        eb = signer.append(
            audit_kind=AuditKind.TASK_CLAIMED,
            actor="test",
            description="tenant B",
            payload={},
            tenant_id=tenant_b,
        )
        await repo.append(ea)
        await repo.append(eb)

        chain_a = await repo.load_chain(tenant_id=tenant_a)
        assert len(chain_a) == 1
        assert chain_a[0].entry_id == ea.entry_id


# ---------------------------------------------------------------------------
# Cross-restart integrity (siembra del _last_hash)
# ---------------------------------------------------------------------------


class TestCrossRestartIntegrity:
    @pytest.mark.integration
    async def test_chain_verifies_after_restart(
        self, tmp_path: Path, repo_factory
    ) -> None:
        """AUD-1 regression: la cadena verifica entre reinicios.

        Simula:
        1. Primera 'ejecución': crea repo, añade entradas.
        2. 'Reinicio': nuevo repo, siembra _last_hash desde head_hash_hex().
        3. Nueva entrada con el signer sembrado.
        4. verify_chain sobre la cadena completa pasa.
        """
        db = tmp_path / "audit.db"

        # --- Primera ejecución ---
        repo1 = repo_factory(db)
        signer1 = _signer()
        e1 = _append_entry(signer1, "pre-restart")
        await repo1.append(e1)

        head_before = await repo1.head_hash_hex()
        assert head_before is not None

        # --- Segunda ejecución (reinicio) ---
        repo2 = repo_factory(db)
        head_loaded = await repo2.head_hash_hex()
        assert head_loaded == head_before, (
            "head_hash_hex() debe ser consistente entre instancias sobre el mismo .db"
        )

        # Sembrar el firmer con el head persistido
        seed = bytes.fromhex(head_loaded)
        signer2 = _signer(seed_hash=seed)
        e2 = _append_entry(signer2, "post-restart")
        await repo2.append(e2)

        # La cadena completa debe verificar
        full_chain = await repo2.load_chain()
        assert len(full_chain) == 2

        verifier = AuditHashChainSigner(signing_key=_SEAL)
        verifier.verify_chain(full_chain)  # No raise

    @pytest.mark.integration
    async def test_chain_broken_without_seed(
        self, tmp_path: Path, repo_factory
    ) -> None:
        """Sin sembrar el head, el segundo signer parte de genesis => la cadena rompe."""
        db = tmp_path / "audit.db"

        repo1 = repo_factory(db)
        signer1 = _signer()
        e1 = _append_entry(signer1, "pre-restart")
        await repo1.append(e1)

        repo2 = repo_factory(db)
        # Signer2 SIN sembrar — arranca desde genesis
        signer2 = AuditHashChainSigner(signing_key=_SEAL)
        e2 = _append_entry(signer2, "post-restart-no-seed")
        await repo2.append(e2)

        full_chain = await repo2.load_chain()
        verifier = AuditHashChainSigner(signing_key=_SEAL)
        with pytest.raises(AuditChainCorrupted):
            verifier.verify_chain(full_chain)

    @pytest.mark.integration
    async def test_seed_from_repo_head_matches_signer_head(
        self, tmp_path: Path, repo_factory
    ) -> None:
        """El head_hash_hex() del repo debe coincidir con signer.head_hash_hex."""
        db = tmp_path / "audit.db"
        repo = repo_factory(db)
        signer = _signer()
        e = _append_entry(signer)
        await repo.append(e)

        repo_head = await repo.head_hash_hex()
        signer_head = signer.head_hash_hex
        assert repo_head == signer_head


# ---------------------------------------------------------------------------
# Mutación detectada por verify_chain
# ---------------------------------------------------------------------------


class TestTamperDetection:
    @pytest.mark.integration
    async def test_verify_chain_detects_payload_mutation(
        self, repo_factory
    ) -> None:
        """Si una entrada es mutada en memoria, verify_chain debe elevar AuditChainCorrupted."""
        from dataclasses import fields

        repo = repo_factory()
        signer = _signer()
        e1 = _append_entry(signer, "original")
        e2 = _append_entry(signer, "second")
        await repo.append(e1)
        await repo.append(e2)

        chain = await repo.load_chain()

        # Mutar el payload_hash_hex de la primera entrada
        corrupted_fields = {f.name: getattr(chain[0], f.name) for f in fields(chain[0])}
        corrupted_fields["payload_hash_hex"] = "0" * 64
        from hermes.agents_os.application.audit_hash_chain import AuditEntry

        corrupted = AuditEntry(**corrupted_fields)
        tampered_chain = [corrupted, chain[1]]

        verifier = AuditHashChainSigner(signing_key=_SEAL)
        with pytest.raises(AuditChainCorrupted):
            verifier.verify_chain(tampered_chain)


# ---------------------------------------------------------------------------
# T032 (parte ancla) — ExternalAnchorPort detecta cadena local reescrita
# ---------------------------------------------------------------------------


class TestExternalAnchorIntegrity:
    @pytest.mark.integration
    async def test_anchor_called_after_append(self, tmp_path: Path) -> None:
        """SqliteAuditRepository llama a anchor() después de cada append (CTRL-8)."""
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

        anchor = FakeExternalAnchor()
        repo = SqliteAuditRepository(db_path=tmp_path / "audit.db", external_anchor=anchor)
        signer = _signer()

        assert len(anchor.anchored) == 0
        e = _append_entry(signer)
        await repo.append(e)
        assert len(anchor.anchored) == 1
        assert anchor.anchored[0] == e.signed_payload_hash_hex

    @pytest.mark.integration
    async def test_anchor_verify_fails_if_local_head_diverges(
        self, tmp_path: Path
    ) -> None:
        """anchor.verify(tampered_head) ⇒ False — detecta cadena reescrita (AUD-2)."""
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

        anchor = FakeExternalAnchor()
        repo = SqliteAuditRepository(db_path=tmp_path / "audit.db", external_anchor=anchor)
        signer = _signer()

        entry = _append_entry(signer)
        await repo.append(entry)

        real_head = signer.head_hash_hex
        assert await anchor.verify(real_head) is True

        # Simula cadena reescrita desde genesis
        rewrite_signer = AuditHashChainSigner(signing_key=_SEAL)
        _append_entry(rewrite_signer, "tampered")
        tampered_head = rewrite_signer.head_hash_hex

        assert tampered_head != real_head
        assert await anchor.verify(tampered_head) is False

    @pytest.mark.integration
    async def test_anchor_verify_fails_with_no_anchors(self) -> None:
        """FakeExternalAnchor.verify ⇒ False si no hay ningún hash anclado (fail-closed)."""
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

        anchor = FakeExternalAnchor()
        assert await anchor.verify("any_hash") is False

    @pytest.mark.integration
    async def test_integrity_chain_and_anchor_consistent(self, tmp_path: Path) -> None:
        """El head_hash anclado coincide con el head del repo tras append (CTRL-8)."""
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

        anchor = FakeExternalAnchor()
        repo = SqliteAuditRepository(db_path=tmp_path / "audit.db", external_anchor=anchor)
        signer = _signer()

        for i in range(3):
            e = _append_entry(signer, f"entry-{i}")
            await repo.append(e)

        repo_head = await repo.head_hash_hex()
        assert repo_head is not None
        latest_anchor = await anchor.get_latest()
        assert latest_anchor == repo_head


# ---------------------------------------------------------------------------
# T042 (CTRL-8) — TsaExternalAnchor con FakeTsaTransport (sin red)
# ---------------------------------------------------------------------------

_TST_FIXTURE = (
    Path(__file__).parent.parent
    / "capabilities"
    / "fixtures"
    / "fixture_tst.bin"
)
_TSR_FIXTURE = (
    Path(__file__).parent.parent
    / "capabilities"
    / "fixtures"
    / "fixture_tsr.bin"
)
_CA_CERT = (
    Path(__file__).parent.parent.parent
    / "ops"
    / "audit"
    / "freetsa_tsa.crt"
)
_FIXTURE_HASH = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TAMPERED_HASH = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class _FakeTsaTransport:
    """FakeTsaTransport local para los tests de auditoría de seguridad."""

    def __init__(self, *, response_bytes: bytes | None = None, fail: bool = False) -> None:
        self._response = response_bytes
        self._fail = fail

    async def post_timestamp_query(
        self, *, url: str, body: bytes, timeout_s: float  # noqa: ARG002
    ) -> bytes:
        from hermes.capabilities.infrastructure.tsa_external_anchor import TsaNetworkError

        if self._fail:
            raise TsaNetworkError("fake network error")
        if self._response is None:
            raise TsaNetworkError("no response configured")
        return self._response


class TestTsaExternalAnchorWithRepo:
    """Integra TsaExternalAnchor con SqliteAuditRepository (FakeTsaTransport)."""

    @pytest.mark.integration
    async def test_tsa_anchor_called_after_repo_append(self, tmp_path: Path) -> None:
        """SqliteAuditRepository llama a TsaExternalAnchor.anchor() tras append."""
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.infrastructure.tsa_external_anchor import TsaExternalAnchor

        tsr_bytes = _TSR_FIXTURE.read_bytes()
        ca_cert = _CA_CERT.read_bytes()
        token_dir = tmp_path / "tokens"

        tsa = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=ca_cert,
            transport=_FakeTsaTransport(response_bytes=tsr_bytes),
        )
        repo = SqliteAuditRepository(db_path=tmp_path / "audit.db", external_anchor=tsa)
        signer = _signer()

        entry = _append_entry(signer, "tsa-anchor-test")
        await repo.append(entry)

        head = await repo.head_hash_hex()
        assert head is not None
        # The TST file is keyed by the real head_hash, not the fixture hash.
        # Since transport returns the fixture TSR (which has FIXTURE_HASH imprint),
        # the TST verification would fail. But the TST file was persisted → anchor ran.
        tst_files = list(token_dir.glob("*.tsr"))
        assert len(tst_files) == 1

    @pytest.mark.integration
    async def test_tsa_verify_detects_tampered_chain(self, tmp_path: Path) -> None:
        """verify() returns False for a hash with no TST → tampering detected (AUD-2)."""
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.infrastructure.tsa_external_anchor import TsaExternalAnchor

        ca_cert = _CA_CERT.read_bytes()
        token_dir = tmp_path / "tokens"
        token_dir.mkdir()

        # Place TST for FIXTURE_HASH only.
        (token_dir / f"{_FIXTURE_HASH}.tsr").write_bytes(_TST_FIXTURE.read_bytes())

        tsa = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=ca_cert,
            transport=_FakeTsaTransport(fail=True),
        )
        repo = SqliteAuditRepository(db_path=tmp_path / "audit.db", external_anchor=tsa)
        signer = _signer()
        entry = _append_entry(signer, "tamper-scenario")
        await repo.append(entry)

        # Verify using the tampered (unknown) head — no TST → False.
        real_head = await repo.head_hash_hex()
        assert real_head is not None
        # real_head != _FIXTURE_HASH, so verify → False (no TST for this hash).
        if real_head != _FIXTURE_HASH:
            result = await tsa.verify(real_head)
            assert result is False  # no TST was persisted for real_head (network failed)

    @pytest.mark.integration
    async def test_tsa_fail_open_does_not_block_append(self, tmp_path: Path) -> None:
        """TSA network failure does NOT prevent audit entries from being persisted."""
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.infrastructure.tsa_external_anchor import TsaExternalAnchor

        ca_cert = _CA_CERT.read_bytes()
        tsa = TsaExternalAnchor(
            token_dir=tmp_path / "tokens",
            ca_cert_pem=ca_cert,
            transport=_FakeTsaTransport(fail=True),
        )
        repo = SqliteAuditRepository(db_path=tmp_path / "audit.db", external_anchor=tsa)
        signer = _signer()

        for i in range(5):
            e = _append_entry(signer, f"entry-{i}")
            await repo.append(e)

        chain = await repo.load_chain()
        assert len(chain) == 5  # all persisted despite TSA failure
