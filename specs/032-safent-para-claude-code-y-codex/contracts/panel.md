# Contrato — Panel web (Enterprise)

Repo: `lumen-control-enterprise`. Rutas nuevas en `src/safent_control/api/console.py`
(prefijo `/api`, misma sesión por cookie, mismo `_require(session, *roles)`, mismo
`_require_step_up_totp`, mismo `x-safent-org` que ya exige `deps._require_org_intent`).
DTOs nuevos en `frontend/src/services/api.ts`.

## 0. Lo que el panel YA cubre (no se rehace)

| Vista / capacidad | Ruta existente | DTO existente |
| --- | --- | --- |
| Sesión y organizaciones | `GET /api/me`, `POST /api/switch-org` | `MeResponse`, `OrgMembership` |
| Flota de instancias | `GET /api/instances`, `GET /api/instances/{id}`, `POST /api/instances/{id}/revoke` | `Instance` |
| Bandeja de aprobaciones | `GET /api/approvals`, `POST /api/approvals/{request_id}` | `PendingApproval` |
| Auditoría | `GET /api/audit`, `GET /api/audit/export.csv` | `AuditEntry`, `AuditFilters` |
| Licencias | `GET/POST/PATCH /api/licenses` | `License` |
| MFA | `POST /api/mfa/enroll`, `/verify`, `GET /api/mfa/status` | `MfaEnrollResponse`, `MfaStatusResponse` |
| Código de emparejamiento | `POST /api/agents/{agent_id}/mint-code` | `MintCodeResponse` |
| Cuentas de anuncios asignadas | `GET /api/ads/grants`, `POST /api/ads/grants`, `…/revoke` | — |
| Encargos a un puesto | `POST /api/delegations`, `GET /api/tasks` | `TeamTask`, `TeamAssignment` |

Consecuencia: **la bandeja de aprobaciones y la auditoría de la Fase 1 no necesitan endpoints
nuevos**; sólo necesitan que la respuesta sepa decir «esto viene de una instalación».

## 1. Instalaciones

### `GET /api/installations`
Rol: cualquiera (`_ANY_ROLE`). Parámetros: `employee_id?`, `status?` (`active|revoked`).
```ts
export type HarnessKind = 'claude-code' | 'codex'
export type InstallationStatus = 'active' | 'revoked'

export interface Installation {
  installation_id: string
  org_id: string
  user_id: string
  employee_id: string
  employee_name: string
  instance_id: string
  harness: HarnessKind
  harness_version: string
  label: string
  hostname: string
  status: InstallationStatus
  created_at: string
  last_seen_at: string | null
  revoked_at: string | null
  runtime_paired: boolean          // installation.runtime_paired_at != null
  policy_version: number           // instance.published_version
  tools_available: number
  tools_interactive: number
  pending_approvals: number
  ads_accounts: number             // ads_grant activos de su instance_id
}
export interface InstallationsResponse { installations: Installation[] }
```

### `GET /api/installations/{installation_id}`
Rol: `_ANY_ROLE`, con comprobación de propiedad por `org_id` (mismo patrón que
`_get_owned_instance`). Devuelve `Installation` más:
```ts
export interface InstallationDetail extends Installation {
  client_name: string              // oauth_client.client_name, ya escapado
  tools: InstallationTool[]
  recent_calls: McpCall[]          // últimas 50 de mcp_call_log
}
export interface InstallationTool {
  name: string
  tool_class: 'read' | 'interactive'
  decision: 'allow' | 'panel' | 'deny'
}
export interface McpCall {
  call_id: string
  ts: string
  tool_name: string
  tool_class: 'read' | 'interactive'
  decision: 'allow' | 'panel' | 'deny'
  outcome: 'ok' | 'pending' | 'denied' | 'error' | 'rate_limited'
  error_code: string
  duration_ms: number
  request_id: string | null
}
```

### `PATCH /api/installations/{installation_id}`
Rol: `_WRITE_ROLES`. Cuerpo `{ "label": "…" }` (1..80 caracteres, saneado). Sólo la etiqueta;
nada más es editable. Auditoría `installation.updated`.

### `POST /api/installations/{installation_id}/revoke`
Rol: `_OWNER_ONLY` + `_require_step_up_totp` (paridad exacta con `POST /api/instances/{id}/revoke`).
Cuerpo `{ "totp": "123456" }`. En una transacción: `installation.status='revoked'`,
`instance.state='revoked'`, todos los `oauth_token` de la instalación `revoked=1`, licencia
liberada. Auditoría `installation.revoked`. Respuesta `{ "ok": true }`.

### `POST /api/installations/{installation_id}/pairing-code`
Rol: `_WRITE_ROLES`. Emite el código para emparejar la **jaula local** contra la misma
instancia. Reutiliza `ProvisioningService.mint_code` con
`agent_template_id = instance.agent_template_id`, `license_id` = la ya asignada.
```ts
export interface InstallationPairingCode {
  code: string           // Crockford base32, 12 caracteres
  expires_at: string     // TTL 10 min
  command: string        // "safent pair XXXX-XXXX-XXXX"
}
```
Auditoría: `agent.mint_code` (constante existente), `target = installation_id`.

## 2. Bandeja de aprobaciones — sin rutas nuevas

