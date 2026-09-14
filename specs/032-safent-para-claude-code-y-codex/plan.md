# 032 — Plan de ejecución

Fecha: 14 de septiembre de 2026. Alcance en `spec.md`. Todo lo que sigue se apoya en capacidades documentadas de Claude Code y de Codex a esta fecha; lo que no pude verificar está marcado «por verificar».

## 1. Arquitectura

```
Claude Code / Codex  ──MCP (stdio)──►  safent mcp (lanzador, agregador)  ──►  motor Safent (contenedor)
        │  hooks / approval policy                 │  políticas · auditoría · sesión      │  companion Anuncios
        │                                          └──► ventana Safent (panel, aprobaciones, ajustes)
        └──HTTPS_PROXY──►  proxy de salida de Safent (lista blanca)
```

Cuatro componentes, dos ya existen:

| Componente | Estado | Dónde vive |
| --- | --- | --- |
| **Lanzador `safent`** (CLI + runtime empaquetado: podman propio, imágenes por digest, máquina virtual) | Existe: es el CLI de la app nativa, probado en el Mac del dueño | `safent-runtime` (`./safent`, `desktop/scripts/stage-runtime.sh`) |
| **Motor + companion** (jaula, políticas, auditoría, freno, propuestas) | Existen y se publican por digest | `safent-runtime`, `safent-ads` |
| **Agregador MCP `safent mcp`** (un servidor por stdio; agrega motor y companion; aplica políticas; sesiones cortas) | Nuevo | `safent-runtime/src/hermes/gateway_mcp/` |
| **Plugin de Claude Code y perfil de Codex** (registro del MCP, hooks, comandos, ajustes) | Nuevo | repo `safent-plugins` (marketplace) + `safent-runtime/ops/harness/` |

### Decisiones

1. **Un solo servidor MCP hacia fuera.** El harness ve `mcp__safent__*`. El agregador reenvía a los servidores internos (motor y companion) con el bearer de sesión, aplica la política del puesto antes de reenviar y escribe cada llamada en la auditoría. Los servidores internos nunca se registran en el harness.
2. **Confirmación nativa para dinero y publicación.** En Claude Code, las herramientas de escritura llevan `_meta["anthropic/requiresUserInteraction"]`, que las hace pasar siempre por la confirmación aunque haya reglas de permiso (Claude Code 2.1.199+). En Codex, el lanzador escribe `tools.<tool>.approval_mode = "approve"` para esas mismas herramientas y `default_tools_approval_mode = "prompt"` para el resto.
3. **Gobierno de las herramientas nativas por hooks**, no por parche: `PreToolUse` → `safent policy check` → `permissionDecision` allow / ask / deny con razón; `PostToolUse` y `PostToolUseFailure` → auditoría. Salida 2 del hook bloquea aunque el JSON falle: fail-closed.
4. **Sesiones cortas, sin secretos en ficheros.** El plugin registra el servidor como `stdio` con `command: safent mcp`; el lanzador obtiene el bearer del motor en el momento (`safent url` ya lo acuña) y lo usa solo en memoria. Ningún token acaba en `.mcp.json`, `~/.claude.json` ni `config.toml`.
5. **Panel = ventana de Safent.** `/safent:panel` abre la app en la sección pedida. En Claude Desktop y ChatGPT, el mismo panel se sirve como MCP App (fase posterior).
6. **Codex y Claude Code comparten el 90 %.** El lanzador y el agregador son idénticos; cambian solo el registro (plugin frente a `codex mcp add`) y el mecanismo de gobierno nativo (hooks frente a `approval_policy`/`sandbox` y modos de aprobación por herramienta).

### Lo verificado y lo pendiente de verificar

Verificado en la documentación del 14-sep: estructura y distribución de plugins de Claude Code (`.claude-plugin/plugin.json`, `skills/`, `agents/`, `hooks/hooks.json`, `.mcp.json`, `bin/`, `settings.json`; `claude --plugin-dir`, marketplaces, `claude plugin validate`); eventos y contrato de hooks (`PreToolUse` con `permissionDecision`, `PostToolUse`, `PermissionRequest`, `Elicitation`, `SessionStart`; exit 2 = bloqueo; hooks gestionados de empresa); `claude mcp add` stdio/http, ámbitos, `.mcp.json`, OAuth, MCP gestionado; Agent SDK con `canUseTool`, modos de permiso y hooks; `codex mcp add`, claves de `[mcp_servers.<name>]`, `default_tools_approval_mode` y `tools.<tool>.approval_mode`, `codex mcp login`, `requirements.toml` con `allow_managed_hooks_only`, y la existencia del protocolo app-server (JSON-RPC por stdio/websocket, `thread/start`, `turn/start`, aprobaciones).

Por verificar antes de la tarea que lo use: valores exactos de `approval_policy` y `sandbox_mode` de Codex (la página pública devolvió 404 hoy; se toman del repositorio `openai/codex`); si Codex honra `HTTPS_PROXY` para las herramientas MCP; si las apps de escritorio de Codex admiten interfaces de terceros (su app-server menciona «apps alojadas»).

