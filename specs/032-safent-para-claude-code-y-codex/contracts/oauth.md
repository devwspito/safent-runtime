# Contrato — Servidor de autorización de Safent Cloud

Repo: `lumen-control-enterprise` · Módulo nuevo: `src/safent_control/oauth/`
(`domain/`, `application/`, `api/oauth.py`). Emisor: `https://mcp.safent.app`.
Recurso protegido: `https://mcp.safent.app/mcp`.
Perfil: **OAuth 2.1** — `authorization_code` + PKCE `S256` obligatorio, clientes públicos,
RFC 8414 (metadatos del AS), RFC 9728 (metadatos del recurso), RFC 7591 (registro dinámico),
RFC 8707 (`resource`), RFC 7009 (revocación).

Todas las respuestas llevan `Cache-Control: no-store` salvo los dos documentos de metadatos
(`Cache-Control: public, max-age=3600`).

---

## 1. `GET /.well-known/oauth-protected-resource/mcp`

Alias aceptado: `GET /.well-known/oauth-protected-resource`.

```json
{
  "resource": "https://mcp.safent.app/mcp",
  "authorization_servers": ["https://mcp.safent.app"],
  "scopes_supported": ["safent.tools", "safent.status"],
  "bearer_methods_supported": ["header"],
  "resource_name": "Safent",
  "resource_documentation": "https://safent.app/docs/mcp"
}
```

El `401` de `/mcp` devuelve siempre:
`WWW-Authenticate: Bearer resource_metadata="https://mcp.safent.app/.well-known/oauth-protected-resource/mcp", error="invalid_token"`.

## 2. `GET /.well-known/oauth-authorization-server`

```json
{
  "issuer": "https://mcp.safent.app",
  "authorization_endpoint": "https://mcp.safent.app/oauth/authorize",
  "token_endpoint": "https://mcp.safent.app/oauth/token",
  "registration_endpoint": "https://mcp.safent.app/oauth/register",
  "revocation_endpoint": "https://mcp.safent.app/oauth/revoke",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "scopes_supported": ["safent.tools", "safent.status"],
  "service_documentation": "https://safent.app/docs/mcp"
}
```

`code_challenge_methods_supported` **no incluye `plain`**: un cliente que pida `plain` recibe
`invalid_request`. Esto cierra la degradación de PKCE por negociación.

## 3. `POST /oauth/register` (RFC 7591)

Sin autenticación (así lo exige el registro dinámico), con limitación de ritmo estricta.

Petición aceptada (todo lo demás se ignora, nunca se rechaza por campos extra):
```json
{
  "client_name": "Claude Code",
  "redirect_uris": ["http://127.0.0.1:51703/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none",
  "scope": "safent.tools safent.status",
  "software_id": "claude-code",
  "software_version": "2.1.199"
}
```

Validación (fail-closed, todo fallo → `400 invalid_redirect_uri` o `400 invalid_client_metadata`):
- `redirect_uris` 1..4 entradas; cada una: o bien `http://127.0.0.1:<puerto>/…` /
  `http://localhost:<puerto>/…`, o bien un esquema privado `^[a-z][a-z0-9+.-]*://` que **no**
  sea `http`/`https`. Sin comodines, sin fragmento, sin `userinfo`, sin credenciales.
- `token_endpoint_auth_method` debe ser `none` (sólo clientes públicos).
- `grant_types` ⊆ `{authorization_code, refresh_token}`; `response_types` = `["code"]`.
- `client_name` ≤ 120 caracteres, se **normaliza y escapa** antes de guardarlo (se pinta en la
  pantalla de consentimiento y en el panel).

Respuesta `201`:
```json
{
  "client_id": "sfc_9f1c…",
  "client_id_issued_at": 1789200000,
  "client_name": "Claude Code",
  "redirect_uris": ["http://127.0.0.1:51703/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none",
  "scope": "safent.tools safent.status"
}
```
Sin `client_secret` (cliente público). Sin `registration_client_uri` en Fase 1: no hay gestión
de registro; un cliente se sustituye registrando otro.

**Derivación de la etiqueta de la instalación** — el `client_name` **no** se usa como etiqueta.
La etiqueta (`installation.label`) se compone en `/oauth/authorize`, en este orden:
1. `harness` = `claude-code` si `software_id ∈ {claude-code, claude_code}` o el `User-Agent`
   del navegador de consentimiento no decide; `codex` si `software_id ∈ {codex, codex-cli}`.
   Si no se puede decidir, la pantalla de consentimiento **lo pregunta** (selector de dos
   opciones); nunca se adivina.
2. `hostname` = el valor del parámetro opcional `safent_host` de `/oauth/authorize` (lo pone el
   lanzador `safent codex install` / el plugin), saneado a `[A-Za-z0-9 ._-]{1,63}`; vacío si
   no viene.
