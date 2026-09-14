# 032 — Tareas, Fase 1 (P1: instalar, vincular y operar Anuncios desde Claude Code y Codex)

Fecha: 14-sep-2026. Fuente: `spec.md`, `plan.md` §2 Fase 1, `research.md`, `data-model.md`,
`contracts/{oauth,mcp-gateway,panel}.md`.
Convención: `[P]` = puede ir en paralelo dentro de su carril · `[US1]` = historia P1 ·
propietario = especialista · cada tarea trae su prueba de aceptación (lo que la da por hecha).

**Tres carriles, un integrador.**
- **A — Nube e identidad** (`lumen-control-enterprise`: despliegue, cuentas, OAuth).
- **B — Pasarela y panel** (`lumen-control-enterprise`: `mcp_gateway/`, `frontend/`).
- **C — Arnés y companion** (`safent-plugins`, `safent-runtime/ops/harness/`, `safent-ads`).

Ningún carril toca ficheros de otro carril. `api/app.py` es el único punto de cruce (A lo toca
en T-A6, B en T-B1): **B espera a que A haya hecho su cambio**, no se edita a la vez.

---

## Puerta 0 — Verificaciones (bloquean, no se paralelizan con lo que dependen)

| # | Tarea | Propietario | Ficheros | Aceptación |
| --- | --- | --- | --- | --- |
| T-V1 | Servidor de autorización de juguete (≤ 150 líneas, desechable) que sirva los dos `.well-known`, `/oauth/register`, `/authorize`, `/token`; capturar todo lo que pida Claude Code | `backend-engineer` | `/tmp/oauth-probe/` (no se commitea) | `claude mcp add --transport http` + `claude mcp login` completan; traza de peticiones anotada en `research.md` §V1 |
| T-V2 | Lo mismo con `codex mcp add safent --url … --oauth-client-registration DCR` + `codex mcp login safent` | `backend-engineer` | ídem | Login completo; diferencias con V1 anotadas en `research.md` §V2 |
| T-V3 | Valores reales de `-a/--ask-for-approval` y de `approval_policy` en 0.154 | `devops-engineer` | — | Tabla de valores válidos en `research.md` §V3 |
| T-V4 | ¿Existe `tools.<tool>.approval_mode = "approve"` y lo respeta para un servidor remoto? | `devops-engineer` | — | Respuesta sí/no con evidencia en `research.md` §V4 |
| T-V7 | ¿`_meta["anthropic/requiresUserInteraction"]` se respeta en un servidor **remoto**? | `backend-engineer` | `/tmp/oauth-probe/` | Sí/no con captura; si es «no», `GatewayPolicy` fuerza `decision='panel'` en toda escritura |
| T-V8 | ¿El `/mcp` del companion de Friendog acepta una cabecera `X-Safent-Caller-Scope` sin romper su bearer? | `backend-engineer` | — | `curl` con la cabecera devuelve el mismo `tools/list` que sin ella |

**Salida de la puerta**: T-V1, T-V2, T-V4 y T-V7 anotados. T-A5, T-B4 y T-C2 no arrancan antes.

---

## Carril A — Nube e identidad (`lumen-control-enterprise`)

