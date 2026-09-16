# Contrato — Pasarela MCP remota (`https://mcp.safent.app/mcp`)

Repo: `lumen-control-enterprise` · Módulo nuevo `src/safent_control/mcp_gateway/`:
- `domain/` — `GatewayPolicy`, `ToolClass`, `PolicyDecision`, `action_digest`.
- `application/` — `ListTools`, `CallTool`, `ResolveApproval`; puertos
  `CompanionPort`, `PolicySnapshotPort`, `ApprovalPort`, `CallLogPort`, `RateLimiterPort`.
- `infrastructure/` — `HttpCompanionClient` (capa anticorrupción hacia `safent-ads`),
  `PublishedPolicySnapshotAdapter`, `RemoteApprovalAdapter`, `SqlCallLog`.
- `api/mcp_gateway.py` — transporte streamable HTTP.

## 0. Montaje (obligatorio, y colisiona con lo que hay)

`api/app.py:201-203` ya monta la superficie MCP del Cerebro en `/mcp` con `FastApiMCP`.

1. La superficie del Cerebro **se mueve a `/mcp/cerebro`** (`mcp.mount_http(mount_path="/mcp/cerebro")`)
   y se republica el `McpSpec.url` de las plantillas que la apuntan.
2. La pasarela se registra como **ruta exacta**, nunca `Mount`:
   `app.router.routes.append(Route("/mcp", endpoint=gateway_app, methods=["GET","POST","DELETE"]))`
   — `Mount("/mcp")` no casa el path pelado y deja al cliente MCP esperando un 307.
3. Ambas van **antes** del catch-all de la SPA (`/{path:path}`), que Starlette resuelve por
   orden de registro.
4. `spa_fallback._api_prefixes` gana `"mcp"` y `".well-known/"`.

## 1. Transporte

`POST /mcp` (JSON-RPC entrante) y `GET /mcp` (flujo SSE del servidor), streamable HTTP del
MCP. `DELETE /mcp` cierra sesión.

- `Authorization: Bearer sft_at_…` obligatorio. Sin token o token inválido/vencido/revocado:
  `401` + `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp", error="invalid_token"`.
- `Mcp-Session-Id`: lo emite la pasarela en la respuesta a `initialize` (32 bytes base64url).
  Sesión = `{installation_id, org_id, protocol_version, created_at, last_seen_at}` en memoria
  con TTL 30 min de inactividad. **La sesión no es autoridad**: cada petición vuelve a resolver
  el bearer contra la base de datos, así que revocar corta en la llamada siguiente aunque la
  sesión siga viva. Un `Mcp-Session-Id` presentado con un bearer de otra instalación ⇒ `404`
  (sesión desconocida), nunca se reusa.
- Protección anti DNS-rebinding: `Host` ∈ {`mcp.safent.app`}, `Origin` ausente o
  `https://mcp.safent.app`. Mismo criterio que el companion ya aplica.
- `MCP-Protocol-Version` se responde con la versión negociada; si el cliente pide una que no
  soportamos, se negocia a la nuestra (comportamiento estándar), nunca se falla.

## 2. `tools/list`

Composición de la lista, por este orden:

1. **Herramientas de gobierno de Safent** (siempre, aunque no haya companion):
   - `safent_status` — estado de la instalación: organización, puesto, herramientas
     disponibles, cuentas de anuncios asignadas, aprobaciones pendientes, versión de política.
   - `safent_panel` — devuelve la URL profunda del panel web
     (`https://app.safent.app/installations/{id}` o `/ads/cockpit`). No abre nada: devuelve el
     enlace para que el modelo lo enseñe.
   - `safent_approval_status` — consulta/reanuda una acción que quedó pendiente de panel (§5).
2. **Herramientas del companion de la organización**, tal como las publica su `/mcp`
   (`tools/list` aguas arriba), filtradas por `GatewayPolicy` (las `deny` **no aparecen**:
   omisión, no filtro cosmético) y por el `ads_grant` de la instalación (sin ninguna cuenta
   asignada sólo quedan `list_businesses`, `get_capabilities` y las de gobierno).

**Decisión de nombres: se conservan los nombres del companion** (`list_campaigns`,
`propose_campaign_draft`, `apply_defensive_action`…). Sin prefijo `safent_`. Razones:
(a) `hermes.capabilities.tool_sensitivity._SAFENT_ADS_WRITE_TOOLS` ya clasifica el gasto por
esos nombres exactos — renombrar bifurca la única fuente de verdad de qué es SPEND;
(b) el `ToolRegistry` del companion **rechaza** nombres que no empiecen por verbo
(`is_read_verb`/`is_proposal_verb`), así que `safent_propose_pause` sería ilegal aguas arriba;
(c) Claude Code y Codex ya espacian por servidor (`mcp__safent__list_campaigns`), así que no
hay colisión posible en el arnés. El prefijo `safent_` queda **sólo** para las tres
herramientas de gobierno, que no pertenecen al catálogo del companion y por tanto no están
sujetas a su regla de nombres.

