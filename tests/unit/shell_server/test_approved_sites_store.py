"""Tests de lógica pura para ApprovedSitesStore y approved_sites_provider.

No requieren GTK ni display. Se ejecutan en cualquier entorno.

Cubre:
  (a) Persistencia — save/load round-trip, defaults, archivo corrupto.
  (b) Validación de dominio — dominios válidos/inválidos, duplicados, normalización.
  (c) as_frozenset — lista vacía = frozenset vacío (fail-closed verificado aquí).
  (d) approved_sites_provider — integración con BrowserSurfaceAdapter._host_is_approved.
  (e) Regresión fail-closed — frozenset vacío → WRITE denegado (sin instanciar GTK).
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helper — instanciar el store apuntando a un directorio temporal
# ---------------------------------------------------------------------------

def _make_store(tmp_path):
    os.environ["XDG_CONFIG_HOME"] = str(tmp_path)
    # Forzar re-importación limpia del módulo para que _config_dir() use el nuevo env.
    import importlib
    import hermes.shell.presentation.gtk4.approved_sites_store as mod
    importlib.reload(mod)
    return mod.ApprovedSitesStore()


# ---------------------------------------------------------------------------
# (a) Persistencia
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_lista_vacia_por_defecto_sin_archivo(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.sites == []

    def test_save_y_load_round_trip(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("example.com")
        store.add("app.empresa.es")

        store2 = _make_store(tmp_path)
        assert "example.com" in store2.sites
        assert "app.empresa.es" in store2.sites
        assert len(store2.sites) == 2

    def test_lista_ordenada_tras_carga(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("zeta.com")
        store.add("alpha.com")
        store.add("medium.com")

        store2 = _make_store(tmp_path)
        assert store2.sites == sorted(store2.sites)

    def test_archivo_corrupto_usa_lista_vacia(self, tmp_path) -> None:
        config_dir = tmp_path / "hermes-shell"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "hermes-approved-sites.json").write_text("NO ES JSON {{{}}}",
                                                                encoding="utf-8")
        store = _make_store(tmp_path)
        assert store.sites == []

    def test_campo_sites_no_es_lista_usa_lista_vacia(self, tmp_path) -> None:
        config_dir = tmp_path / "hermes-shell"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "hermes-approved-sites.json").write_text(
            json.dumps({"sites": "no-es-lista"}), encoding="utf-8"
        )
        store = _make_store(tmp_path)
        assert store.sites == []

    def test_entradas_invalidas_en_disco_son_ignoradas(self, tmp_path) -> None:
        config_dir = tmp_path / "hermes-shell"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "hermes-approved-sites.json").write_text(
            json.dumps({"sites": ["good.com", "http://bad.com", "", "   "]}),
            encoding="utf-8",
        )
        store = _make_store(tmp_path)
        assert store.sites == ["good.com"]

    def test_save_error_escritura_no_lanza(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("example.com")
        config_dir = tmp_path / "hermes-shell"
        config_dir.chmod(0o555)
        try:
            store.save()  # no debe lanzar
        finally:
            config_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# (b) Validación de dominio — add() y remove()
# ---------------------------------------------------------------------------

class TestDomainValidation:

    def test_dominio_simple_valido(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.add("example.com") is True
        assert "example.com" in store.sites

    def test_subdominio_valido(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.add("api.example.com") is True

    def test_normaliza_a_lowercase(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("EXAMPLE.COM")
        assert "example.com" in store.sites

    def test_dominio_con_esquema_rechazado(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.add("https://example.com") is False
        assert store.add("http://example.com") is False
        assert store.sites == []

    def test_dominio_con_path_rechazado(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.add("example.com/ruta/pagina") is False
        assert store.sites == []

    def test_dominio_vacio_rechazado(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.add("") is False
        assert store.add("   ") is False
        assert store.sites == []

    def test_dominio_duplicado_devuelve_false(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("example.com")
        result = store.add("example.com")
        assert result is False
        assert store.sites.count("example.com") == 1

    def test_duplicado_case_insensitive(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("example.com")
        result = store.add("EXAMPLE.COM")
        assert result is False
        assert len(store.sites) == 1

    def test_remove_dominio_existente(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("example.com")
        assert store.remove("example.com") is True
        assert "example.com" not in store.sites

    def test_remove_dominio_inexistente_devuelve_false(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.remove("noexiste.com") is False

    def test_remove_persiste_en_disco(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("a.com")
        store.add("b.com")
        store.remove("a.com")

        store2 = _make_store(tmp_path)
        assert "a.com" not in store2.sites
        assert "b.com" in store2.sites

    def test_dominio_con_guion_valido(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.add("my-company.com") is True

    def test_localhost_valido(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        assert store.add("localhost") is True


# ---------------------------------------------------------------------------
# (c) as_frozenset — fail-closed
# ---------------------------------------------------------------------------

class TestAsFrozenset:

    def test_lista_vacia_devuelve_frozenset_vacio(self, tmp_path) -> None:
        """Fail-closed: sin sitios aprobados, frozenset vacío → WRITE denegado."""
        store = _make_store(tmp_path)
        assert store.as_frozenset() == frozenset()

    def test_sitios_presentes_en_frozenset(self, tmp_path) -> None:
        store = _make_store(tmp_path)
        store.add("example.com")
        store.add("other.org")
        fs = store.as_frozenset()
        assert "example.com" in fs
        assert "other.org" in fs

    def test_frozenset_es_snapshot_independiente(self, tmp_path) -> None:
        """El frozenset no cambia si luego se añade otro sitio al store."""
        store = _make_store(tmp_path)
        store.add("example.com")
        snapshot = store.as_frozenset()
        store.add("extra.com")
        assert "extra.com" not in snapshot


# ---------------------------------------------------------------------------
# (d) approved_sites_provider — integración con _host_is_approved
# ---------------------------------------------------------------------------

class TestApprovedSitesProvider:

    def test_provider_devuelve_frozenset_del_store(self, tmp_path) -> None:
        from hermes.shell.presentation.gtk4.approved_sites_store import (  # noqa: PLC0415
            approved_sites_provider,
        )
        from uuid import uuid4  # noqa: PLC0415

        store = _make_store(tmp_path)
        store.add("example.com")

        provider = approved_sites_provider(store)
        result = provider(uuid4())
        assert "example.com" in result

    def test_provider_con_store_vacio_devuelve_frozenset_vacio(self, tmp_path) -> None:
        from hermes.shell.presentation.gtk4.approved_sites_store import (  # noqa: PLC0415
            approved_sites_provider,
        )
        from uuid import uuid4  # noqa: PLC0415

        store = _make_store(tmp_path)
        provider = approved_sites_provider(store)
        assert provider(uuid4()) == frozenset()

    def test_provider_refleja_cambios_del_store_en_tiempo_real(self, tmp_path) -> None:
        """El provider lee del store en cada llamada — no captura un snapshot."""
        from hermes.shell.presentation.gtk4.approved_sites_store import (  # noqa: PLC0415
            approved_sites_provider,
        )
        from uuid import uuid4  # noqa: PLC0415

        store = _make_store(tmp_path)
        provider = approved_sites_provider(store)

        tid = uuid4()
        assert provider(tid) == frozenset()

        store.add("example.com")
        assert "example.com" in provider(tid)

        store.remove("example.com")
        assert provider(tid) == frozenset()


# ---------------------------------------------------------------------------
# (e) Regresión fail-closed con _host_is_approved
# ---------------------------------------------------------------------------

class TestFailClosedRegression:
    """Verifica que frozenset vacío → _host_is_approved devuelve False.

    Esta es la invariante central de Fix-5 / CTRL-5: sin approved_sites
    configurados, WRITE verbs deben ser denegados por el adapter.
    """

    def test_frozenset_vacio_deniega_cualquier_host(self) -> None:
        from hermes.agents_os.infrastructure.browser_surface_adapter import (  # noqa: PLC0415
            _host_is_approved,
        )

        assert _host_is_approved("example.com", frozenset()) is False
        assert _host_is_approved("google.com", frozenset()) is False
        assert _host_is_approved("", frozenset()) is False

    def test_store_vacio_produce_frozenset_que_deniega_write(self, tmp_path) -> None:
        """El adapter recibirá frozenset vacío si el store está vacío → fail-closed."""
        from hermes.agents_os.infrastructure.browser_surface_adapter import (  # noqa: PLC0415
            _host_is_approved,
        )
        from hermes.shell.presentation.gtk4.approved_sites_store import (  # noqa: PLC0415
            approved_sites_provider,
        )
        from uuid import uuid4  # noqa: PLC0415

        store = _make_store(tmp_path)
        provider = approved_sites_provider(store)
        approved = provider(uuid4())

        assert _host_is_approved("any-site.com", approved) is False

    def test_store_con_sitio_aprueba_host_exacto(self, tmp_path) -> None:
        from hermes.agents_os.infrastructure.browser_surface_adapter import (  # noqa: PLC0415
            _host_is_approved,
        )
        from hermes.shell.presentation.gtk4.approved_sites_store import (  # noqa: PLC0415
            approved_sites_provider,
        )
        from uuid import uuid4  # noqa: PLC0415

        store = _make_store(tmp_path)
        store.add("example.com")
        provider = approved_sites_provider(store)
        approved = provider(uuid4())

        assert _host_is_approved("example.com", approved) is True

    def test_store_con_sitio_aprueba_subdominio(self, tmp_path) -> None:
        from hermes.agents_os.infrastructure.browser_surface_adapter import (  # noqa: PLC0415
            _host_is_approved,
        )
        from hermes.shell.presentation.gtk4.approved_sites_store import (  # noqa: PLC0415
            approved_sites_provider,
        )
        from uuid import uuid4  # noqa: PLC0415

        store = _make_store(tmp_path)
        store.add("example.com")
        provider = approved_sites_provider(store)
        approved = provider(uuid4())

        assert _host_is_approved("api.example.com", approved) is True

    def test_store_con_sitio_deniega_host_diferente(self, tmp_path) -> None:
        from hermes.agents_os.infrastructure.browser_surface_adapter import (  # noqa: PLC0415
            _host_is_approved,
        )
        from hermes.shell.presentation.gtk4.approved_sites_store import (  # noqa: PLC0415
            approved_sites_provider,
        )
        from uuid import uuid4  # noqa: PLC0415

        store = _make_store(tmp_path)
        store.add("safe.com")
        provider = approved_sites_provider(store)
        approved = provider(uuid4())

        assert _host_is_approved("evil.com", approved) is False