| # | Tarea | Propietario | Ficheros | Aceptación |
| --- | --- | --- | --- | --- |
| T-A1 [US1] | **Desplegar Safent Cloud para Friendog**: Enterprise + companion en la VM, HTTPS, subdominios `app.safent.app` y `mcp.safent.app`, secretos de servicio, copias y salud (DEPLOY-01) | `devops-engineer` | `deploy/vm/*`, `deploy.sh`, `Dockerfile`, `safent-ads/compose.yaml` + `compose.companion.yaml` | `GET https://app.safent.app/readyz` → `{"ok":true}` desde fuera; restaurar una copia levanta el servicio; `mcp.safent.app` resuelve al mismo backend |
| T-A2 [US1] | **Migración M1–M6**: `instance.kind`, `tenant.personal/owner_user_id`, tablas `installation`, `oauth_client`, `oauth_authorization_code`, `oauth_token`, `org_invitation`, `mcp_call_log`, índices, constantes de auditoría | `database-engineer` | `src/safent_control/infrastructure/repository.py` (`_SCHEMA` + guardas `__init__`), `infrastructure/repository_postgres.py` (`_run_schema`), `domain/audit.py` | Base SQLite existente y base Postgres existente arrancan sin pérdida; `PRAGMA table_info` / `information_schema` coinciden entre ambos adaptadores; prueba de paridad de esquema en verde |
| T-A3 [US1] [P] | **Dominio y repositorio de `Installation`**: dataclass, estados, invariantes 1–5 de `data-model.md` §3 | `backend-engineer` | `src/safent_control/domain/entities.py` (`Installation`, `InstallationStatus`, `HarnessKind`), `application/installation_service.py`, métodos de repositorio en ambos adaptadores | Unit: crear instalación reserva asiento (402 sin asiento), `instance_secret_hash` queda `NULL`, revocar apaga tokens+instancia en una transacción, reanudar el mismo (user, org, client, harness, host) no duplica |
| T-A4 [US1] [P] | **Alta personal e invitaciones**: `POST /api/signup`, `POST/GET/DELETE /api/orgs/{id}/invitations`, `POST /api/invitations/accept` | `backend-engineer` | `src/safent_control/application/{org_signup,invitations}.py`, `api/console.py` | Unit + integración: alta idempotente; organización personal con `seat_limit=3` y `personal=1`; invitar a una organización personal → 403; aceptar con otro email → 403; dos organizaciones aisladas |
| T-A5 [US1] | **Servidor de autorización** completo según `contracts/oauth.md` §1–§7 | `backend-engineer` + revisión de `security-engineer` | `src/safent_control/oauth/{domain,application}/`, `api/oauth.py`, `api/well_known.py` | Batería de conformidad: PKCE `plain` rechazado; `redirect_uri` no literal rechazado **sin redirigir**; código de 60 s, un uso, doble uso revoca instalación; rotación de refresco + detección de reuso revoca la cadena; `resource` distinto rechazado; revocación corta en la llamada siguiente. Y: `claude mcp login safent` **real** completa |
| T-A6 [US1] | **Mover la superficie MCP del Cerebro a `/mcp/cerebro`** y añadir `"mcp"`+`".well-known/"` a `_api_prefixes` de la SPA | `backend-engineer` | `src/safent_control/api/app.py` | El puente del Cerebro sigue funcionando apuntando a `/mcp/cerebro`; `GET /mcp` deja de estar servido por `FastApiMCP`; la SPA no intercepta `/.well-known/*` |
| T-A7 [US1] | **Paso a paso (MFA) en el consentimiento** según `contracts/oauth.md` §4 paso 6, reutilizando `MfaService.verify_step_up` | `backend-engineer` | `src/safent_control/api/oauth.py`, `application/mfa_service.py` (sin cambios de forma) | Organización personal sin aprobación remota: consiente sin MFA. Organización de empresa: sin TOTP válido no emite código; el contador monótono impide reusar el mismo código |
| T-A8 | **Modelo de amenazas STRIDE** del servidor de autorización y de la pasarela | `security-engineer` | `specs/032-…/threat-model.md` | Cubre las diez filas de `contracts/oauth.md` §8 y las nueve de `mcp-gateway.md` §8; veredicto antes de exponer `mcp.safent.app` a Internet |

Orden interno: T-A1 → T-A2 → {T-A3, T-A4} → T-A6 → T-A5 → T-A7 → T-A8.

---

## Carril B — Pasarela MCP y panel (`lumen-control-enterprise`)