### 2.1 Esquemas: expansión de `$ref`
El `inputSchema` de cada herramienta se publica con los `$ref` locales (`#/$defs/X`,
`#/definitions/X`) **expandidos en línea** y las tablas de definiciones eliminadas — el mismo
algoritmo que `hermes.runtime.mcp_tool_specs.inline_local_refs` (límite de profundidad, los
recursivos se dejan como están). Sin esto, `propose_campaign_draft` (cuyo `changes` es un
modelo cerrado) le llega al modelo como `object` pelado y se inventa las claves. La pasarela
**importa el algoritmo**, no lo reimplementa: se extrae a un módulo compartido o se copia con
prueba de paridad byte a byte contra el del runtime (ver `tasks.md` T-B3).

### 2.2 `_meta` por herramienta
```jsonc
{
  "name": "propose_campaign_draft",
  "description": "…",
  "inputSchema": { /* expandido */ },
  "_meta": {
    "anthropic/requiresUserInteraction": true,
    "safent/toolClass": "interactive",
    "safent/decision": "allow"      // "allow" | "panel"
  }
}
```
`anthropic/requiresUserInteraction: true` en **toda** herramienta cuyo `ToolClass` del
companion sea `PROPOSAL` o `CATALOG_WRITE`, y en `apply_defensive_action`. En Claude Code
(2.1.199+) eso fuerza la tarjeta de confirmación nativa y **no se puede autoaprobar por
reglas**. Las de clase `READ` no lo llevan.
Las tres de gobierno: `safent_status` y `safent_panel` son lectura;
`safent_approval_status` es lectura (consulta un estado que un humano ya decidió).

### 2.3 `GET /mcp/tool-classes` — fuente única para el lanzador
Sin autenticación de instalación (sólo bearer válido), respuesta cacheable 5 min:
```json
{
  "version": "2026-09-14",
  "interactive": ["propose_budget_change", "propose_pause", "propose_ad_child", "..."],
  "read": ["list_campaigns", "get_campaign", "..."]
}
```
`interactive` es **exactamente** el conjunto que lleva `requiresUserInteraction`. El lanzador
de Codex lo lee y escribe la configuración; nunca mantiene su propia lista (§7).

## 3. `tools/call` — orden de ejecución

```
1. Resolver bearer → (installation, instance, license)          401 si falla
2. installation.status='active' ∧ instance.state='active'
   ∧ license vigente ∧ tenant.billing_status ∈ {trialing,active}   401 / 402
3. Ritmo (§6)                                                    -32000 SAFENT_RATE_LIMITED
4. Herramienta en el catálogo compuesto                          -32602 SAFENT_TOOL_UNKNOWN
5. GatewayPolicy(tool) → allow | panel | deny                    deny ⇒ resultado de error
6. arg_digest = SHA-256(canonical({tool, args}))
7. si panel  → crear remote_approval, devolver «pendiente» (§5)
   si allow  → llamar al companion (§4)
8. Escribir mcp_call_log SIEMPRE (una fila por llamada)
9. Escribir audit_log si decision ≠ allow-de-lectura:
   mcp.tool_write | mcp.tool_denied | mcp.tool_pending
```

Paso 5, **fail-closed** (Constitución IV): sin `published_policy` verificable para la
instancia, o con firma que no valida contra la clave pública del tenant, la decisión es `deny`
para todo salvo `safent_status` y `safent_panel`.

`GatewayPolicy` se deriva del `access_scope.policy_overlay` del paquete firmado:
`enabled=false` ⇒ `deny` · `approval='hitl'` ⇒ `panel` · `approval='auto'` ⇒ `allow` ·
sin entrada ⇒ `allow` para `READ`, `panel` para toda escritura si
`license.remote_approval_enabled=1`, `allow` en otro caso (la confirmación nativa del arnés ya
la cubre).

## 4. Llamada al companion alojado

Configuración por organización (`SAFENT_MCP_COMPANIONS`, secretos por referencia, nunca en
claro en el repositorio):
```
org_id -> { base_url: "https://ads-<org>.safent.internal:8443",
            mcp_path: "/mcp",
            token_ref: "<id en el gestor de secretos>",
            ca_path: "/etc/safent/ads-ca.pem" }
```