## 2. Fases y tareas

Cada tarea nombra su repositorio, sus ficheros y su prueba. Ninguna fase se da por cerrada sin el recorrido de aceptación de su historia en un Mac limpio.

### Fase 1 — Instalar y operar Anuncios desde Claude Code y Codex (P1) · 1 semana

| # | Tarea | Repo · ficheros | Prueba |
| --- | --- | --- | --- |
| 1.1 | **Verbo `safent mcp`**: servidor MCP por stdio que (a) arranca el runtime si no está (reutiliza `stage-runtime` + `up`), (b) acuña el bearer de sesión, (c) descubre las herramientas del motor y del companion, (d) las expone con prefijo único, esquemas anidados ya expandidos y `_meta.anthropic/requiresUserInteraction` en las de escritura, (e) reenvía cada llamada tras consultar la política del puesto y la escribe en la auditoría | `safent-runtime`: `safent` (verbo), `src/hermes/gateway_mcp/{server.py,catalog.py,policy.py,session.py}` | unit: catálogo agregado, prefijo, `_meta` en escrituras, política deniega antes de reenviar; integración: `tools/list` y `tools/call` reales contra motor + companion en contenedor |
| 1.2 | **Arranque en segundo plano con progreso**: `safent mcp` responde al `initialize` de inmediato con una herramienta `safent_status`; las demás aparecen (notificación `tools/list_changed`) cuando el runtime está listo; mientras, cualquier llamada devuelve el estado y el porcentaje | `safent-runtime`: `gateway_mcp/server.py`, `safent` (`facts --json`, eventos de progreso ya existentes) | integración: primer arranque en frío desde el MCP en un Mac limpio (`podman machine` creado por el lanzador) |
| 1.3 | **Plugin de Claude Code**: repo `safent-plugins` con marketplace `.claude-plugin/marketplace.json` y plugin `safent/` con `plugin.json`, `.mcp.json` (`safent` → `stdio`, `command: safent mcp`), `bin/safent` (el lanzador), `skills/panel/SKILL.md` (`/safent:panel`), `skills/status/SKILL.md`; instalación con `/plugin marketplace add devwspito/safent-plugins` + `/plugin install safent@safent-plugins` | nuevo repo `safent-plugins` | `claude plugin validate`; `claude --plugin-dir` en el Mac; `claude plugin eval` con los 6 pasos de P1 |
| 1.4 | **Perfil de Codex**: `safent codex install` escribe `[mcp_servers.safent]` (stdio, `command`, `startup_timeout_sec`), `default_tools_approval_mode = "prompt"`, `tools.<escritura>.approval_mode = "approve"`; `safent codex uninstall` lo retira sin tocar el resto del `config.toml` | `safent-runtime`: `ops/harness/codex.py`, verbo en `safent` | unit: edición idempotente del TOML; integración: `codex mcp list` ve `safent` y `codex` completa P1 |
| 1.5 | **OAuth de cuentas desde el chat**: herramienta `connect_account` que devuelve el enlace de consentimiento del companion y otra `list_platform_accounts` ya existente; el callback del OAuth aterriza en el companion por el puente (ya derivado del origen del navegador) | `safent-ads`: `broker/application/oauth_connect_flow.py` (sin cambios previstos), `safent-runtime`: `gateway_mcp/catalog.py` | integración con proveedor falso; manual con Google real en el Mac del dueño |
| 1.6 | **Distribución del lanzador**: paquete `safent` con el binario y el runtime por plataforma (macOS arm64, Linux x64/arm64) firmado; `npx safent` y `brew install devwspito/tap/safent`; versión mínima exigida por el plugin | `agents-autonomy` (pipeline: nuevo trabajo `launcher`), `safent-runtime/ops/release/` | pipeline en modo `release_artifacts`; `safent --version`; instalación limpia en el Mac |
| 1.7 | **Recorrido de aceptación P1** en el Mac del dueño, con Claude Code y con Codex, grabado en `specs/032/verificacion-p1-*.md` | — | 10 de 10 con cada harness (SC-2) |

### Fase 2 — Gobierno de las herramientas nativas (P2) · 1 semana