3. `label` = `"Claude Code en <hostname>"` · `"Codex en <hostname>"` · `"Claude Code"` si no
   hay hostname. Editable después desde el panel.

Ritmo: 5 registros / hora / IP y 50 / hora globales; excedido → `429` con `Retry-After`.
Auditoría: `oauth.client_registered` (org `system`, `target` = `client_id`).

## 4. `GET /oauth/authorize`

Parámetros: `response_type=code`, `client_id`, `redirect_uri`, `code_challenge`,
`code_challenge_method=S256`, `state`, `scope`, `resource` (RFC 8707; si viene debe ser
exactamente `https://mcp.safent.app/mcp`), y el opcional propio `safent_host`.

Orden de ejecución — **cualquier fallo antes del paso 4 responde con una página de error de
Safent, nunca con un redirect** (es la defensa contra redirect abierto):

1. `client_id` existe y `status='active'`.
2. `redirect_uri` **coincide exacta y literalmente** (comparación de cadena completa, sin
   normalizar, sin prefijos) con una de las `redirect_uris` registradas. A partir de aquí el
   redirect es seguro.
3. `code_challenge_method='S256'` y `code_challenge` es base64url de 43..128 caracteres.
   `plain` ⇒ error. Ausente ⇒ error.
4. Sesión: si no hay cookie `ConsoleSession` válida → 302 a `/auth/login` con el
   `/oauth/authorize` original guardado **en el servidor** (fila `oauth_authorization_request`
   efímera, 10 min) y referenciada por un identificador opaco en la URL de vuelta. No se
   reinyecta la URL completa como parámetro.
5. Resolución de organización:
   - Si el email de la sesión tiene una `org_invitation` vigente → la pantalla la ofrece en
     primer lugar; aceptarla crea la `membership` y el `employee` del puesto invitado.
   - Organizaciones donde ya hay `membership` → lista.
   - Si no hay ninguna y no hay invitación → se crea **la organización personal** en el acto
     (`tenant.personal=1`, `seat_limit=3`, `membership` owner, `employee`, `agent_template`
     por defecto y `license` plan `personal`) y se sigue sin pedir nada.
6. Paso a paso (MFA) — se exige `POST /api/mfa/verify` fresco en la propia pantalla cuando:
   la organización elegida tiene `personal=0`, **o** `license.remote_approval_enabled=1`.
   Se resuelve con el `MfaService.verify_step_up` existente (contador monótono anti-replay).
   Una organización personal sin aprobación remota **no** pide MFA (criterio SC-1, < 5 min).
7. Pantalla de consentimiento. Muestra, en castellano y sin jerga: nombre del cliente
   (escapado), organización elegida, nombre propuesto de la instalación (editable),
   número de herramientas que quedarán disponibles, cuáles exigirán confirmación, y que la
   instalación será revocable desde el panel.
8. Al aceptar: reserva de asiento (`license`), creación de `instance` (`kind='harness'`,
   `instance_secret_hash=NULL`) y de `installation`; auditoría `installation.created` +
   `oauth.consent_granted`; emisión del código.
   Sin asiento libre → página de error `402` con enlace al panel de licencias
   (**no** redirect con `error=`: es un problema de la organización, no del cliente).

Redirect de éxito: `302 {redirect_uri}?code=<código>&state=<state>`.
Código: 32 bytes aleatorios en base64url, **sólo se guarda su SHA-256**, TTL **60 s**, un solo
uso (CAS sobre `consumed_at`).

Reanudación: si ya existe una `installation` `active` para (`user_id`, `org_id`, `client_id`,
`harness`, `hostname`), se **reanuda** esa misma instalación en vez de crear otra — el
identificador del panel no cambia al renovar el login.

## 5. `POST /oauth/token`

`Content-Type: application/x-www-form-urlencoded`. Sin autenticación de cliente (público).

### 5.1 `grant_type=authorization_code`
Campos: `code`, `redirect_uri`, `client_id`, `code_verifier`, `resource` (opcional).
Comprobaciones, todas en la misma transacción:
- código existe, no consumido, no vencido → si no, `invalid_grant` (mensaje idéntico en los
  tres casos: no se distingue "no existe" de "vencido" de "ya usado").
- `client_id` y `redirect_uri` **iguales** a los del código.
- `BASE64URL(SHA256(code_verifier)) == code_challenge`, comparación en tiempo constante.
- `resource`, si viene, igual al del código.
- `installation.status='active'` e `instance.state='active'` y licencia vigente.
- Consumo del código con UPDATE condicional (`consumed_at IS NULL`); perder la carrera es
  `invalid_grant` y **revoca la instalación** (un código usado dos veces es robo).

Respuesta `200`:
```json
{
  "access_token": "sft_at_…",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "sft_rt_…",
  "scope": "safent.tools safent.status"
}
```