Petición de la pasarela al companion:
- `Authorization: Bearer <ADS_MCP_TOKEN de esa organización>` — credencial de **servicio**,
  jamás el token del arnés. El companion la compara en tiempo constante
  (`BearerTokenMiddleware`).
- `X-Safent-Caller-Scope: <grant_token>` — el **token de concesión Ed25519 que Enterprise ya
  emite** (`AdsGrants.issue(grant_id, instance)`), firmado con la clave del tenant, con
  `purpose`/`aud`/`exp`/`jti`. La pasarela lo obtiene en proceso con
  `repo.get_instance(installation.instance_id)`; no hay llamada HTTP a `/v1/ads/grants/token`
  (ese camino exige `instance_secret`, que una instalación de arnés no tiene por diseño).
- `X-Safent-Installation: <installation_id>` y `X-Safent-Request: <call_id>` — sólo para traza.

**Cambio necesario en `safent-ads`**: `StaticCallerScopeResolver` se sustituye por
`EnterpriseCallerScopeResolver`, que introspecciona `X-Safent-Caller-Scope` contra
`POST /internal/ads/introspect` de Enterprise y devuelve
`CallerScope(caller_id="installation:<id>", allowed_business_ids=frozenset(...))`.
Sin cabecera o con introspección fallida ⇒ `CallerScope` con `allowed_business_ids=frozenset()`
(no `None`): alcance vacío, no alcance total. Hoy `None` significa «todos los negocios» y es el
valor por defecto — ese default es el riesgo y hay que invertirlo.

Hasta cerrar ADS-02 hay **un despliegue de companion por organización**; el aislamiento real lo
dan el despliegue y este alcance, no el modelo de datos del companion (que sigue siendo de un
solo propietario, `SqlOwnerBridgeRepository`).

## 5. Pendiente de aprobación y reanudación

Cuando la decisión es `panel`, la pasarela crea `remote_approval` (`request_id` uuid4 fresco,
`proposal_id = "gw:" + arg_digest[:32]`, `expires_at = now + 30 min`) y devuelve un resultado
**no de error**:

```json
{
  "content": [{"type": "text", "text": "Esta acción necesita la aprobación de un responsable en el panel de Safent. Se ha enviado la solicitud. Consulta el estado con safent_approval_status usando request_id=\"…\"; se puede aprobar desde https://app.safent.app/approvals."}],
  "structuredContent": {
    "status": "pending_approval",
    "request_id": "…",
    "tool_name": "propose_budget_change",
    "expires_at": "2026-09-14T18:35:00Z",
    "panel_url": "https://app.safent.app/approvals"
  },
  "isError": false
}
```

`safent_approval_status({"request_id": "..."})`:
- `pending` → `{"status":"pending", "expires_at": …}` y una frase que invita a esperar o a
  abrir el panel. El modelo decide cuándo reintentar; no hay espera bloqueante en el servidor.
- `approved` → **ejecuta la herramienta original una sola vez** y devuelve su resultado real.
  La ejecución única se garantiza con el `UPDATE remote_approval SET acked=1 WHERE
  request_id=? AND acked=0 AND state='approved'`: sólo quien pasa el pestillo 0→1 llama al
  companion. Los argumentos originales se guardan cifrados junto a la fila (columna nueva
  `mcp_call_log.pending_args_enc`, AES-GCM con la clave del tenant, borrada al ejecutar o al
  expirar) — la pasarela **no** pide al modelo que repita los argumentos: eso permitiría
  aprobar una cosa y ejecutar otra.
- `denied` / `expired` → resultado con `isError: true` y código `SAFENT_APPROVAL_DENIED` /
  `SAFENT_APPROVAL_EXPIRED`.

**Se descarta re-invocar la herramienta original tras la aprobación**: rompe el vínculo entre
lo que el humano leyó (`params_redacted` + `action_digest`) y lo que se ejecuta (TOCTOU).

Doble vía, tal como pide `spec.md` regla 4: la confirmación nativa del arnés
(`requiresUserInteraction` / `approval_mode="approve"`) ocurre **antes** de que la llamada
llegue a la pasarela; la aprobación de panel ocurre **después**. Una no sustituye a la otra.

## 6. Límites de ritmo

Cubo de fichas (token bucket) en el proceso, respaldado por la base de datos para el conteo
diario:

