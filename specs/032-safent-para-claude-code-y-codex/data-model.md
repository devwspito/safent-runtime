# 032 — Modelo de datos (Fase 1)

Fecha: 14-sep-2026. Ámbito: `spec.md` P1 + lo que P2/P3 necesitan que exista ya.
Regla dura de este documento: **no se inventa un modelo paralelo donde Enterprise ya
tiene uno**. Todo lo nuevo se cuelga de `tenant/employee/agent_template/license/instance`.

## 1. Lenguaje ubicuo

| Término (negocio) | Nombre en código / tabla | Definición |
| --- | --- | --- |
| Cuenta (persona) | `console_user` (`User`) | Persona identificada por email verificado. Ya existe. |
| Organización | `tenant` (`Tenant`) | Frontera de facturación y gobierno. `org_id == tenant_id`. Ya existe. |
| Organización personal | `tenant.personal = 1` | Organización de una sola persona, creada en el alta. **Nuevo campo.** |
| Pertenencia | `membership` (`Membership`) | (persona, organización, rol owner/admin/viewer). Ya existe. |
| Invitación | `org_invitation` | Oferta de puesto por correo, pendiente de que la persona entre. **Nueva tabla.** |
| Puesto | `employee` (`Employee`) | La persona dentro de la organización. Ya existe. |
| Agente del puesto | `agent_template` (`AgentTemplate`) | Definición + bloques de gobierno del puesto. Ya existe. |
| Licencia | `license` (`License`) | Derecho de asiento. Se asigna a una instancia. Ya existe. |
| Instancia | `instance` (`Instance`) | Runtime asociado a un puesto. Ya existe; gana `kind`. |
| **Instalación** | `installation` | Claude Code o Codex vinculado por OAuth a un puesto. **Nueva tabla.** |
| Arnés | `installation.harness` | `claude-code` \| `codex`. |
| Código de emparejamiento | `association_code` (`AssociationCode`) | Código de un solo uso para lo que no tiene navegador. Ya existe. |
| Cliente OAuth | `oauth_client` | Registro dinámico (RFC 7591) del arnés. **Nueva tabla.** |
| Concesión / Refresco | `oauth_token` | Credencial opaca ligada a una instalación. **Nueva tabla.** |
| Política publicada | `published_policy` | Paquete firmado por instancia + versión. Ya existe. **Es el PolicySnapshot.** |
| Aprobación | `remote_approval` (`RemoteApproval`) | Acción pendiente de decisión humana. Ya existe. |
| Decisión firmada | `DecisionEnvelope` (`domain/approval.py`) | Ed25519 del tenant. Ya existe, no se toca. |
| Entrada de auditoría | `audit_log` (`AuditEntry`) | Registro append-only encadenable. Ya existe. |
| Llamada de herramienta | `mcp_call_log` | Traza por llamada de la pasarela. **Nueva tabla.** |
| Concesión de cuenta de anuncios | `ads_grant` (`AdsGrant`) | (org, usuario, puesto, instancia, negocio, plataforma, cuenta). Ya existe. |

## 2. Contextos acotados

- **Identidad y Organización** (Enterprise `application/auth_service.py`, `provisioning.py`)
  — cuentas, organizaciones, pertenencias, invitaciones. Contrato hacia fuera: `ConsoleSession`
  y `(user_id, org_id, role)`.
- **Puestos y Licencias** (Enterprise `provisioning.py`, `pairing_service.py`) — empleado,
  plantilla, licencia, instancia, código. Contrato: `Instance` activa + `License` asignada.
- **Gobierno** (Enterprise `policy_compiler.py`, `approval_service.py`, `audit_service.py`)
  — compila política, resuelve aprobaciones, audita. Contrato: `published_policy` firmada y
  `DecisionEnvelope`.
- **Pasarela MCP** (Enterprise `mcp_gateway/`, NUEVO) — traduce `tools/list`/`tools/call` del
  arnés a llamadas al companion, con política y auditoría. **No contiene reglas de negocio de
  anuncios**: es un adaptador de presentación + un caso de uso de admisión.