### 5.2 `grant_type=refresh_token`
Campos: `refresh_token`, `client_id`, `scope` (opcional, sólo puede reducir), `resource`.
- Rotación obligatoria: se emite un refresco nuevo y el presentado queda `revoked=1` con
  `rotated_from` encadenado.
- **Detección de reutilización**: si el refresco presentado ya estaba `revoked=1` y tiene
  descendencia → se marca `reuse_detected_at`, se revoca **toda la cadena y la instalación**,
  se audita `installation.revoked` con razón `refresh_reuse`, y se responde `invalid_grant`.
- Si la instalación está revocada, la licencia caducada o la instancia revocada →
  `invalid_grant`.

### 5.3 Contenido y vida de los tokens
Tokens **opacos** (no JWT): 32 bytes aleatorios, prefijo `sft_at_` / `sft_rt_`, guardados como
SHA-256. No hay `aud`/`iss` que verificar en el cliente porque no se le entrega nada firmado.
La resolución en cada petición es una consulta por hash:

`token_hash → oauth_token{installation_id, user_id, org_id, client_id, scope, resource}`
`→ installation{employee_id, instance_id, harness, status}` `→ instance{state}` `→ license`.

Vidas: `access` 3600 s, `refresh` 30 días deslizantes. **La revocación es inmediata** porque la
validación toca la base de datos en cada llamada; por eso el acceso puede durar una hora sin
debilitar el corte desde el panel (justificación de la elección frente a un JWT de 5 min).

`resource` se guarda y se **comprueba** en `/mcp`: un token emitido para otro recurso no sirve
(mitigación de reenvío entre servidores MCP, RFC 8707).

### 5.4 Errores
Formato RFC 6749 §5.2: `{"error": "...", "error_description": "..."}` con `400` (o `401` para
`invalid_client`). `error_description` es genérica y en castellano; nunca dice qué campo
concreto falló en el camino del código.

## 6. `POST /oauth/revoke` (RFC 7009)
Campos: `token`, `token_type_hint` (opcional), `client_id`.
Siempre `200` (aunque el token no exista). Revocar un `refresh` revoca la cadena entera y
todos los `access` de la misma instalación. Revocar un `access` revoca sólo ese.
Auditoría `oauth.token_revoked`.

## 7. Interacción con MFA (paso a paso)
- **Al instalar**: §4 paso 6 — sólo organizaciones no personales o con aprobación remota.
- **Al aprobar en el panel**: sin cambios, sigue `requires_security_step_up(tool_name)` en
  `POST /api/approvals/{request_id}`.
- **Al revocar una instalación**: `POST /api/installations/{id}/revoke` exige TOTP, igual que
  `POST /api/instances/{id}/revoke` hoy.
- El arnés **nunca** ve ni transporta un código TOTP: el paso a paso vive en el navegador y en
  el panel, jamás en una herramienta MCP.

## 8. Notas de amenaza

| Amenaza | Mitigación en este contrato |
| --- | --- |
| Redirect abierto | Coincidencia literal exacta de `redirect_uri` **antes** de cualquier redirect; todo fallo previo se pinta como página, no se redirige. Sin comodines ni normalización. |
| Degradación de PKCE | `plain` ausente de los metadatos y rechazado en `/authorize`; `code_challenge` obligatorio; verificación en tiempo constante. |
| Reenvío de código (code injection) | Código de 60 s, un solo uso con CAS, ligado a `client_id`+`redirect_uri`+`code_challenge`; doble uso ⇒ revocación de la instalación. |
| Reutilización / robo de refresco | Rotación obligatoria + detección de reuso ⇒ revocación de la cadena y de la instalación. |
| Suplantación de cliente | El registro dinámico no confiere autoridad; la autoridad es el consentimiento de la persona sobre una organización concreta. El `client_name` se escapa: no puede fingir ser Safent en la pantalla. |
| Reenvío del token a otro servidor MCP | `resource` (RFC 8707) guardado y comprobado en `/mcp`. |
| Escalada desde el arnés al contrato v1 de instancia | `instance_secret_hash = NULL` en toda instalación de arnés: `_authenticate_instance*` no puede autenticarla jamás. |
| Confused deputy en el consentimiento | La organización se elige en la pantalla, no en un parámetro; `org_id` sale siempre de `employee.org_id`, nunca del cliente. |
| Fuerza bruta de registro | Ritmo por IP y global; `oauth_client` con `created_ip` para barrido. |
| Fijación de sesión en el login | El `/oauth/authorize` original se guarda en servidor y se referencia por id opaco; nunca se reinyecta como URL en un parámetro. |
| CSRF en el consentimiento | El botón de aceptar es `POST /oauth/authorize/consent` con testigo de un solo uso ligado a la `ConsoleSession` y a la petición guardada. |
