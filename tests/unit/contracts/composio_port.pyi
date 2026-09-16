"""Contrato del paquete compartido `safent_composio` y de los puertos de Enterprise.

UNA implementación, nunca copiada (Decisión 1 de research.md). Este fichero es la
superficie pública acordada: lo que hoy vive en
`hermes.integrations.composio.composio_client` se extrae tal cual a `safent_composio`
y lo consumen los DOS productos.

Reglas del contrato:
  - El paquete es PURO: sin FastAPI, sin SQLite, sin vault, sin D-Bus, sin logging
    de la clave. Recibe la clave ya descifrada y no la persiste.
  - Ningún objeto del SDK de Composio cruza esta frontera: sólo los VOs de aquí.
  - Los errores del proveedor se normalizan a ComposioApiError con detalle TRUNCADO;
    nunca se reenvía la respuesta cruda a una capa superior.
  - La clasificación OAuth (`managed_auth_available` / `oauth_simple`) vive AQUÍ y sólo
    aquí: si divergiera, Enterprise y Community mostrarían catálogos distintos.
  - **El transporte HTTP es INYECTABLE (REQ-07)**: el cliente no fabrica su propio stack
    de red a la fuerza. Ver `ComposioClient.__init__`.
  - **La normalización de la entrada del proveedor vive en el ADAPTADOR de cada producto
    (REQ-19)**, no en la capa de aplicación ni en la UI. Reglas obligatorias, aplicadas
    ANTES de que nada suba del adaptador:
      * `slug`   → `^[a-z0-9_-]{1,64}$`; el elemento que no case se DESCARTA (no se
                   propaga «tal cual, ya lo pintará alguien»).
      * `name`   → ≤ 120 caracteres, texto plano, sin caracteres de control.
      * `description` → ≤ 200 caracteres, texto plano, una frase; jamás HTML ni URL
                   ejecutable; prohibido `dangerouslySetInnerHTML` en las vistas.
      * `connected_account_id` → `^[A-Za-z0-9_-]{1,200}$`.
      * `redirect_url` / `authorization_url` → `https`, host en la lista blanca del
                   proveedor, sin credenciales en la autoridad, sin caracteres de
                   control. Lo que no pase se rechaza con código cerrado y no se guarda
                   ni se muestra.
      * Si la respuesta del proveedor contiene la clave (eco), es ERROR, no traza
        (mismo control que `crm_transport.py:89`).
  - **Ningún log del secreto, en ninguno de los dos productos.** `ComposioApiError.detail`
    va truncado y sólo se registra en `debug`.

Cada repo mantiene un test de contrato que importa exactamente estos nombres.
"""

from dataclasses import dataclass
from typing import Any, Protocol

# ── Value objects (safent_composio) ─────────────────────────────────────────
# Todos con `kw_only=True`: se construyen SIEMPRE por palabra clave (añadir o reordenar un
# campo nunca desalinea a un llamador posicional en ninguno de los dos productos).

@dataclass(frozen=True, slots=True, kw_only=True)
class ToolkitInfo:
    slug: str
    name: str
    description: str
    oauth_simple: bool
    managed_auth_available: bool | None  # None == el proveedor no lo declara
    setup_required: bool
    auth_schemes: tuple[str, ...]  # tal cual las declara el proveedor; sólo informativo

@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectedAccountInfo:
    id: str
    toolkit_slug: str
    entity_id: str
    status: str
    auth_config_id: str

@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionInitResult:
    connected_account_id: str
    redirect_url: str
    status: str

@dataclass(frozen=True, slots=True, kw_only=True)
class AuthConfigInfo:
    id: str
    toolkit_slug: str
    status: str  # estado que informa el proveedor; la lectura en vivo manda, no este campo