- **Anuncios** (repo `safent-ads`, proceso aparte) — catálogo, propuestas, freno de gasto,
  reconciliación. Se comunica sólo por su `/mcp` con bearer de servicio + alcance firmado.
  **Capa anticorrupción**: `mcp_gateway/infrastructure/companion_client.py`; ningún tipo de
  `safent_ads` entra en el dominio de Enterprise.

Dependencias: Pasarela → Gobierno → Puestos → Identidad. Anuncios no depende de ninguno
(recibe alcance en la llamada). Sin ciclos.

## 3. Agregados

### Account (`console_user`) — EXISTE, sin cambios de forma
Invariantes ya vigentes que la Fase 1 hereda: email único y verificado; `is_service_account`
nunca puede auto-inscribir MFA; `mfa_last_counter` monótono (anti-replay del paso a paso).
Nuevo uso: una Account sin ninguna `membership` **no puede consentir** un `/oauth/authorize`;
el alta personal (§5) se lo resuelve antes de enseñar la pantalla de consentimiento.

### Organization (`tenant`) — EXISTE, +2 columnas
- Nuevas: `personal INTEGER NOT NULL DEFAULT 0`, `owner_user_id TEXT NOT NULL DEFAULT ''`.
- **Invariantes**:
  1. `personal=1` ⇒ existe exactamente una `membership` con rol `owner` y `user_id = owner_user_id`;
     `POST /api/orgs/{id}/invitations` se rechaza (403 `personal_org_not_invitable`).
  2. `personal=1` ⇒ `seat_limit = 3` (tres instalaciones: Claude Code, Codex, app propia).
  3. Una persona tiene **como mucho una** organización personal (índice único parcial sobre
     `owner_user_id` cuando `personal=1`).
- Estado: `billing_status` (`trialing|active|past_due|canceled`) ya gobierna la publicación.

### Membership / OrgInvitation
`org_invitation`: `invitation_id`, `org_id`, `email`, `role`, `employee_id`, `license_id`,
`token_hash`, `created_by_user_id`, `created_at`, `expires_at`, `accepted_at`, `accepted_user_id`.
- **Invariantes**: el token en claro se enseña una vez (correo) y sólo se guarda su SHA-256
  (mismo primitivo que `hash_secret`); TTL ≤ 7 días; un solo consumo (`accepted_at` CAS);
  la aceptación exige que el email verificado de la sesión sea **igual** al `email` invitado;
  aceptar crea `membership` y **no** crea organización personal nueva.

### Installation — NUEVO AGREGADO (el corazón de esta spec)
```
installation_id        TEXT PK           uuid4
org_id                 TEXT NOT NULL
user_id                TEXT NOT NULL     -> console_user
employee_id            TEXT NOT NULL     -> employee (el puesto)
instance_id            TEXT NOT NULL UNIQUE -> instance (kind='harness')
client_id              TEXT NOT NULL     -> oauth_client
harness                TEXT NOT NULL     'claude-code' | 'codex'
harness_version        TEXT NOT NULL DEFAULT ''
label                  TEXT NOT NULL     "Claude Code en el MacBook de Luis"
hostname               TEXT NOT NULL DEFAULT ''
created_at             TEXT NOT NULL
last_seen_at           TEXT
status                 TEXT NOT NULL DEFAULT 'active'   'active' | 'revoked'
revoked_at             TEXT
revoked_by_user_id     TEXT
runtime_paired_at      TEXT              != NULL  <=>  hay jaula local emparejada
```
Índices: `installation_org_idx(org_id, status)`, `installation_user_idx(user_id)`,
`installation_instance_uq UNIQUE(instance_id)`.