| # | Tarea | Repo · ficheros | Prueba |
| --- | --- | --- | --- |
| 2.1 | **`safent policy check`**: lee la petición del hook por stdin (`tool_name`, `tool_input`, `cwd`, `session_id`), consulta la política del puesto (la misma que usa la jaula: `tool_policy`, catálogo de riesgo) y responde `permissionDecision` allow/ask/deny con `permissionDecisionReason`; salida 2 en cualquier error | `safent-runtime`: `src/hermes/gateway_mcp/hook.py`, verbo en `safent` | unit: tabla de decisiones por política; `rm -rf` y `curl` a dominio no permitido → deny; edición de fichero en el proyecto → allow; comando desconocido → ask |
| 2.2 | **Hooks del plugin**: `hooks/hooks.json` con `PreToolUse` (`Bash|Write|Edit|WebFetch|mcp__.*`) → `safent policy check`; `PostToolUse` y `PostToolUseFailure` → `safent audit record`; `SessionStart` → `safent session start` (inyecta contexto del puesto); `Elicitation` para registrar confirmaciones | `safent-plugins/safent/hooks/hooks.json` | `claude --plugin-dir` + escenarios de 2.1; auditoría firmada en el motor |
| 2.3 | **Codex**: el lanzador escribe `approval_policy` y `sandbox_mode` según la política del puesto (valores exactos: por verificar en `openai/codex`), y en `requirements.toml` de empresa `allow_managed_hooks_only`; documentar qué NO se puede gobernar en Codex por falta de hooks | `safent-runtime/ops/harness/codex.py` | integración: política «solo lectura» deniega escrituras en Codex |
| 2.4 | **Proxy de salida**: el lanzador arranca el harness con `HTTPS_PROXY`/`NO_PROXY` apuntando al proxy de Safent cuando la política lo exige; comprobar que Claude Code y Codex lo honran (por verificar en Codex) | `safent-runtime`: `safent` (`launch` verbo), `ops/agents-os-edition/netns/` | integración: dominio fuera de lista → conexión denegada y auditada |
| 2.5 | **Ventana de Safent para Claude Code**: `/safent:panel` abre la app (o la instala si no está) en la sección pedida; la app detecta que el harness es externo y oculta su chat propio | `safent-runtime/desktop/`, `frontend/` | test de la cáscara: deep-link por sección; manual en el Mac |

### Fase 3 — Enterprise con puestos Claude Code o Codex (P3) · 1–2 semanas

| # | Tarea | Repo · ficheros | Prueba |
| --- | --- | --- | --- |
| 3.1 | **Emparejar el puesto**: `safent pair <código>` (existe) aplica al agregador la política, cuentas y capacidades asignadas; el agregador expone solo lo permitido | `safent-runtime`: `gateway_mcp/policy.py`, `shell_server/pairing*` | integración: dos puestos, dos asignaciones, catálogos distintos |
| 3.2 | **Encargos**: el lanzador recibe el encargo por la bandeja existente y ejecuta una sesión sin ventana del harness (Claude Code: Agent SDK con `canUseTool` → admisión y aprobaciones del puesto; Codex: app-server `thread/start` + `turn/start` con aprobaciones) y devuelve el resultado por `/v1/outbox/result` | `safent-runtime`: `src/hermes/gateway_mcp/delegate.py`; `lumen-control-enterprise`: read-model `GET /api/tasks` (pendiente de 028) | integración: encargo entregado → admitido → ejecutado → resultado; crash antes y después del ack |
| 3.3 | **Sin claves de modelo en Enterprise**: retirar de Enterprise la herencia LLM para puestos de este tipo; el puesto entra con la cuenta del proveedor; Enterprise solo registra qué proveedor y qué plan | `lumen-control-enterprise`: `llm/*` (gate cerrado se queda para la app Hermes) | unit: un puesto «harness externo» no recibe grant de inferencia |
| 3.4 | **Actualización**: el plugin exige versión mínima del lanzador; el lanzador se actualiza por paquete y las imágenes por `runtime-manifest.json` firmado (existe) | `safent-plugins`, `safent-runtime/ops/release/` | instalación vieja + versión nueva publicada → actualización sin pasos |

### Fase 4 — Paneles incrustados donde existan (opcional)

MCP Apps para Claude Desktop y ChatGPT con el panel actual del companion. Solo si P1–P3 están cerradas.

## 3. Orden y ritmo

- Se avanza por recorridos, no por bloques: 1.1→1.2→1.3→1.7 primero con Claude Code; Codex (1.4) entra cuando P1 sale a la primera con Claude Code.
- Máximo tres carriles en paralelo: agregador (1.1–1.2), plugin y perfil (1.3–1.4), lanzador y distribución (1.6). Un integrador.
- El pipeline de escritorio solo se lanza con certeza (5,38 €/pasada). Las pruebas se hacen en local (contenedores en la DGX) y en el Mac del dueño por ssh, como hasta ahora.
- Cada corte verificable: commit, informe en `specs/032/`, y actualización de `SAFENT-PENDIENTES.md` en Enterprise.

## 4. Riesgos que no se ocultan

- **Confirmaciones dentro del chat**: en Claude Code están garantizadas por `requiresUserInteraction`; en Codex dependen de `approval_mode = "approve"` por herramienta (documentado). Si un cliente futuro ignorara la marca, el companion sigue exigiendo su propia aprobación: la doble vía se conserva.
- **Sandbox real de los comandos del harness**: los hooks gobiernan pero no aíslan; el aislamiento del propio Claude Code o Codex (ejecutarlos dentro del contenedor) es un paso posterior a P2.
- **Cuentas del proveedor**: el usuario entra con su cuenta de Anthropic u OpenAI; Safent no la ve ni la guarda. Si una empresa exige facturación centralizada, es el plan de equipo del proveedor, no Safent.
- **Dos productos con el mismo runtime**: la app Hermes y este camino comparten motor, companion, políticas y auditoría; toda corrección del companion (como las de hoy) sirve a ambos.
