# 032 — Plan de ejecución (v2, 14-sep-2026 tarde)

Alcance en `spec.md`. Esta versión sustituye a la de la mañana tras la petición del dueño: **el panel web se vincula con la instalación en el momento de instalar el MCP, y Enterprise también**. La forma más simple que lo consigue es un servidor MCP remoto con OAuth: el vínculo es el login. Todo lo que sigue se apoya en capacidades documentadas de Claude Code y de Codex a esta fecha; lo no verificado está marcado.

## 1. Arquitectura

```
Claude Code / Codex ──MCP remoto (HTTPS + OAuth)──▶ Safent Cloud ──▶ companion Anuncios (por organización)
        │                                            │  cuentas · organizaciones · instalaciones
        │                                            │  políticas · aprobaciones · auditoría · relé
        │                                            └──▶ panel web (instalaciones, aprobaciones, Anuncios)
        └──(opcional, por política)──▶ runtime local (jaula: hooks, proxy, contenedor) ──código──▶ Safent Cloud
```

| Componente | Estado | Dónde vive |
| --- | --- | --- |
| **Safent Cloud**: cuentas (personales y de empresa), organizaciones, instalaciones, políticas, aprobaciones, auditoría, relé | Existe como Enterprise (falta desplegar y generalizar a cuentas personales) | `lumen-control-enterprise` |
| **Companion Anuncios alojado** por organización | Existe (single-owner); multi-organización pendiente (ADS-02) | `safent-ads` |
| **Servidor MCP remoto `mcp.safent.app/mcp`** con OAuth: expone las herramientas de la organización con política y auditoría | Nuevo | `lumen-control-enterprise/src/safent_control/mcp_gateway/` |
| **Plugin de Claude Code / perfil de Codex**: registro del servidor remoto y login en un comando | Nuevo | repo `safent-plugins` + `safent-runtime/ops/harness/` |
| **Runtime local** (jaula: hooks, proxy, contenedor) emparejado por código | Existe (CLI de la app); hooks nuevos | `safent-runtime` |

### Decisiones

1. **El vínculo es el login.** `claude mcp add --transport http safent https://mcp.safent.app/mcp` + `claude mcp login safent` (Codex: `[mcp_servers.safent] url = …, auth = "oauth"` + `codex mcp login safent`). El OAuth aterriza en Safent Cloud; al completarlo se crea la **instalación** (id único, nombre del equipo y del harness, persona, organización) y aparece en el panel. Revocable desde la web. Sin códigos que copiar.
2. **Enterprise se vincula solo.** El administrador invita por correo; cuando la persona hace login desde su harness, la instalación queda ligada a su puesto, con cuentas asignadas, capacidades y políticas de la organización. Las cuentas personales son una organización de una persona: mismo código, mismo panel.
3. **Anuncios en la nube.** El companion corre en Safent Cloud por organización; instalar en Claude Code o Codex no requiere podman, imágenes ni máquina virtual. Friendog = una organización desplegada hoy; varias organizaciones exigen cerrar ADS-02 (aislamiento multi-organización del companion).
4. **Confirmación nativa para dinero y publicación.** Herramientas de escritura con `_meta["anthropic/requiresUserInteraction"]` (Claude Code 2.1.199+) y `tools.<tool>.approval_mode = "approve"` (Codex). Además, la política de la organización puede exigir **aprobación en el panel** (web o móvil): la herramienta devuelve «pendiente» y el panel la resuelve por el relé. Doble vía.
5. **Código de emparejamiento solo para lo que no tiene navegador.** El runtime local (jaula) y los servidores se emparejan con `safent pair <código>` (existe): la web muestra el código, el lanzador lo consume.
6. **Panel gráfico = panel web** (y la ventana de Safent para la app propia). En Claude Desktop y ChatGPT se incrusta el mismo panel como MCP App más adelante. Claude Code y Codex no admiten paneles de terceros.
7. **Sin secretos en ficheros.** OAuth del harness: el token lo guarda el propio harness en su almacén; el companion nunca ve credenciales de proveedor fuera del broker. Nada en `.mcp.json` ni en `config.toml` salvo la URL.