`GET /api/approvals` y `POST /api/approvals/{request_id}` se mantienen. El DTO
`PendingApproval` gana tres campos **opcionales** (ausentes para las filas que empuja una
instancia de la app propia, presentes para las que crea la pasarela):
```ts
export interface PendingApproval {
  // … campos actuales sin cambios …
  origin?: 'harness' | 'runtime'
  installation_id?: string | null
  installation_label?: string | null
}
```
`origin` se deriva en el servidor de `instance.kind` (`'harness'` ⇒ `harness`), no de una
columna nueva en `remote_approval`.

La resolución sigue firmando el `DecisionEnvelope` Ed25519 existente
(`domain/approval.py`, canonicalización **pinchada**: no se toca). La pasarela lee el estado
por consulta a `remote_approval`, así que para la Fase 1 **no interviene el relé**
(`delegation_message`): ese camino es para el runtime local que hace *pull* de
`/v1/approvals/decisions`, y una instalación de arnés no puede autenticarse en `/v1/*`.
Cuando la instalación tenga jaula emparejada (`runtime_paired=true`), la jaula sí usa el
camino existente sin cambio alguno.

## 3. Feed de auditoría

`GET /api/audit` gana dos filtros opcionales: `installation_id`, `action_prefix` (p. ej.
`mcp.`). El DTO `AuditEntry` no cambia.

Nueva ruta para el detalle por instalación (no encadenada, alta frecuencia, retención 90 días):

### `GET /api/installations/{installation_id}/calls`
Rol: `_ANY_ROLE`. Parámetros: `since?`, `outcome?`, `limit` (≤ 200), `cursor?`.
```ts
export interface McpCallsResponse { calls: McpCall[]; next_cursor: string | null }
```
Nunca devuelve argumentos: sólo `arg_digest` cuando se pida explícitamente con `verbose=1`.

## 4. Alta personal e invitación

### `POST /api/signup` — sin sesión previa, tras el login por Google
Idempotente. Si la persona ya tiene alguna `membership`, devuelve `200` con lo que ya hay.
Si no, crea en una transacción: `tenant(personal=1, seat_limit=3, owner_user_id)`,
`membership(owner)`, `employee`, `agent_template` por defecto, `license(plan='personal')`.
```ts
export interface SignupResponse {
  org_id: string
  org_name: string          // "Espacio de <nombre>"
  personal: true
  employee_id: string
  license_id: string
}
```
Se invoca también, en el servidor, desde el paso 5 de `/oauth/authorize`: una persona que
instala sin haber pasado nunca por el panel **no ve un formulario**.

### `POST /api/orgs/{org_id}/invitations`
Rol: `_WRITE_ROLES` + `_require_step_up_totp`. Rechaza `403 personal_org_not_invitable` si
`tenant.personal=1`.
```ts
export interface InvitationCreate { email: string; role: 'admin' | 'viewer'; name: string; totp: string }
export interface Invitation {
  invitation_id: string
  org_id: string
  email: string
  role: 'admin' | 'viewer'
  employee_id: string
  created_at: string
  expires_at: string
  accepted_at: string | null
  status: 'pending' | 'accepted' | 'expired'
}
```
El enlace con el testigo en claro se envía por correo y **no** vuelve en la respuesta salvo en
`ENVIRONMENT=dev` (mismo criterio que `dev-login`). Auditoría `invitation.created`.

### `GET /api/orgs/{org_id}/invitations` · `DELETE /api/orgs/{org_id}/invitations/{id}`
Listar y revocar. `_WRITE_ROLES`. Auditoría `invitation.revoked`.

### `POST /api/invitations/accept`
Con sesión. Cuerpo `{ "token": "…" }`. Exige que el email verificado de la sesión sea igual al
invitado; crea `membership` y activa el `employee`; marca `accepted_at` con CAS. Respuesta:
`{ "org_id": "…", "role": "admin" }`. Auditoría `invitation.accepted`.

## 5. Anuncios en el panel

Sin contrato nuevo: la vista de Anuncios de Safent Cloud sirve el panel del companion
(`safent-ads/panel`) desde la nube, con la sesión del propio companion — el mismo material que
hoy sirve el puente local `hermes.shell_server.ads_bridge`, pero sin jaula por medio. El
enlace profundo que devuelve `safent_panel` apunta a esa vista.

## 6. Vistas del frontend (React)

| Vista | Ruta SPA | Fuente |
| --- | --- | --- |
| Instalaciones (lista) | `/installations` | `GET /api/installations` |
| Instalación (detalle + revocar + código de emparejamiento) | `/installations/:id` | `GET /api/installations/:id`, `…/calls` |
| Aprobaciones | `/approvals` (existe) | `GET /api/approvals` con la insignia `origin` |
| Auditoría | `/audit` (existe) | `GET /api/audit?installation_id=` |
| Invitaciones | `/team/invitations` | `GET/POST /api/orgs/:id/invitations` |
| Anuncios | `/ads/*` (existe) | panel del companion |

Estados obligatorios en cada vista nueva: cargando · vacío («Aún no has instalado Safent en
Claude Code ni en Codex», con el comando copiable) · error · sin permisos. Texto en castellano.