| # | Tarea | Propietario | Ficheros | Aceptación |
| --- | --- | --- | --- | --- |
| T-B1 [US1] | **Esqueleto de transporte**: `Route("/mcp")` exacta (nunca `Mount`), `initialize`, `Mcp-Session-Id`, anti DNS-rebinding, `401` con `WWW-Authenticate`+`resource_metadata` | `backend-engineer` | `src/safent_control/mcp_gateway/api/mcp_gateway.py`, `api/app.py` (una línea de registro, **después** de T-A6) | `POST /mcp` pelado responde (no 307); `initialize` devuelve sesión; bearer ausente → 401 con la cabecera exacta; `Host` ajeno → 403 |
| T-B2 [US1] [P] | **`GatewayPolicy`** desde `published_policy` verificado: `allow`/`panel`/`deny`, fail-closed sin política o con firma inválida | `backend-engineer` | `mcp_gateway/domain/policy.py`, `infrastructure/published_policy_snapshot.py` | Tabla de decisiones en unit; firma manipulada → `deny` en todo salvo `safent_status`/`safent_panel`; sin `published_policy` → lo mismo |
| T-B3 [US1] [P] | **Expansión de `$ref`**: extraer `inline_local_refs` a un módulo compartido o replicar con prueba de paridad byte a byte contra `hermes/runtime/mcp_tool_specs.py` | `backend-engineer` | `mcp_gateway/domain/schema_inlining.py`, `tests/unit/mcp_gateway/test_schema_parity.py` | El esquema publicado de `propose_campaign_draft` no contiene `$ref` ni `$defs` y es **idéntico** al que produce el runtime sobre la misma entrada |
| T-B4 [US1] | **`tools/list`**: gobierno (`safent_status`, `safent_panel`, `safent_approval_status`) + catálogo del companion filtrado por política y por `ads_grant`; `_meta` con `anthropic/requiresUserInteraction` en toda `PROPOSAL`/`CATALOG_WRITE`; `GET /mcp/tool-classes` | `backend-engineer` | `mcp_gateway/application/list_tools.py`, `infrastructure/http_companion_client.py` | Desde Claude Code real: `/mcp` lista las herramientas; ninguna denegada aparece; toda escritura pide confirmación nativa; `GET /mcp/tool-classes` y el `_meta` publicado coinciden exactamente (prueba que compara los dos conjuntos) |
| T-B5 [US1] | **`tools/call`**: orden de ejecución de `mcp-gateway.md` §3, llamada al companion con credencial de servicio + `X-Safent-Caller-Scope` (token `ads_grant` emitido en proceso), `mcp_call_log` siempre, `audit_log` cuando toca | `backend-engineer` | `mcp_gateway/application/call_tool.py`, `infrastructure/{http_companion_client,sql_call_log}.py` | Integración con companion falso: `deny` no sale a la red; `allow` devuelve el resultado del companion tal cual; una fila en `mcp_call_log` por llamada; escritura deja además fila en `audit_log` con `installation_id` |
| T-B6 [US1] | **Aprobación de panel**: creación de `remote_approval`, resultado «pendiente», `safent_approval_status` con pestillo `acked` 0→1 y argumentos cifrados | `backend-engineer` + revisión de `security-engineer` | `mcp_gateway/application/{pending_approval,resolve_approval}.py` | Dos `safent_approval_status` concurrentes sobre una aprobación aprobada ejecutan **una** llamada al companion; `denied`/`expired` devuelven error; los argumentos se borran al ejecutar y al expirar; aprobar en el panel resuelve una herramienta «pendiente» en Claude Code de verdad |
| T-B7 [US1] [P] | **Límites de ritmo** de `mcp-gateway.md` §6 y mapa de errores §8 | `backend-engineer` | `mcp_gateway/infrastructure/rate_limiter.py`, `domain/errors.py` | Cada límite se dispara en su umbral; ningún mensaje de error contiene texto del origen, DSN ni traza (prueba de barrido sobre todas las ramas) |
| T-B8 [US1] | **Panel: instalaciones** — `GET /api/installations`, `GET /api/installations/{id}`, `PATCH`, `POST …/revoke` (OWNER+TOTP), `POST …/pairing-code`, `GET …/calls` | `backend-engineer` | `src/safent_control/api/console.py` | Por ruta: aislamiento entre organizaciones; revocar sin TOTP → 401; revocar corta el `tools/call` siguiente; el código de emparejamiento es consumible por `safent pair` |
| T-B9 [US1] [P] | **Panel: vistas React** — `/installations`, `/installations/:id`, insignia `origin` en `/approvals`, filtro `installation_id` en `/audit`, `/team/invitations` | `frontend-engineer` | `frontend/src/services/api.ts`, `frontend/src/views/Installations*.tsx`, `frontend/src/views/Approvals*.tsx` | Prueba por vista con los cuatro estados (cargando, vacío, error, sin permisos); el vacío enseña el comando copiable; contraste y foco correctos |
| T-B10 [US1] [P] | **`origin`/`installation_id`/`installation_label` en `PendingApproval`**, derivados de `instance.kind` (sin columna nueva) | `backend-engineer` | `src/safent_control/api/console.py`, `frontend/src/services/api.ts` | Una aprobación de la app propia no trae `origin`; una de la pasarela trae `harness` + etiqueta |

Orden interno: T-B1 → {T-B2, T-B3} → T-B4 → T-B5 → T-B6 → {T-B7, T-B8} → {T-B9, T-B10}.
Dependencia externa: T-B1 después de T-A6; T-B4/T-B5 después de T-A5 (necesitan bearer real).

---

## Carril C — Arnés y companion (`safent-plugins`, `safent-runtime`, `safent-ads`)