### Verificado y por verificar

Verificado (documentación del 14-sep): `claude mcp add --transport http` con OAuth y `claude mcp login`; MCP gestionado por la organización (`managedMcpServers`, `allowedMcpServers`); plugins (manifiesto, `.mcp.json`, `hooks/hooks.json`, `bin/`, marketplaces, `claude plugin validate`); hooks (`PreToolUse` con `permissionDecision`, `PostToolUse`, `Elicitation`, salida 2 bloquea, hooks gestionados); Agent SDK (`canUseTool`, modos, marca `requiresUserInteraction` que fuerza confirmación); Codex `codex mcp add`, `[mcp_servers.<name>]` con `url`, `auth = "oauth"`, `bearer_token_env_var`, `default_tools_approval_mode` y `tools.<tool>.approval_mode`, `codex mcp login`, `requirements.toml` gestionado, protocolo app-server con aprobaciones.

Por verificar antes de la tarea que lo use: valores exactos de `approval_policy` y `sandbox_mode` de Codex; si Codex honra el proxy para MCP; si la app de escritorio de Codex admite interfaces de terceros; límites de tokens OAuth de Claude Code para servidores remotos propios (registro dinámico de cliente).

## 2. Fases y tareas

### Fase 1 — Instalar, vincular y operar Anuncios desde Claude Code y Codex (P1) · 1–2 semanas

| # | Tarea | Repo · ficheros | Prueba |
| --- | --- | --- | --- |
| 1.1 | **Desplegar Safent Cloud para Friendog**: Enterprise + companion en la VM, HTTPS y subdominio, secretos de servicio, copias y salud (bloque DEPLOY-01 pendiente) | `lumen-control-enterprise` (`deploy.sh`, Dockerfile), `safent-ads` (compose) | health, backup/restore, `curl` HTTPS desde fuera |
| 1.2 | **Cuentas personales y organizaciones**: alta con correo, organización de una persona por defecto, invitación por correo a una empresa | `lumen-control-enterprise/src/safent_control/{accounts,orgs}/` | unit + integración: alta, invitación, dos organizaciones aisladas |
| 1.3 | **OAuth para harnesses**: servidor de autorización (registro dinámico de cliente, PKCE, refresh, revocación) que emite tokens ligados a persona + organización + **instalación**; alta automática de la instalación en el primer login con nombre del equipo y del harness | `lumen-control-enterprise/src/safent_control/oauth/` | conformidad OAuth 2.1 con `claude mcp login` y `codex mcp login` reales; revocar desde la web corta el acceso |
| 1.4 | **Servidor MCP remoto** `/mcp` (streamable HTTP): expone las herramientas del companion de la organización y las de gobierno (`safent_status`, `safent_panel`), esquemas anidados expandidos, `_meta.anthropic/requiresUserInteraction` en escrituras, política de la organización antes de reenviar, auditoría por instalación | `lumen-control-enterprise/src/safent_control/mcp_gateway/` | `tools/list` y `tools/call` reales desde Claude Code y Codex; política deniega antes de reenviar |
| 1.5 | **Panel web: instalaciones y aprobaciones**: lista de instalaciones por persona/organización, estado, herramientas, revocar; bandeja de aprobaciones resuelta por el relé; auditoría; Anuncios (panel actual del companion servido por la nube) | `lumen-control-enterprise/frontend/` | test por vista; aprobación desde la web resuelve una herramienta «pendiente» en Claude Code |
| 1.6 | **Plugin y perfil de un comando**: plugin `safent` en el marketplace (`.mcp.json` con el servidor remoto, `/safent:panel`, `/safent:status`, `/safent:pair`), y `safent codex install` que escribe `[mcp_servers.safent]` con `auth = "oauth"` y los modos de aprobación | `safent-plugins`, `safent-runtime/ops/harness/codex.py` | `claude plugin validate`; `claude plugin eval` con el recorrido P1; `codex mcp list` |
| 1.7 | **Conectar cuentas desde el chat**: `connect_account` devuelve el enlace de consentimiento del companion (redirect al panel web); `list_platform_accounts` | `safent-ads` (existente) + gateway | proveedor falso en integración; Google real con el dueño |
| 1.8 | **Recorrido de aceptación P1** con Claude Code y con Codex en un Mac limpio, sin runtime local: instalar → login → cuentas → dos borradores → aprobar una con freno → ver en el panel web | — | 10 de 10 (SC-2) |

