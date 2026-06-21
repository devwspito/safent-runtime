"""ApprovedSitesStore — persistencia de la lista de sitios aprobados para el navegador.

El agente solo puede realizar acciones de escritura (pulsar, escribir) en los
sitios que el usuario haya aprobado explícitamente. Sin sitios aprobados el
agente NO actúa en webs (comportamiento fail-closed).

La lista se almacena en JSON junto al resto de configuración de la shell
(~/.config/hermes-shell/hermes-approved-sites.json).

Formato en disco:
    {
        "sites": ["example.com", "app.company.com"]
    }

Invariantes:
  - Dominio vacío, con espacios o con esquema (http://) → rechazado en add().
  - La lista nunca contiene duplicados (case-insensitive), espacios ni esquemas.
  - El archivo corruptuo o inexistente → lista vacía (fail-closed: sin permisos).
  - Errores de E/S en save() → log de aviso, sin lanzar excepción.

Sin dependencias de GTK — testeable en cualquier entorno.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_PERSIST_FILENAME = "hermes-approved-sites.json"

# Expresión regular simple para hostname válido (sin esquema, sin path).
# Acepta: example.com, api.example.com, localhost, 192.168.1.1
_HOSTNAME_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$"
)


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / "hermes-shell"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_domain(raw: str) -> str | None:
    """Devuelve el hostname normalizado (lowercase, sin esquema, sin path ni slash).

    Devuelve None si el valor no es un hostname válido.
    """
    candidate = raw.strip().lower()
    # Rechazar si lleva esquema (http://, https://).
    if "://" in candidate:
        return None
    # Rechazar si lleva path (/).
    if "/" in candidate:
        return None
    if not candidate:
        return None
    if not _HOSTNAME_RE.match(candidate):
        return None
    return candidate


class ApprovedSitesStore:
    """Lectura/escritura de la lista de sitios aprobados para el navegador.

    Diseñado para instanciarse una vez en la shell y pasarse al adapter
    como proveedor. Solo se usa desde el hilo GTK y desde los tests de lógica pura.
    """

    def __init__(self) -> None:
        self._sites: list[str] = []
        self._load()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    @property
    def sites(self) -> list[str]:
        """Lista de dominios aprobados (snapshot, ordenada)."""
        return list(self._sites)

    def add(self, raw_domain: str) -> bool:
        """Añade un dominio a la lista.

        Devuelve True si se añadió, False si ya existía o era inválido.
        """
        normalized = _normalize_domain(raw_domain)
        if normalized is None:
            logger.warning("dominio inválido rechazado: %r", raw_domain)
            return False
        if normalized in self._sites:
            return False
        self._sites.append(normalized)
        self._sites.sort()
        self.save()
        return True

    def remove(self, domain: str) -> bool:
        """Elimina un dominio de la lista.

        Devuelve True si se eliminó, False si no estaba en la lista.
        """
        normalized = domain.strip().lower()
        if normalized not in self._sites:
            return False
        self._sites.remove(normalized)
        self.save()
        return True

    def as_frozenset(self) -> frozenset[str]:
        """Devuelve los sitios aprobados como frozenset para el adapter.

        Frozenset vacío → fail-closed (el adapter deniega WRITE).
        """
        return frozenset(self._sites)

    def save(self) -> None:
        """Persiste el estado actual en disco. Silencia errores de E/S."""
        path = _config_dir() / _PERSIST_FILENAME
        try:
            path.write_text(
                json.dumps({"sites": self._sites}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("no se pudo persistir approved-sites: %s", exc)

    # ------------------------------------------------------------------
    # Implementación interna
    # ------------------------------------------------------------------

    def _load(self) -> None:
        path = _config_dir() / _PERSIST_FILENAME
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_sites = data.get("sites", [])
            if not isinstance(raw_sites, list):
                logger.warning("approved-sites: campo 'sites' no es lista, ignorando")
                return
            valid: list[str] = []
            for entry in raw_sites:
                if not isinstance(entry, str):
                    continue
                normalized = _normalize_domain(entry)
                if normalized and normalized not in valid:
                    valid.append(normalized)
            self._sites = sorted(valid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("approved-sites corruptos, usando lista vacía: %s", exc)


def approved_sites_provider(store: ApprovedSitesStore):
    """Devuelve un ApprovedSitesProvider que lee del store en cada llamada.

    Se inyecta en BrowserSurfaceAdapter como:
        adapter = BrowserSurfaceAdapter(
            factory=...,
            registry=...,
            approved_sites=approved_sites_provider(store),
        )

    El proveedor es per-tenant-agnostic en la instalación de escritorio
    (un único usuario, sin multi-tenant). Se ignora tenant_id.
    """
    from uuid import UUID  # noqa: PLC0415

    def _provider(_tenant_id: UUID) -> frozenset[str]:
        return store.as_frozenset()

    return _provider