| # | Tarea | Propietario | Ficheros | Aceptación |
| --- | --- | --- | --- | --- |
| T-C1 [US1] | **Invertir el valor por defecto de `CallerScope`**: sin alcance válido ⇒ `frozenset()` (vacío), nunca `None` | `backend-engineer` + revisión de `security-engineer` | `safent-ads/src/safent_ads/mcp/application/caller_scope.py`, `mcp/infrastructure/caller_scope_resolver.py` | Unit: `CallerScope` sin alcance no accede a ningún negocio; el resolutor estático actual queda detrás de una bandera explícita de desarrollo y falla al arrancar en producción |
| T-C2 [US1] | **`EnterpriseCallerScopeResolver`**: introspecciona `X-Safent-Caller-Scope` contra `POST /internal/ads/introspect` y devuelve `caller_id="installation:<id>"` + negocios permitidos | `backend-engineer` | `safent-ads/src/safent_ads/mcp/infrastructure/enterprise_caller_scope_resolver.py`, `composition/app.py` | Integración: token válido → alcance con los negocios de la concesión; token vencido/manipulado/de otra organización → alcance vacío y `BusinessForbiddenError` en el dispatcher |
| T-C3 [US1] [P] | **Plugin `safent` para Claude Code**: `.mcp.json` con el servidor remoto, comandos `/safent:panel`, `/safent:status`, `/safent:pair`; publicación en el marketplace | `devops-engineer` | `safent-plugins/safent/{.mcp.json,plugin.json,commands/}` | `claude plugin validate` en verde; `/plugin install safent@safent-plugins` deja el servidor registrado; ni un secreto en ningún fichero (barrido, SC-4) |
| T-C4 [US1] | **`safent codex install`**: escribe `[mcp_servers.safent]` con `auth = "oauth"` y **genera** los `tools.<tool>.approval_mode = "approve"` desde `GET /mcp/tool-classes` | `backend-engineer` | `safent-runtime/ops/harness/codex.py` | `codex mcp get safent` muestra el bloque; la lista de `approve` coincide con la respuesta del endpoint (prueba que compara); el fichero generado **nunca** contiene `--dangerously-bypass-approvals-and-sandbox` (prueba de barrido) |
| T-C5 [US1] [P] | **`connect_account` / `list_platform_accounts` por la pasarela**: el enlace de consentimiento del companion apunta al panel web, no al puente local | `backend-engineer` | `safent-ads` (existente) + `mcp_gateway/application/list_tools.py` | Con proveedor falso en integración: la herramienta devuelve una URL del panel; tras el OAuth, `list_platform_accounts` enseña la cuenta. Con Google real: sólo con el dueño presente |
| T-C6 [US1] | **`safent pair` contra la instalación**: la jaula local se empareja sobre `installation.instance_id`, sella `runtime_paired_at`, no crea instancia nueva | `backend-engineer` | `safent-runtime` (`safent` CLI), `lumen-control-enterprise/application/pairing_service.py` | Dos instalaciones, dos códigos, cada jaula ligada a la suya; el asiento no se duplica; revocar la instalación corta también la jaula |

Orden interno: T-C1 → T-C2 → {T-C3, T-C4} → {T-C5, T-C6}.
Dependencia externa: T-C2 después de T-V8; T-C4 después de T-B4 (necesita `/mcp/tool-classes`).

---

## Integración

| # | Tarea | Propietario | Aceptación |
| --- | --- | --- | --- |
| T-I1 [US1] | **`quickstart.md`**: recorrido de humo de principio a fin | `qa-engineer` | Instalar → login → cuentas → dos borradores → aprobar una con freno → verla en el panel, escrito paso a paso y ejecutable por alguien que no ha tocado el código |
| T-I2 [US1] | **Aceptación P1 con Claude Code** en un Mac limpio, sin runtime local | `qa-engineer` + dueño | 10 de 10 intentos con el modelo por defecto (SC-2); menos de 5 minutos de reloj (SC-1); instalación visible en el panel y revocable |
| T-I3 [US1] | **Aceptación P1 con Codex** (`gpt-5.6`) en el mismo Mac | `qa-engineer` + dueño | 10 de 10 (SC-2) |
| T-I4 | **Barrido de credenciales** en ficheros del arnés y en la configuración generada | `security-engineer` | Cero credenciales de proveedor en `.mcp.json`, `config.toml` y en el almacén del lanzador (SC-4) |
| T-I5 | **Revisión** contra `spec.md`, `plan.md`, la constitución y el modelo de amenazas | `code-reviewer` + `security-engineer` | Veredicto con referencias a secciones; ninguna violación del Principio 0 (la pasarela vive en Enterprise, no en el shell-server) |

Orden: T-I1 en paralelo desde T-B5; T-I2 → T-I3 tras cerrar A, B y C; T-I4 y T-I5 antes de
abrir `mcp.safent.app` al público.
