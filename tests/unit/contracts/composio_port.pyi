"""Contrato del paquete compartido `safent_composio` (spec 002, Enterprise).

UNA implementación, nunca copiada (Decisión 1 de research.md). Copia local de
`enterprise-conexiones/specs/002-conexiones-e-integraciones/contracts/composio_port.pyi`,
mantenida a mano en lockstep con ese fichero — cada repo guarda su propia copia
porque no se garantiza tener el otro repo a mano en CI. `test_composio_port_contract.py`
falla si `safent_composio` se desvía de esto.

Reglas del contrato:
  - El paquete es PURO: sin FastAPI, sin SQLite, sin vault, sin D-Bus, sin logging
    de la clave. Recibe la clave ya descifrada y no la persiste.
  - Ningún objeto del SDK de Composio cruza esta frontera: sólo los VOs de aquí.
  - Los errores del proveedor se normalizan a ComposioApiError con detalle TRUNCADO;
    nunca se reenvía la respuesta cruda a una capa superior.
  - La clasificación OAuth (`managed_auth_available` / `oauth_simple`) vive AQUÍ y sólo
    aquí: si divergiera, Enterprise y Community mostrarían catálogos distintos.
  - `transport` (REQ-07): boundary anti-SSRF de Enterprise. Cuando se inyecta,
    TODAS las llamadas lo usan; nunca se leen variables de entorno de proxy
    (`trust_env=False`) y nunca se siguen redirecciones. `None` conserva el
    comportamiento por defecto de hoy (cliente httpx propio del SDK).
  - `delete_connection` (REQ-11) es IDEMPOTENTE: un 404/410 del proveedor
    (ya no existe) se trata como revocación exitosa, nunca como error.

Cada repo mantiene un test de contrato que importa exactamente estos nombres.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

# ── Value objects (safent_composio) ─────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ToolkitInfo:
    slug: str
    name: str
    description: str
    oauth_simple: bool
    managed_auth_available: bool | None  # None == el proveedor no lo declara
    setup_required: bool

@dataclass(frozen=True, slots=True)
class ConnectedAccountInfo:
    id: str
    toolkit_slug: str
    entity_id: str
    status: str
    auth_config_id: str

@dataclass(frozen=True, slots=True)
class ConnectionInitResult:
    connected_account_id: str
    redirect_url: str
    status: str

@dataclass(frozen=True, slots=True)
class AuthConfigInfo:
    id: str
    toolkit_slug: str

class ComposioApiError(Exception):
    status_code: int
    detail: str  # truncado; jamás contiene la clave

# ── Cliente compartido (safent_composio.ComposioClient) ─────────────────────

class ComposioClient:
    def __init__(
        self,
        *,
        api_key: str,
        auth_config_ids: dict[str, str] | None = ...,
        transport: httpx.BaseTransport | None = ...,
    ) -> None: ...
    async def list_toolkits(self, *, search: str | None = ..., limit: int = ...) -> list[ToolkitInfo]: ...
    async def assert_oauth_simple(self, toolkit_slug: str) -> None: ...
    async def initiate_connection(
        self, *, toolkit_slug: str, entity_id: str, redirect_url: str | None = ...
    ) -> ConnectionInitResult: ...
    async def get_connected_account(self, connection_id: str, *, entity_id: str) -> ConnectedAccountInfo: ...
    async def list_connected_accounts(self, entity_id: str) -> list[ConnectedAccountInfo]: ...
    async def delete_connection(self, connection_id: str, *, entity_id: str) -> None: ...
    async def validate_auth_config(self, toolkit_slug: str, config_id: str) -> AuthConfigInfo: ...

# ── Puertos de la capa de aplicación de Enterprise ──────────────────────────
# La aplicación depende de ESTOS protocolos, nunca de ComposioClient. El adaptador
# (infrastructure/composio_transport.py) es quien conoce el paquete compartido.

class ConnectionCatalogPort(Protocol):
    async def fetch_catalog(self, *, secret: str, search: str | None, limit: int) -> list[ToolkitInfo]:
        """Lectura EN VIVO del catálogo. Lanza ProviderUnreachable / KeyRejected."""
        ...

class ConnectionAuthorizationPort(Protocol):
    async def begin(self, *, secret: str, toolkit: str, entity_id: str) -> ConnectionInitResult: ...
    async def read(self, *, secret: str, entity_id: str, connected_account_id: str) -> ConnectedAccountInfo: ...
    async def list_for_entity(self, *, secret: str, entity_id: str) -> list[ConnectedAccountInfo]: ...
    async def revoke(self, *, secret: str, entity_id: str, connected_account_id: str) -> None:
        """Debe CONFIRMAR la revocación en el proveedor; si no la confirma, levanta."""
        ...

class EntityIdMinter(Protocol):
    def mint(self) -> str:
        """Identidad opaca por puesto, no reutilizable (`sf-<uuid4>`)."""
        ...