# Los VOs se construyen SIEMPRE por palabra clave y el paquete puede ser un SUPERCONJUNTO
# de este contrato (campos o métodos extra que sólo usa el runtime, p. ej. `ToolInfo`,
# `list_tools`, `execute_action`). El test de contrato de cada repo comprueba INCLUSIÓN
# (todo lo de aquí existe con estos nombres, tipos y orden), no igualdad exacta.

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
        transport: Any | None = None,
    ) -> None:
        """`transport` es un `httpx.AsyncBaseTransport` INYECTADO (REQ-07).

        Sólo de palabra clave y con `None` por defecto: sin él, el cliente se comporta
        exactamente como hoy (stack del SDK), así que el runtime no cambia de conducta al
        extraer el paquete. Con él, TODO el tráfico del cliente sale por ese borde.

        Enterprise inyecta SIEMPRE su borde HTTPS pineado —el ÚNICO del repo, promovido a
        `infrastructure/http_boundary.py` (REQ-21)—: host fijo `https`, `trust_env=False`
        (ningún proxy de entorno), `follow_redirects=False`, DNS resuelto y validado con
        el mismo `_public_ip` (rechaza privadas, loopback, enlace-local, IPv4 mapeada en
        IPv6 y prefijos de transición NAT64/6to4/Teredo), conexión pineada a la dirección
        validada, tope de respuesta y tiempo de espera duro.

        PROHIBIDO crear un segundo validador de destino: la regla permanente es «una
        implementación, nunca copiada» — tres copias del guard compartían el mismo agujero
        de IPv4 mapeada. Hay un test que falla si aparece una segunda (REQ-21).
        """
        ...
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
# `RevokeOutcome` es un tipo del PUERTO (vive en `application/ports.py` de Enterprise),
# no del paquete: el cliente compartido devuelve None en `delete_connection` y es el
# adaptador quien traduce «404/410 del proveedor» a `already_absent`.

@dataclass(frozen=True, slots=True, kw_only=True)
class RevokeOutcome:
    """`deleted` = lo borramos nosotros ahora. `already_absent` = el proveedor dijo que ya
    no existía (404/410) y eso cuenta como ÉXITO (REQ-11)."""

    result: str  # "deleted" | "already_absent"

class ConnectionCatalogPort(Protocol):
    async def fetch_catalog(self, *, secret: str, search: str | None, limit: int) -> list[ToolkitInfo]:
        """Lectura EN VIVO del catálogo. Lanza ProviderUnreachable / KeyRejected."""
        ...

class ConnectionAuthorizationPort(Protocol):
    async def begin(self, *, secret: str, toolkit: str, entity_id: str) -> ConnectionInitResult: ...
    async def read(self, *, secret: str, entity_id: str, connected_account_id: str) -> ConnectedAccountInfo: ...
    async def list_for_entity(self, *, secret: str, entity_id: str) -> list[ConnectedAccountInfo]: ...
    async def revoke(self, *, secret: str, entity_id: str, connected_account_id: str) -> RevokeOutcome:
        """Debe CONFIRMAR la revocación en el proveedor; si no la confirma, levanta.

        IDEMPOTENTE (REQ-11): un `404`/`410` del proveedor significa «ya no existe», y eso
        ES la confirmación que buscábamos → devuelve `already_absent`, no levanta. Sólo el
        error de red o un `5xx` levantan `ProviderUnreachable`, y entonces el estado local
        se deja INTACTO para reintentar: nunca se marca `revoked` sin confirmación, ni se
        vuelve a `active` sin lectura fresca.
        """
        ...

class OrgKeyRevocationPort(Protocol):
    async def revoke_key(self, *, secret: str) -> None:
        """Cierra en el proveedor una clave que dejamos de usar (REQ-06, rotación).

        La confirmación NO es la respuesta del proveedor —su endpoint de revocación
        responde igual haya coincidido o no, por diseño anti-oráculo—: la confirmación es
        que una lectura EN VIVO posterior con esa misma clave FALLE. Quien orqueste la
        rotación hace las dos cosas y, si la lectura sigue funcionando, deja
        `previous_key_state = still_valid` y lo dice en pantalla.
        """
        ...

class EntityIdMinter(Protocol):
    def mint(self) -> str:
        """Identidad opaca por puesto, no reutilizable (`sf-<uuid4>`).

        CSPRNG obligatorio (REQ-22): `uuid.uuid4()` o `secrets`. PROHIBIDO derivarla de
        `employee_id`, del correo o de cualquier otro dato de la persona: sería PII estable
        y reactivar a alguien heredaría autorizaciones que se le habían retirado. Tests de
        formato, de unicidad y de no-reutilización tras revocar (`plan.md §14.1`).

        AVISO honesto de alcance: la opacidad NO aisla frente al proveedor. Con la clave de
        proyecto se pueden ENUMERAR todas las cuentas conectadas sin conocer ninguna
        identidad (research.md Decisión 12). El `entity_id` da atribución, revocación por
        puesto y no-reutilización — no confidencialidad.
        """
        ...