- **Invariantes**:
  1. **1:1 con `instance`.** Cada instalación materializa una fila `instance` con
     `kind='harness'`, `agent_template_id` = la plantilla del puesto, `state='active'`,
     `hardware_fingerprint = 'harness:' || installation_id`, `instance_secret_hash = NULL`.
     Consecuencia de seguridad buscada: `_authenticate_instance*` (api/app.py) **nunca** puede
     autenticar una instalación de arnés — `/v1/policy`, `/v1/metering`, `/v1/outbox`,
     `/v1/mcp-token` quedan estructuralmente cerrados a un token OAuth.
  2. **Una instalación = un asiento.** Crearla reserva una `license` igual que el
     emparejamiento (mismo camino de `SeatLimitError`, 402). Revocar la licencia o poner
     `instance.state='revoked'` corta la instalación en la siguiente llamada.
  3. `status='revoked'` ⇒ todos sus `oauth_token` quedan `revoked=1` en la misma transacción.
  4. `org_id` es **derivado** de `employee.org_id`; nunca viene del cliente.
  5. La jaula local se empareja **contra la misma instancia**: `safent pair <código>` ejecuta
     `/v1/associate`+`/v1/proof` sobre `installation.instance_id`, fija
     `instance_secret_hash` y `hardware_fingerprint` reales y sella `runtime_paired_at`.
     Un puesto = una instancia = un asiento = una versión de política = un hilo de auditoría,
     tenga jaula o no.
- **Ciclo de vida**: `(no existe)` → *primer `/oauth/authorize` consentido* → `active`
  → *panel `POST /api/installations/{id}/revoke`* → `revoked` (terminal; un nuevo login crea
  otra instalación con otro id).
- **Eventos**: `InstalacionCreada`, `InstalacionRevocada`, `JaulaLocalEmparejada`.

### OAuthClient (`oauth_client`) — NUEVO
```
client_id                    TEXT PK       'sfc_' || 32 hex
client_id_issued_at          INTEGER NOT NULL
client_name                  TEXT NOT NULL
redirect_uris                TEXT NOT NULL   JSON array
grant_types                  TEXT NOT NULL DEFAULT '["authorization_code","refresh_token"]'
response_types               TEXT NOT NULL DEFAULT '["code"]'
token_endpoint_auth_method   TEXT NOT NULL DEFAULT 'none'
scope                        TEXT NOT NULL DEFAULT 'safent.tools safent.status'
software_id                  TEXT NOT NULL DEFAULT ''
software_version             TEXT NOT NULL DEFAULT ''
registration_access_token_hash TEXT NOT NULL DEFAULT ''
created_at                   TEXT NOT NULL
created_ip                   TEXT NOT NULL DEFAULT ''
status                       TEXT NOT NULL DEFAULT 'active'
```
- **Invariantes**: sólo clientes **públicos** (`token_endpoint_auth_method='none'`), por tanto
  PKCE `S256` obligatorio; `redirect_uris` ⊆ {`http://127.0.0.1:<puerto>/...`,
  `http://localhost:<puerto>/...`, esquema privado `claude://`/`codex://`} — nada de `http://`
  hacia una IP pública ni comodines; un cliente **no confiere autoridad**: la autoridad
  la da el consentimiento de la persona, el cliente sólo identifica al arnés.

### Grant / RefreshToken (`oauth_token`) — NUEVO
```
token_id        TEXT PK
token_hash      TEXT NOT NULL UNIQUE      SHA-256 del valor opaco (hash_secret)
kind            TEXT NOT NULL             'access' | 'refresh'
installation_id TEXT NOT NULL
user_id         TEXT NOT NULL
org_id          TEXT NOT NULL
client_id       TEXT NOT NULL
scope           TEXT NOT NULL
resource        TEXT NOT NULL             RFC 8707: 'https://mcp.safent.app/mcp'
issued_at       TEXT NOT NULL
expires_at      TEXT NOT NULL
revoked         INTEGER NOT NULL DEFAULT 0
rotated_from    TEXT                      cadena de rotación del refresco
reuse_detected_at TEXT
```
- **Invariantes**: el valor en claro se devuelve una vez y nunca se persiste (mismo patrón que
  `service_account_token`); `access` TTL 3600 s, `refresh` TTL 30 días con **rotación en cada
  uso**; reutilizar un refresco ya rotado marca `reuse_detected_at` y **revoca toda la cadena
  y la instalación** (detección de robo); la validación es una consulta a BD en cada petición,
  así que revocar en el panel corta en la llamada siguiente sin esperar al vencimiento.