| Ámbito | Sostenido | Ráfaga |
| --- | --- | --- |
| `tools/call` por instalación | 120 / min | 20 |
| `tools/call` de clase `interactive` por instalación | 10 / min | 3 |
| `tools/call` por organización | 600 / min | 100 |
| `initialize` por instalación | 10 / min | 5 |
| `/oauth/token` por `client_id` | 60 / min | 10 |

Excedido: resultado de herramienta `isError: true`, código `SAFENT_RATE_LIMITED`, con
`retry_after_seconds`. Al nivel HTTP (fuera de JSON-RPC) se responde `429` con `Retry-After`.

## 7. Configuración que escribe el lanzador

### Claude Code (plugin `safent`, `.mcp.json`)
```json
{ "mcpServers": { "safent": { "type": "http", "url": "https://mcp.safent.app/mcp" } } }
```
Equivalente por comando: `claude mcp add --transport http safent https://mcp.safent.app/mcp`
seguido de `claude mcp login safent`. **Nada más**: ni cabeceras, ni tokens, ni variables de
entorno (SC-4). El token lo guarda el propio almacén de Claude Code.

### Codex (`safent codex install`, repo `safent-runtime/ops/harness/codex.py`)
Verificado contra `codex-cli 0.154.0-alpha.6.1` en esta máquina:
```sh
codex mcp add safent --url https://mcp.safent.app/mcp --oauth-client-registration DCR
codex mcp login safent
```
y el bloque de configuración equivalente:
```toml
[mcp_servers.safent]
url = "https://mcp.safent.app/mcp"
auth = "oauth"

[mcp_servers.safent.tools.propose_budget_change]
approval_mode = "approve"
# … una entrada por cada nombre de GET /mcp/tool-classes → interactive
```
El lanzador **genera** ese bloque a partir de `GET /mcp/tool-classes` en cada
`safent codex install` y en cada arranque; nunca lleva la lista escrita a mano. El lanzador
jamás escribe `--dangerously-bypass-approvals-and-sandbox`.

## 8. Mapa de errores

| Situación | Capa | Respuesta |
| --- | --- | --- |
| Sin bearer / inválido / revocado / recurso equivocado | HTTP | `401` + `WWW-Authenticate` con `resource_metadata` |
| Suscripción inactiva / sin asiento | HTTP | `402`, cuerpo `{"error":{"code":"SAFENT_SEAT_INACTIVE"}}` |
| `Host`/`Origin` no permitidos | HTTP | `403` |
| Sesión MCP desconocida | HTTP | `404` |
| Ritmo excedido a nivel de transporte | HTTP | `429` + `Retry-After` |
| Herramienta desconocida | JSON-RPC | `-32602`, `SAFENT_TOOL_UNKNOWN` |
| Política deniega | resultado | `isError: true`, `SAFENT_POLICY_DENIED` + razón legible |
| Negocio fuera del alcance concedido | resultado | `isError: true`, `SAFENT_ACCOUNT_NOT_GRANTED` |
| Argumentos inválidos (los valida el companion) | resultado | `isError: true`, se **reenvía tal cual** el `{code, message}` del companion (es lo que le enseña al modelo a corregirse) |
| Companion caído / TLS / tiempo agotado | JSON-RPC | `-32603`, `SAFENT_COMPANION_UNAVAILABLE`, sin texto del origen |
| Ritmo excedido dentro de una llamada | resultado | `isError: true`, `SAFENT_RATE_LIMITED` |
| Aprobación pendiente | resultado | **no es error** (§5) |

Ningún mensaje devuelve texto del origen, DSN, cabecera ni traza. Los códigos son estables y
en inglés; los mensajes, en castellano y para una persona.

## 9. Auditoría por llamada

Siempre, una fila `mcp_call_log`:
`{call_id, installation_id, org_id, ts, tool_name, tool_class, decision, outcome, error_code,
arg_digest, duration_ms, request_id?}`.

Además, fila en `audit_log` (encadenada, retención indefinida) cuando
`tool_class='interactive'` **o** `decision≠'allow'`:
`action ∈ {mcp.tool_write, mcp.tool_denied, mcp.tool_pending}`, `target=installation_id`,
`actor='installation:'+id`, `actor_ref='installation:'+id`,
`changes={tool_name, decision, outcome, arg_digest, request_id?}`.

Las lecturas **no** entran en `audit_log`: esa tabla lleva `seq`/`prev_hash`/`entry_hash` por
organización y serializa las escrituras; meterle 120 lecturas por minuto por instalación
destruiría la cadena como instrumento y como rendimiento. SC-3 exige el rastro de las acciones
con dinero, y esas sí van encadenadas.