### Fase 2 — Gobierno local del harness (P2) · 1 semana

| # | Tarea | Repo · ficheros | Prueba |
| --- | --- | --- | --- |
| 2.1 | **Runtime local emparejado por código**: `safent pair <código>` (existe) liga la jaula local a la instalación; la web genera el código en la ficha de la instalación | `safent-runtime` (`safent`), `lumen-control-enterprise` (emisión de códigos, existe) | dos instalaciones, dos códigos, revocación |
| 2.2 | **`safent policy check`** para hooks: decisión permitir/preguntar/denegar con razón según la política de la organización, cerrada por defecto ante error | `safent-runtime/src/hermes/gateway_mcp/hook.py` | tabla de decisiones; `rm -rf` y dominio no permitido → deny |
| 2.3 | **Hooks del plugin** (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SessionStart`, `Elicitation`) → policy y auditoría | `safent-plugins/safent/hooks/hooks.json` | escenarios de 2.2 con el plugin cargado |
| 2.4 | **Codex**: `approval_policy`, `sandbox_mode` y `requirements.toml` según la política (valores por verificar); documentar lo no gobernable | `safent-runtime/ops/harness/codex.py` | «solo lectura» deniega escrituras |
| 2.5 | **Proxy de salida** con lista blanca cuando la política lo exige | `safent-runtime` | dominio fuera de lista → denegado y auditado |

### Fase 3 — Encargos y multi-organización (P3) · 1–2 semanas

| # | Tarea | Repo · ficheros | Prueba |
| --- | --- | --- | --- |
| 3.1 | **Encargos a un puesto**: Equipo encarga; la instalación con runtime local ejecuta una sesión sin ventana del harness (Agent SDK con `canUseTool`; Codex app-server) con admisiones y aprobaciones; resultado por `/v1/outbox/result` | `safent-runtime/src/hermes/gateway_mcp/delegate.py`, Enterprise `GET /api/tasks` (028) | entregado → admitido → ejecutado → devuelto; caída antes y después del acuse |
| 3.2 | **Multi-organización del companion** (ADS-02): referencias con organización y conexión, IAM por asignación firmada, freno y frecuencia por cuenta física | `safent-ads` | dos organizaciones, misma cuenta remota, aislamiento |
| 3.3 | **Sin claves de modelo en Enterprise** para instalaciones de harness externo | `lumen-control-enterprise/llm/*` | una instalación externa nunca recibe permiso de inferencia |
| 3.4 | **MCP Apps** para Claude Desktop y ChatGPT con el panel actual (opcional) | `safent-ads/panel` | panel embebido en una conversación |

## 3. Orden y ritmo

- Recorridos, no bloques. El primero: 1.1→1.3→1.4→1.6→1.8 con Claude Code y Friendog; Codex cuando salga a la primera.
- Tres carriles: nube (1.1–1.3), gateway MCP + panel (1.4–1.5), plugin/perfil (1.6). Un integrador.
- El pipeline de escritorio no interviene en la fase 1 (no hay app que construir para este camino).
- Cada corte verificable: commit, informe en `specs/032/`, actualización de `SAFENT-PENDIENTES.md`.

## 4. Riesgos declarados

- Un servidor MCP remoto propio exige un servidor OAuth correcto (registro dinámico, PKCE, revocación); es la pieza de seguridad nueva y se revisa como tal antes de exponerla.
- Hasta cerrar ADS-02, Safent Cloud sirve a una organización por despliegue: Friendog primero.
- Los hooks gobiernan pero no aíslan; aislar el propio harness es posterior a la fase 2.
- La app propia con Hermes y este camino comparten companion, políticas y auditoría; ninguna corrección se duplica.