### PolicySnapshot — **NO ES UNA TABLA NUEVA**
Es la fila `published_policy(instance_id, version)` de la instancia de la instalación.
La pasarela compila en memoria un `GatewayPolicy` a partir del `payload_json` publicado:
`access_scope.policy_overlay[tool] = {enabled, approval: 'auto'|'hitl'}` (ya existe, ya viaja
firmado) más `license.remote_approval_enabled`. Decisión efectiva por herramienta:
`deny` (no habilitada) · `panel` (`approval='hitl'`) · `allow` (`approval='auto'`).
- **Invariantes**: la pasarela **nunca** decide con un overlay que no venga de un
  `published_policy` verificado con la clave pública del tenant; si no hay política publicada
  o la firma no valida ⇒ **deny** (fail-closed, Constitución IV).

### Approval — **REUSA `remote_approval`**
La pasarela escribe filas con `instance_id = installation.instance_id`,
`agent_id = agent_template_id`, `tool_name` = nombre de la herramienta del companion,
`params_redacted`, `action_digest`, `risk='high'`, `sensitivity=['spend']`.
- **Invariantes nuevas**: `request_id` fresco por llamada (ya es su contrato);
  `acked` es el **pestillo de ejecución única**: la pasarela sólo ejecuta la herramienta al
  pasar `acked` 0→1 con un UPDATE condicional; `expires_at` = `created_at` + 30 min para las
  creadas por la pasarela (el modelo sigue en el chat, no puede esperar 72 h).

### AuditEntry — **REUSA `audit_log`**, nuevas acciones
`installation.created`, `installation.revoked`, `installation.runtime_paired`,
`oauth.client_registered`, `oauth.consent_granted`, `oauth.token_revoked`,
`mcp.tool_write`, `mcp.tool_denied`, `mcp.tool_pending`.
`target` = `installation_id`; `actor_ref` = `installation:<id>` para lo iniciado por el arnés,
`user:<id>` para lo iniciado en el panel.
- **Invariante de volumen**: `audit_log` lleva `seq`/`prev_hash`/`entry_hash` (cadena por
  organización): serializa las escrituras. Por eso **las lecturas no entran aquí**.

### McpCall (`mcp_call_log`) — NUEVA, no encadenada
```
call_id         TEXT PK
installation_id TEXT NOT NULL
org_id          TEXT NOT NULL
ts              TEXT NOT NULL
tool_name       TEXT NOT NULL
tool_class      TEXT NOT NULL     'read' | 'interactive'
decision        TEXT NOT NULL     'allow' | 'panel' | 'deny'
outcome         TEXT NOT NULL     'ok' | 'pending' | 'denied' | 'error' | 'rate_limited'
error_code      TEXT NOT NULL DEFAULT ''
arg_digest      TEXT NOT NULL     SHA-256 de los argumentos canónicos (nunca los argumentos)
duration_ms     INTEGER NOT NULL DEFAULT 0
request_id      TEXT              -> remote_approval.request_id cuando outcome='pending'
```
Índices: `mcp_call_install_ts_idx(installation_id, ts)`, `mcp_call_org_ts_idx(org_id, ts)`.
- **Invariantes**: nunca guarda argumentos en claro (sólo el digest, que es el mismo
  `action_digest` que firma la decisión); barrido de retención a 90 días; una fila por
  `tools/call`, siempre, incluso si la política denegó antes de salir hacia el companion.

## 4. Eventos de dominio

- **`InstalacionCreada`** — emitido por `Installation` al consentir el primer `/oauth/authorize`.
  Payload: `installation_id, org_id, user_id, employee_id, instance_id, harness, label`.
- **`InstalacionRevocada`** — emitido al revocar desde el panel o al detectar reuso de refresco.
  Payload: `installation_id, org_id, revoked_by_user_id, reason`.
- **`LlamadaDeHerramientaAdmitida` / `…Denegada` / `…Pendiente`** — emitido por la pasarela tras
  evaluar `GatewayPolicy`. Payload: `installation_id, tool_name, decision, arg_digest[, request_id]`.
- **`AprobacionResuelta`** — ya existe (`approval.resolved`), lo emite el panel; la pasarela lo
  observa por consulta a `remote_approval`, no por bus.

## 5. Relaciones

```
console_user 1─n membership n─1 tenant
tenant 1─n employee 1─n agent_template 1─n instance 1─1 installation
tenant 1─n license           license 0..1─1 instance   (asiento)
installation 1─n oauth_token        oauth_client 1─n installation
instance 1─n published_policy (versión monótona)
instance 1─n remote_approval        installation 1─n mcp_call_log
instance 1─n ads_grant  (cuenta de anuncios asignada al puesto)
tenant 1─n org_invitation
```

## 6. Plan de migración (entrega a `database-engineer`)

Mecánica existente: `_SCHEMA` con `CREATE TABLE IF NOT EXISTS` + guardas `ALTER TABLE`
idempotentes en `ControlPlaneRepository.__init__`, espejadas en `_run_schema` de
`repository_postgres.py`. Se sigue tal cual — **expandir, nunca contraer**.

| # | Cambio | SQLite | Postgres | Riesgo |
| --- | --- | --- | --- | --- |
| M1 | `instance` + `kind TEXT NOT NULL DEFAULT 'app'` | guarda `PRAGMA table_info` | `ADD COLUMN IF NOT EXISTS` | nulo (default retrocompatible) |
| M2 | `tenant` + `personal`, `owner_user_id` | ídem | ídem | nulo |
| M3 | Tablas nuevas: `installation`, `oauth_client`, `oauth_authorization_code`, `oauth_token`, `org_invitation`, `mcp_call_log` | en `_SCHEMA` | en `_run_schema` | nulo (aditivo) |
| M4 | Índice único parcial `tenant_personal_owner_uq ON tenant(owner_user_id) WHERE personal=1` | soportado | soportado | requiere backfill vacío (no hay filas `personal=1` todavía) |
| M5 | Barrido de retención `mcp_call_log` (90 d) y `oauth_authorization_code` (60 s) | tarea periódica del proceso | ídem | operar antes de que crezca |
| M6 | Constantes de acción nuevas en `domain/audit.py` | — | — | nulo |

Nada de esto toca filas existentes. No hay contracción en Fase 1: `instance.kind` se queda
con default `'app'` y ninguna consulta actual filtra por él.

## 7. Lo que este modelo obliga a cambiar del plan

1. **`/mcp` ya está ocupado.** `api/app.py:201-203` monta la superficie MCP del Cerebro en
   `/mcp` con `FastApiMCP`. La pasarela del arnés necesita ese path público
   (`https://mcp.safent.app/mcp`). Cambio: la superficie del Cerebro pasa a `/mcp/cerebro`
   (un valor de configuración en el puente del Cerebro + republicar los `mcp` de las
   plantillas que la apunten) y la pasarela se registra como **`Route("/mcp")` exacta**, jamás
   `Mount` (`Mount("/mcp")` no casa el path pelado → 307 y el cliente MCP se queda esperando;
   el propio companion ya lo documenta en `mcp/presentation/http.py`).
2. **El companion es de un solo propietario.** `SqlOwnerBridgeRepository` mantiene como mucho
   una fila en `owners` y `StaticCallerScopeResolver` devuelve alcance sin restricción. Hasta
   cerrar ADS-02, **un despliegue de companion por organización** y el alcance por negocio se
   impone en la pasarela con el `ads_grant` de la instalación, no dentro del companion.
3. **`ads_grant` se lleva a la instalación gratis** porque su clave es `instance_id` — que es
   justo la razón de que la instalación materialice una instancia en vez de ser un principal
   nuevo. Lo mismo vale para `remote_approval`, `delegation_message` y `published_policy`.
4. **Asientos**: una instalación consume una licencia. Ver pregunta abierta 1 del bloque de
   diseño de `plan.md`.
