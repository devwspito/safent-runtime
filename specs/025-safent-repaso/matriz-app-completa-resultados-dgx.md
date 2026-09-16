# 025 — Resultados de la matriz completa (168 filas) sobre la DGX

Ejecución 2026-09-10 en el host DGX (`linux/aarch64`, podman **rootless** 4.9.3) contra las
imágenes finales: runtime `localhost/safent-runtime:latest` (== `:0.8.42`, label
`org.opencontainers.image.revision=607c98e`) y companion `localhost/safent-ads:local`
(safent-ads `main` 17b586e), seleccionado automáticamente por `run-safent.sh` al no exportar
`SAFENT_ADS_IMAGE`.

Instancia única `matriz-final-1` (volumen `matriz-final-1-data`, puerto `127.0.0.1:18090`,
estado del companion en el scratchpad de la sesión, **nunca** `~/.safent`), lanzada con:

```sh
SAFENT_NAME=matriz-final-1 SAFENT_VOLUME=matriz-final-1-data \
SAFENT_COMPANION_STATE=$SCRATCH/safent-state/companions/ads \
./ops/container/run-safent.sh localhost/safent-runtime:latest 18090
```

Estado del host antes de empezar: `podman ps -a` sólo con tres contenedores `safent-*`
apagados de pasadas antiguas (`safent-diag`, `safent-v839`, `safent-audit` — no tocados) y
`podman network ls` **sin** `safent-companions`: no había companion ni red ajena que
respetar, así que la instancia de esta pasada es la única que existe.

Auth de la API: `GET /app/?k=$(podman exec matriz-final-1 cat
/var/lib/hermes-bootstrap/bootstrap/webui-bootstrap)` → bearer inyectado en
`window.__SAFENT_TOKEN__`. Ninguna credencial real en toda la pasada (proveedores, Composio,
Brave, Tailscale, Google/Meta: sólo valores falsos o `[DUEÑO]`).

**Leyenda:** `PASS` · `FALLA` · `[DUEÑO]` (necesita credencial/cuenta real del dueño) ·
`NO-APLICA-DGX` (sólo tiene sentido en macOS/Windows/Tauri/instalador). El orden de las
secciones es el **orden de ejecución** (destructivo al final), no el orden del documento de
la matriz; cada fila lleva su id.

---

## §0 Instalación y arranque (INST)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| INST-01 | **PASS** | `run-safent.sh localhost/safent-runtime:latest 18090` + `curl localhost:18090/healthz` | Provisionado del companion OK (`ads-migrate` `ExitCode=0`, los 4 servicios `Up`, `ads-db`/`ads-api` `healthy`); `podman run` devolvió 16:28:20, `Startup finished in 2.520s`, `Started hermes-runtime.service` 16:28:22, `/healthz` **200 al primer sondeo** (16:28:28, +8 s) |
| INST-02 | **PASS** | `podman exec matriz-final-1 systemctl is-system-running` / `--failed` | `running`, **0 unidades failed**; 20 unidades `hermes-*` cargadas: `hermes-runtime`, `hermes-shell-server`, `hermes-egress-proxy`, `hermes-browser-netns`, `hermes-mcp-launcher`, `hermes-companion-egress`, `hermes-exec-launcher`, `hermes-audit-tail`, `hermes-host-firewall`, `hermes-keygen`, 4 `hermes-mcp-*`, y los 3 `.path` (config-sync, tailscaled, tailscale-control) `active waiting` |
| INST-03 | **PASS** | `cat /sys/kernel/security/lsm`; `systemctl show hermes-landlock-assert -p Result`; `printenv HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE` | LSM = `lockdown,capability,landlock,yama,apparmor,ima,evm`; `Result=success`, `ExecMainStatus=0`; journal: `hermes-landlock-assert: Landlock LSM activo — OK`, `landlock_loader.applied abi=7 rules=13 mask=0x17bf`, `runtime_landlock.applied outcome=applied enforcing=True`, `confinement_check.PASS`. Sin variable de degradación. Caso negativo (kernel sin Landlock) **no ejecutable** en esta máquina: un único kernel |
| INST-04 | **PASS** | `curl "localhost:18090/app/?k=<webui-bootstrap>"` | 200 y `window.__SAFENT_TOKEN__="f111b6ac…"` (64 hex) inyectado en `index.html`; el bearer funciona en todas las llamadas posteriores |
| INST-05 | **PASS** | `curl -i localhost:18090/api/v1/agents` y `.../api/v1/runtime/agent-stream` sin cabecera | ambas **401**; con bearer, `/agents` → 200. WebSockets sin token: ver `SEG-VNC` en §Seguridad |
| INST-06 | **PASS** | `GET /api/v1/mcp` tras el arranque | `safent-ads` con `health:"healthy"`, **`tool_count: 61`**, `companion_status:"listo"`; `argv` con `mcp-remote@0.8.6`; journal `hermes.dbus.companion_seed_imported slug=safent-ads` a los ~2 s del arranque. Semillas de oficina: excel 25, word 54, powerpoint 37, todas `healthy` |
| INST-07 | **PASS** | ver §Recreación final (`--no-companion` sobre el mismo nombre, tras el desmontaje de la instancia principal) | fila resuelta al final del informe |
| INST-08 | **PASS** | ver §Recreación final (`--codex-auth` con `auth.json` dummy) | fila resuelta al final del informe; con cuenta real de Codex sería `[DUEÑO]` |

**Smoke** (INST-01/02/04/05 + CHAT-01 + AGT-01 + MCP-01 + SEG-01/18): todo verde a los 8 s
del arranque — `GET /security/kill-switch` → `{"engaged":false}`, `GET /egress/mode` →
`{"mode":"deny"}` (deny-by-default confirmado en instalación nueva), `GET /agents/roster` →
departamentos poblados, `GET /instance/features` → `edition:"community"`.

## §2 Chat (CHAT)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| CHAT-01 | **PASS** (con matiz) | `POST /api/v1/chat {"user_message":"hola matriz"}` sin proveedor + `frontend/src/views/ChatView.tsx:431-447` | `NoModelBanner` (`role="alert"`) con `chat.nomodel.text` + CTA que navega a `/proveedores`. El turno **sí** se acepta (200 `task_id`) y el stream termina `outcome:"failed"` con `HermesModelNotConfiguredError: HERMES_MODEL no está definido…`: el compositor no se desactiva (`canSend` no mira el proveedor). Ningún proveedor contactado (no hay ninguno), pero el usuario ve un error técnico en vez de un bloqueo |
| CHAT-02 | **[DUEÑO]** (mecánica **PASS**) | `POST /chat` → `GET /chat/stream/{task_id}?token=…` | SSE completo con ids: `id:1 {"kind":"status","status":"in_progress"}` → `id:2 {"kind":"done","outcome":"failed"}`. La respuesta real de un modelo exige clave/cuenta del dueño |
| CHAT-03 | **[DUEÑO]** | requiere stream vivo de un modelo real | sin modelo el stream muere en <1 s; no hay ventana donde pulsar `chat.stop` |
| CHAT-04 | **[DUEÑO]** | idem (reconexión con `Last-Event-ID` a media respuesta) | el protocolo numera eventos (`id:` incremental, visto arriba), que es la precondición del fix; la reanudación real necesita un turno largo |
| CHAT-05 | **PASS** | `POST /api/v1/workspace/files` (multipart) | 201 `{"name":"f1.txt","path":"/var/lib/hermes/workspace/f1.txt","size":47}`; visible en `GET /workspace/files` |
| CHAT-06 | **[DUEÑO]**/parcial | límite de adjunto | el error inline existe (`chat.err.attach`, `ChatView.tsx:720`); no se ejercitó con un fichero por encima del tope (no hay tope declarado en el cliente, el rechazo es del backend) |
| CHAT-07 | **NO-APLICA-DGX** | File System Access API | no hay navegador Chrome/Edge en el host |
| CHAT-08 | **NO-APLICA-DGX** | idem con Firefox | — |
| CHAT-09 | **[DUEÑO]** | delegación a especialista | necesita modelo real que emita `delegate_task` |
| CHAT-10 | **[DUEÑO]** | chip "usando el navegador" | necesita modelo real que llame `browser_navigate` |
| CHAT-11 | **PASS** (código) | `frontend/src/components/Layout.tsx:405-432` | la lista recorta y pinta `layout.recents.more`/`layout.recents.less` con `aria-expanded`; `GET /chat/conversations` devuelve las conversaciones creadas en esta pasada |
| CHAT-12 | **PASS** (código) | `Layout.tsx:507-512` | botón `layout.new_chat` presente en el sidebar |

## §3 Agentes (AGT)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| AGT-01 | **PASS** | `GET /api/v1/agents/roster` | 200, departamentos agrupados (`cerebro`/CEO con el agente `default`) |
| AGT-02 | **PASS** | `GET /api/v1/agents/default-roster` | `{"enabled":false}` en fresco (CE siembra sólo `default`) |
| AGT-03 | **PASS** | `POST /api/v1/agents {"name":"Matriz Tester","department":"cerebro"}` | 200 `agent_id=7fb30686762f40b8807ebeed43a41d31` |
| AGT-04 | **PASS** | `PATCH /api/v1/agents/{id} {"name":"Matriz Tester 2"}` | 200 con el nombre nuevo |
| AGT-05 | **PASS** | ver §Destructivo | `DELETE /agents/{id}` |
| AGT-06 | **PASS** (por diseño, no hay activo global) | `POST /agents/{id}/activate` → `GET /agents/active` | activate → `{"ok":true,"active_agent_id":"7fb3…","deprecated":true}`; `GET /agents/active` → **200** `{"active_agent_id":"","deprecated":true}` (antes 405 — hallazgo #7 cerrado). Ambos son no-ops declarados: el binding es por conversación (`agents_api.py:195-210`), así que la UI nunca podrá "conocer el agente activo" por esta vía |
| AGT-07 | **PASS** | `GET /api/v1/runtime/agent-stream?token=…` | SSE empuja `RuntimeSnapshot` completo (`runtime.state`, `active_task_count`, `stats.agents[]`) sin polling |
| AGT-08 | **NO-APLICA-DGX** | backoff de reconexión con throttling de DevTools | sin navegador |
| AGT-09 | **NO-APLICA-DGX** | piso pixel (canvas) | sin navegador |
| AGT-10 | **PASS** | `POST /api/v1/tasks/scheduled {"label":"matriz-cron","instruction":"di hola","cron":"* * * * *"}` | 201 `{"ok":true,"trigger_id":"d3dff588-d5d0-47e7-bc22-9286df0f0cd6"}` (UUID completo) |
| AGT-11 | **PASS** | `GET /tasks/configured` → `GET /tasks/scheduled/{trigger_id}` | la lista devuelve **el mismo UUID** y el detalle responde **200** (dead-end lista→detalle sigue cerrado) |
| AGT-12 | **PASS** | `POST /tasks/scheduled/{id}/enabled {"enabled":false\|true}` | 200 `{"ok":true}` en ambos sentidos (nada de `{"ok":false}` bajo 200) |
| AGT-13 | **PASS** | ver §Destructivo | `DELETE /tasks/scheduled/{id}` |
| AGT-14 | **PASS — regresión R14 CERRADA** | dejar disparar el cron 2 min → `GET /api/v1/tasks/recent` y `GET /tasks/configured` | `/tasks/recent` ya **no** está vacío: 3 entradas con `label`, `trigger_kind:"timer"`, `enqueued_at`; `configured` trae `last_run_at:"2026-09-10T14:31:22.517712+00:00"`, `last_status:"pending"` y `next_run_at` **avanzando** (14:33). Journal: 2× `hermes.triggers.timer.fired`, `tasks.loop.task_claimed`/`task_failed` (falla por no haber modelo, esperado). Matiz: `last_status` se queda en `pending` aunque el bucle ya reportó `task_failed` — el estado terminal no vuelve a la fila |

## §4 Habilidades (SKL)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| SKL-01 | **PASS** | `GET /api/v1/skills/hub/search?q=git` | 200 con 8+ resultados (`official/research/gitnexus-explorer`, `skills-sh/dalestudy/skills/git`…) |
| SKL-02 | **PASS** | `POST /skills/hub/install {"identifier":"skills-sh/dalestudy/skills/git","force":false}` | 202 `{"op_id":…}` → `GET /skills` lista `native:git` (`signed_at` 14:33:32) |
| SKL-03 | **PASS** | mismo POST con `official/research/gitnexus-explorer` | 202 `{"ok":false,"blocked":true,"score":30,"verdict":"FAIL","risks":[…privilege escalation…, CRITICAL executes raw contents…]}` — sin `force` no continúa |
| SKL-04 | **PASS** (backend) — ver §Seguridad para el flujo de UI con UN solo TOTP | `force:true` sin MFA | **403** `{"code":"mfa_not_enrolled"}` (el bypass del hallazgo #4 sigue cerrado) |
| SKL-05 | **PASS** | ver §Destructivo | `DELETE /skills/hub/{name}` |
| SKL-06 | **PASS** | `GET /api/v1/skills/native:git/details` | 200 con el paquete completo |
| SKL-07 | **PASS** (parcial, no ejercitable) | `POST /skills/native:git/promote {"confirm":true}` | 404 `skill not found` con el id que usa la UI (`package_id`), **pero** el botón "Promover" sólo se pinta si `isValidated` (`SkillsView.tsx:708`) y una skill `native` no lo está: no hay dead-end alcanzable desde la UI. Sin skill `validated` (la única fábrica era enseñar por navegador, retirada) la fila no es ejercitable |
| SKL-08 | **[DUEÑO]** | `skills.verify` inserta un turno de chat | necesita modelo real |
| SKL-09 | **NO-APLICA — capacidad retirada** | `POST /api/v1/training`, `/api/v1/training/{id}/start`, `/teach/vnc`; grep del bundle | las tres rutas → **404**; **cero** referencias a `TeachModal`/`startTeaching`/`skills.teach` en `frontend/src`; `/app/ensenar` redirige a `/capacidades?tab=en-vivo` (`App.tsx:109`). Coincide con `retirada-ensenar.md` |
| SKL-10 | **NO-APLICA — capacidad retirada** | idem | no hay sesión que abandonar; `GET /skills` no deja huérfanas |

## §5 Integraciones (INTG)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| INTG-01 | **PASS** | `GET /api/v1/integrations/composio/status` en fresco | `{"has_key":false,"enabled":false,"entity_id":"default"}` — la UI no llama a `connected`/`toolkits` en ese estado |
| INTG-02 | **[DUEÑO]** | clave Composio real | — |
| INTG-03 | **[DUEÑO]** | OAuth de un toolkit real | — |
| INTG-04 | **[DUEÑO]** | depende de INTG-03 | — |
| INTG-05 | **PASS** | `GET /api/v1/web-search/status` | `{"brave":false,"tavily":false,"exa":false,"ddgs_fallback":true}` |
| INTG-06 | **[DUEÑO]** | clave Brave real | — |
| INTG-07 | **PASS** | `POST /web-search/key {"provider":"brave","api_key":""}` | **422** `string_too_short` (el backend también valida); la UI corta antes con `int.brave.err.enter_key` |
| INTG-08 | **[DUEÑO]** | fallback DDGS desde el chat | necesita modelo activo que dispare `web_search` |

## §6 Herramientas / MCP (MCP)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| MCP-01 | **PASS** | `GET /api/v1/mcp` | excel 25 / word 54 / powerpoint 37 `healthy` + `safent-ads` 61 `healthy` `companion_status:"listo"` |
| MCP-02 | **PASS** | `POST /api/v1/mcp {"server_id":"memory","argv":["npx","-y","@modelcontextprotocol/server-memory"]}` | **201 en 7 s** `{"ok":true,"tool_count":9}`; la lista lo da `healthy` con **11** tools (misma discrepancia 9≠11 de las dos pasadas anteriores) |
| MCP-03 | **PASS** | `GET /api/v1/mcp/registry?q=memory&limit=5` | 200 con resultados externos (`ai.justonce/memory`, con `unsupported_reason` cuando sólo es remoto) |
| MCP-04 | **FALLA — el EXDEV del hallazgo #5 SIGUE ABIERTO** | `POST /api/v1/mcp {"server_id":"time","argv":["uvx","mcp-server-time"]}` (paquete nunca cacheado, volumen nuevo) | 400 `prefetch falló … (rc=1): Failed to download 'tzlocal==5.4.4' … failed to rename file from /var/lib/hermes/uv-cache/.tmpHyj2Z5 to /var/lib/hermes/uv-cache/archive-v0/xaY-8j_kZQF9Yebj: Invalid cross-device link (os error 18)`. Contexto medido dentro de la jaula: `UV_CACHE_DIR=/var/lib/hermes/uv-cache`, `TMPDIR=/var/lib/hermes/tmp`, ambos en `/dev/nvme0n1p2` según `df` — el rename falla igualmente. **Ningún MCP de Python nuevo es instalable** |
| MCP-05 | **FALLA** | `POST /api/v1/mcp` con `env` (el propio ejemplo del formulario) | el `<textarea>` de `mcp.env.label` sugiere `BRAVE_API_KEY=br-xxx` (`McpView.tsx:1148`) y el backend responde **400** `clave de env no permitida: 'SECRET_TOKEN' (allowlist: ['ADS_BEARER','CONTEXT7_API_KEY','HOME','MCP_REMOTE_CONFIG_DIR','NODE_EXTRA_CA_CERTS','OD_*','OPENAI_*','REPLICATE_API_TOKEN','XDG_CONFIG_HOME'])` — `BRAVE_API_KEY` **no** está en `_MCP_BYOK_ENV_KEYS` (`dbus_runtime_service.py:7141-7168`). Quien siga el ejemplo de la UI recibe un error crudo del backend |
| MCP-06 | **PASS** | `POST /mcp {"argv":["bash","-c","echo hi"]}` | **400** `runner 'bash' no permitido (allowlist: ['npx','pipx','uvx'])` |
| MCP-07 | **PASS** | `POST /mcp/managed-remote/no-existe/connect` | **400** `slug 'no-existe' no es un servidor MANAGED_REMOTE conocido` |
| MCP-08 | **PASS** (por el companion, no por la URL manual) | companion arriba + `POST /mcp/managed-remote/safent-ads/connect {"url":"https://ads.safent.internal:8443/mcp"}` | el seed ya deja `safent-ads` conectado con 61 tools y la entrada "Anuncios" del sidebar; el connect manual con **esa misma URL** responde 400 `managed_remote endpoint must use port 443 (got 8443)` — el camino manual sólo sirve para un self-hosted en 443 |
| MCP-09 | **PASS** (validación) / **[DUEÑO]** (éxito) | `POST … {"url":"http://…"}` | **400** `managed_remote endpoint must use https:// (got 'http://')`; conectar de verdad exige un MCP de Ads propio del dueño |
| MCP-10 | **PASS** | `DELETE /api/v1/mcp/memory` y `/mcp/gh-test` | **204** en ambos; desaparecen de la lista |
| MCP-11 | **PASS** | `GET /mcp` sobre una entrada con env | el payload devuelve `argv`/`health`/`tool_count`; no expone valores de env en claro |
| MCP-12 | **PASS** | `POST /api/v1/security/scans/install {"kind":"mcp","identifier":"npx:evil-dropper-mcp"}` | 200 `score:45, verdict:"WARN", requires_owner_approval:true, engine:"trivy"` con riesgos CVE listados |

## §7 En vivo (LIVE)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| LIVE-01 | **PASS** | `GET /api/v1/runtime/status` en fresco + `EnVivoView.tsx:75-79` | `{"state":"idle","activity":[],"browser_live":false}`; la vista pinta `envivo.no_tasks` y **no** monta `VncFrame` |
| LIVE-02 | **[DUEÑO]** | requiere modelo real que dispare `browser_navigate` | el frame se monta sólo con `status.browser_live === true` (`EnVivoView.tsx:40-44`), no por el nombre de la tool |
| LIVE-03 | **[DUEÑO]** | idem con destino bloqueado por egress | misma condición de código: sin página real `browser_live` sigue `false`, el frame no aparece |
| LIVE-04 | **PASS** | `POST /api/v1/tasks/{task_id}/cancel` sobre una tarea de cron encolada | 200 `{"ok":true,"requested":true}` |
| LIVE-05 | **PASS** | `grep -c teach frontend/src/views/EnVivoView.tsx` | **0** coincidencias: la vista es sólo monitor + detener, coherente con la retirada de "enseñar" |

## §8 Seguridad (SEG) y §19 SSH gobernado

| id | resultado | cómo | evidencia |
|---|---|---|---|
| SEG-01 | **PASS** | `GET /api/v1/security/kill-switch` en fresco | `{"engaged":false,"reason":null,…}` |
| SEG-02 | **PASS** | `POST /security/kill-switch {"engaged":true,"reason":"matriz: prueba de freno"}` **sin MFA enrolado** | 200 `{"ok":true,"engaged":true}`; el estado devuelve `reason`, `changed_by`, `changed_at` |
| SEG-03 | **PASS** | `POST /api/v1/chat` con el freno activo | **423** `{"code":"kill_switch_engaged","message":"El freno de emergencia está activo — libéralo desde Seguridad para enviar mensajes."}` |
| SEG-04 | **PASS** | `POST /security/kill-switch {"engaged":false,"totp":<válido>}` | 200 `{"ok":true,"engaged":false}`; el chat vuelve a admitir turnos (200) inmediatamente después |
| SEG-05 | **PASS** (código) | `frontend/src/components/KillSwitchBanner.tsx:51-54` | banner global con texto de alerta y `<Link to="/sistema?tab=seguridad">Liberar</Link>` |
| **Liberación SIN MFA (hallazgo C)** | **FALLA (parcial — sigue sin salida en instalación nueva)** | con MFA **no** enrolado: `POST … {"engaged":false}` y `{"engaged":false,"device_password":"contrasena-falsa-matriz"}` | el camino nuevo existe y llega de verdad al helper root (`security_api.py:117-166` → `hermes-tailscale-control` acción `kill_switch_release`), pero ambas respuestas son **403** `invalid_device_password` y el journal explica por qué: `WARNING hermes-tailscale-control: cuenta hermes-user sin contraseña válida (passwordless/locked) — gate FAIL-CLOSED (configura una en el onboarding)` + `kill_switch_release: PAM verification FAILED`. En una instalación nueva **nadie ha puesto contraseña de dispositivo**, así que la única salida real sigue siendo enrolar MFA con el freno echado (`POST /mfa/enroll` **sí** funciona frenado) y soltar con TOTP. Cero fugas: `grep` del journal por la contraseña falsa → 0 |
| SEG-06 | **[DUEÑO]** | `GET /api/v1/approvals/pending` | `[]` 200 — la tarjeta sólo la genera una propuesta de tool de un modelo real |
| SEG-07 | **[DUEÑO]** | idem | — |
| SEG-08 | **PASS** | `POST /api/v1/mfa/enroll {"totp":null}` (con el freno activo) | 200 `otpauth://totp/Safent:owner?secret=…&issuer=Safent&algorithm=SHA1&digits=6&period=30`; `GET /mfa/status` → `{"enrolled":true}`. Matiz: el enrolado es **inmediato** en la primera llamada (`approvals_api.py:82-92`) y la UI (`MfaEnroll.tsx`) sólo confirma en local — nadie comprueba que el dueño llegara a escanear el QR |
| SEG-09 | **PASS** | `POST /security/kill-switch {"engaged":false,"totp":"000000"}` | **401** `{"code":"invalid_totp"}` |
| SEG-10 | **PASS** | reenviar el TOTP ya gastado | **401** `{"code":"totp_replayed"}`; con un código nuevo → 200 |
| SEG-11 | **[DUEÑO]** | `GET /api/v1/inbound-delegations` | `[]` 200; exige un segundo Safent emparejado |
| SEG-12 | **PASS** | `POST /policies/preset` con `totp:""` y con TOTP válido | sin código **401** `invalid_totp`; con código 200 `{"ok":true,"preset":"bloqueado"}` y el catálogo pasa a **0 de 88 tools activas**; vuelta a `equilibrado` 200 |
| SEG-13 | **PASS** (código) | `SeguridadView.tsx:502-503,480-483` | los toggles se acumulan en `toolPending` y aparece la barra "Guardar cambios"/"Descartar" |
| SEG-14 | **PASS** | `POST /policies/tools {"tools":{"web_search":false},"totp":<válido>}` | 200 `{"ok":true,"count":1}`; `GET /policies` confirma `web_search:false` y persiste |
| SEG-15 | **FALLA (dead-end)** | `POST /policies/mfa_on_dangers {"enabled":false,"totp":<válido>}` → luego los **cuerpos exactos** que manda la UI cuando `mfaDisabled` (`SeguridadView.tsx:622-671`) | desactivar la verificación devuelve 200 `{"mfa_on_dangers":false}`, pero a partir de ahí **el backend sigue exigiendo TOTP** y la UI ya no lo pide: `POST /policies/preset {"preset":"permisivo","totp":""}` → **401**, `POST /policies/tools {…,"totp":""}` → **401**, y —lo peor— `POST /policies/mfa_on_dangers {"enabled":true,"totp":""}` → **401**: ni siquiera se puede volver a **encender** la verificación desde la UI. El dueño se queda con presets, lote de permisos y el propio interruptor rotos, con un toast de error y sin ningún sitio donde teclear el código |
| SEG-16 | **PASS** (código) | `seg.changes.discard` | vuelve al estado persistido sin llamada de red |
| SEG-17 | **PASS** (código) | `SeguridadView.tsx:517-539,854-861` | `defenseGroups` (categoría `security`) se pinta aparte del catálogo de capacidades |
| SEG-18 | **PASS** | `GET /api/v1/egress/mode` recién arrancado | `{"mode":"deny","description":"deny: only explicitly allowed domains reachable"}` — deny-by-default sigue cerrado |
| SEG-19 | **PASS** | `POST /egress/mode` con `totp:""` **teniendo `mfa_on_dangers:false`** y con TOTP | sin código **401** `Cambiar el modo de red exige tu código MFA` (no hay bypass, tal como dice el diseño); con código 200 `{"ok":true,"mode":"allow","pushed":true}` y vuelta a `deny` 200 |
| SEG-20 | **PASS** | `POST /egress/deny/add {"domain":"example.org"}` en modo `allow`, **sin** TOTP | 200 `{"ok":true,"denylist":["example.org"],"pushed":true}` |
| SEG-21 | **PASS** | `POST /egress/domains/grant` con dominio válido e inválido | `example.com` → 200 `{"domains":["example.com"],"pushed":true}`; `"no es dominio"` → **422** `{"code":"invalid_domain"}` (nada de `{ok:false}` bajo 200) |
| SEG-22 | **PASS** | `GET /api/v1/egress/domains` | `{"mode":"deny","domains":["example.com"],"denylist":[],"blocklist_count":0}` — el contador existe y viene del backend (0 en esta instalación) |
| SEG-23 | **PASS** | `GET /api/v1/tailnet` en fresco | `{"configured":false,"online":false,"node_name":null,…,"last_attempt":null}` |
| SEG-24 | **PASS — hallazgo D CERRADO** (éxito real: **[DUEÑO]**) | `POST /api/v1/tailnet/connect {"auth_key":"tskey-auth-FAKEmatrizfinal-noreal-…"}` y sondeo de `GET /tailnet` cada 15 s | 202 `{"staged":true}`; a los ~45 s el helper agota los reintentos y el estado pasa a `{"configured":false,"online":false,"last_attempt":{"at":"…14:47:42…","ok":false,"error_kind":"tailscale_up_failed"}}` — ya **no** miente con `configured:true`. La UI mapea ese estado a `failed` (`SeguridadView.tsx:1402-1406`). `grep` del journal por la clave falsa → **0**; `/run/hermes/tailscale-control/` vacío (shred) |
| SEG-25 | **PASS** (mecanismo) / **[DUEÑO]** (éxito) | `POST /api/v1/tailnet/disconnect {"password":"password-falsa-matriz-2"}` | 200 `{"staged":true}`; el helper root responde `disconnect action: PAM verification FAILED for user 'hermes-user' — aborting` y aborta sin tocar el marker; 0 apariciones de la contraseña en el journal |
| SEG-26 | **PASS** | `POST /api/v1/security/scans/install {"kind":"skill",…}` → `POST /api/v1/security/decisions {…,"decision":"allow","totp":<válido>}` | 201 `{"ok":true,"reauth_grant":"q7TOulZnKI3I…"}`; `GET /security/scans` deja la fila `official/research/gitnexus-explorer FAIL ALLOWED`; `GET /security/audit/head` con `integrity:"present"` |
| **SKL-04 (flujo de UI, UN solo TOTP)** | **PASS — hallazgo A CERRADO** | secuencia exacta de `SkillsView.handleScanApprove`: `scans/install` → `security/decisions {…,totp}` → `POST /skills/hub/install {"identifier":"official/research/gitnexus-explorer","force":true}` con cabecera `X-Owner-Reauth-Grant: <reauth_grant>` y **sin** segundo TOTP | **202** `{"op_id":…}` → `GET /skills/hub/ops/{id}` → `{"status":"done"}`; `GET /skills` lista `native:gitnexus-explorer`. El 401 `invalid_totp` de R11 ya no ocurre |
| SEG-WS | **PASS** | handshake WebSocket a `/api/v1/watch/agent/live` y `/api/v1/vnc` sin token, con token malo y con el bearer | sin token → **403**, token malo → **403**, bearer válido → **101 Switching Protocols** en ambos |
| SSH-01 | **PASS** | `podman exec … which ssh && ssh -V` | `/usr/bin/ssh`, `OpenSSH_9.6p1 Ubuntu-3ubuntu13.19` — el `openssh-client` del follow-up de `ssh-v2.md` ya está horneado |
| SSH-02 | **PASS — el GAP está cerrado** | dentro de la jaula: `build_capability_tool_specs(broker=…, consent_context=…)` y `GET /api/v1/policies` | el esquema de tools del LLM trae **24** capacidades e incluye `tailnet_ssh`, `tailnet_file_get`, `tailnet_file_put`; el catálogo de políticas (88 tools, preset `equilibrado`) también las lista. Ya no es cierto que "el agente no puede invocarlo" |
| SSH-03 | **PASS — el GAP está cerrado** | `GET /api/v1/tailnet/ssh-hosts`; `DELETE /api/v1/tailnet/ssh-hosts/{host}` | `{"hosts":[]}` 200; el DELETE valida el TOTP (`totp:""` → **422** `string_too_short`) y la UI lo consume (`client.ts:795-802`, `SshHostsSection`) |

## §9 Coste (COST)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| COST-01 | **PASS** | `GET /api/v1/usage/summary?period=7d\|30d\|mtd` | los tres devuelven su periodo; tras los turnos de §10: `{"period":"7d","cycles":7,"self_hosted_cycles":7,"top_models":[{"model":"gemini/gemini-2.5-…"}]}`. Matiz: un `period` inválido (`day`) no da 422, se coerciona en silencio a `30d` |
| COST-02 | **PASS** | `GET /usage/by-agent?period=7d` | `{"agents":[{"agent_id":"default","name":"CEO","cycles":7,…}]}` |
| COST-03 | **PASS** | `GET /usage/timeseries?period=7d&dimension=cost` | 200 con `points` (vacío mientras el coste es 0 — claves placeholder) |
| COST-04 | **PASS** | `GET /chat/conversations/{id}/usage` | 200 con los ciclos y **el modelo real usado**: `{"model":"gemini/gemini-2.5-flash","cost_usd":0.0,…}` — esta ruta fue la que permitió comprobar el enrutado de §10 |

## §10 Modelo de IA / Proveedores (PROV)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| PROV-01 | **PASS** | `GET /api/v1/providers/native` | **51** ids (`nous`, `openai-codex`, `openai-api`, `xai-oauth`, `qwen-oauth`, `gemini`, `zai`, `lmstudio`, `copilot`…) |
| PROV-02 | **FALLA** (SDKs sí, modelo no) | el cuerpo **exacto** de la UI: `configureNativeProvider({provider_id, api_key})` (`ProvidersView.tsx:536-538`, sin `model` ni `set_active`) → `POST /providers/{id}/activate` → turno de chat | SDKs presentes (`anthropic 0.87.0`, `openai 2.24.0`, `boto3`) — la mitad del hallazgo #2 sigue cerrada. **Pero** al no mandar `model`, `native_providers.json` guarda `""` y `_write_hermes_model_config` deja `config.yaml` con `model.provider: anthropic` y **sin `model.default`**; el primer turno muere con `HermesModelNotConfiguredError: HERMES_MODEL no está definido`. Confirmado dentro de la jaula (`cat /var/lib/hermes/hermes-home/config.yaml`). Sólo mandando `model` a mano por API (`{"provider_id":"gemini","model":"gemini-2.5-flash","set_active":true}` → 201 `{"ok":true}`) queda utilizable |
| PROV-03 | **FALLA** | `POST /api/v1/providers/anthropic/test` (id nativo, el que manda la UI: `testProvider(created.provider_id)`) | **200** `{"ok":false,"error":"daemon_unavailable"}` — `test_provider` hace `UUID(provider_id)` (`dbus_runtime_service.py:6528+`) y un id nativo no lo es. Como la UI activa **sólo si `r.ok === true`** (`ProvidersView.tsx:545-556`), conectar un proveedor nativo desde la tarjeta **siempre** acaba en "conexión fallida", aun con clave válida. Además el `{ok:false}` viaja bajo HTTP 200 |
| PROV-04 | **PASS** (parte a: no pisa el activo) | `POST /providers/native {"provider_id":"gemini",…,"set_active":true}` → `{"provider_id":"anthropic",…,"set_active":false}` → `GET /providers/native/active` | el activo sigue siendo `gemini` (`default_model: gemini-2.5-flash`, `is_active:true`). También: `set_active:true` **sin** `model` responde **201 con `{"ok":false,"error":"model requerido para activar"}`** (un `{ok:false}` bajo 2xx, el anti-patrón que el resto de la API ya no usa) |
| PROV-05 | **FALLA (regresión parcial frente a R5)** | activar `anthropic` (200, `config.yaml` = `provider: anthropic` + `default: claude-sonnet-4-5`, `GET /providers/native/active` = anthropic) y lanzar turnos a distintos retardos, mirando `GET /chat/conversations/{id}/usage` | activación 16:55:36 → turno a **+2 s** = `gemini/gemini-2.5-flash` (el proveedor VIEJO); turno a **+71 s** = `anthropic/claude-sonnet-4-5` (journal: `provider=anthropic model=claude-sonnet-4-5 … HTTP 401: invalid x-api-key`). Repetido al revés: activar gemini y turno a **+35 s** → ya gemini. El cambio es efectivo sin reiniciar, pero **no en el turno inmediatamente siguiente**: hay una ventana de ~30 s en la que el motor sigue sirviendo con el proveedor anterior, justo lo que R5 daba por cerrado ("mismo segundo") |
| PROV-06 | **PASS** | `DELETE /api/v1/providers/{uuid}` | **204** sobre el proveedor custom creado en PROV-10 |
| PROV-07 | **PASS** (hasta el login) | `POST /api/v1/providers/openai-codex/oauth/start` | 200 `{"flow":"device_code","user_code":"5LL1-MBLGR","verification_url":"https://auth.openai.com/codex/device","expires_in":900,"poll_interval":5}`; completar el login es `[DUEÑO]` |
| PROV-08 | **[DUEÑO]** | esperar los 900 s del device code | no ejecutado: consume el flujo OAuth de la cuenta del dueño |
| PROV-09 | **PASS** (código) | `ProvidersView.tsx:97-104,565-568` | detecta `{ok:false,error:"oauth_required"}` y ofrece `providers.oauth.fallback_notice` |
| PROV-10 | **PASS** | `POST /api/v1/providers` sin y con `default_model` | sin él → **422** `Field required`; con él → **201** `{"provider_id":"05e9afae-…","kind":"openai_compatible","default_model":"qwen"}` |
| PROV-11 | **[DUEÑO]** | requiere instancia emparejada | — |
| PROV-12 | **PASS** | `podman restart matriz-final-1` | `/healthz` 200 a los **3 s** (restart 9 s), mismo bearer; sobreviven proveedor activo (`gemini`), `egress deny`, MFA, las 2 skills, el cron y los MCP. Matiz: `safent-ads` vuelve `disconnected`/`tool_count 0` durante ~1 min y se recupera solo (`healthy`, 61, `listo`) |

## §11 Memoria (MEM)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| MEM-01 | **PASS** | `GET /api/v1/memory` | `[]` 200 — sin turnos con modelo real no hay hechos que recordar |
| MEM-02 | **PASS** | `GET /memory/search?q=safent` y sin `q` | con `q` → 200 `[]`; sin `q` → **422** |
| MEM-03 | **PASS** (camino negativo) | `GET /memory/noexiste:0` | **404** `{"code":"not_found"}` |
| MEM-04 | **[DUEÑO]** | necesita una entrada real | `PUT` sobre id inexistente → 400 `update_failed` |
| MEM-05 | **[DUEÑO]** | guard de PII/inyección | el `PUT` con `DNI 12345678Z` + tarjeta de prueba muere antes en `entry not found`; sin entrada real no se alcanza el guard |
| MEM-06 | **FALLA (menor)** | `DELETE /api/v1/memory/noexiste:0` | **200** sobre un id que no existe (mientras `GET` da 404 y `PUT` 400): borrar algo inexistente se reporta como éxito |

## §12 Archivos (FILES)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| FILES-01 | **PASS** | `GET /api/v1/workspace/files` | 200, árbol con `name/kind/path/size/modified` |
| FILES-02 | **PASS** | `GET /workspace/files?path=.` | 200 con el listado del subdirectorio |
| FILES-03 | **PASS** | `POST /workspace/files` multipart | **201** `{"name":"f1.txt","path":"/var/lib/hermes/workspace/f1.txt","size":47}` |
| FILES-04 | **PASS** | tres `POST` secuenciales (lo que hace la UI, `ok += 1` por fichero) | 201/201/201 y los tres en la lista; con nombre repetido desambigua (`m3 (1).txt`). Los tres en **una sola** petición multipart sólo guardan el último — el endpoint acepta un `file` por llamada |
| FILES-05 | **PASS** | `GET /workspace/download?path=f1.txt` | 200 y `md5sum` idéntico al original (`6c94dee50ed20e7f62419535a0039faf`) |
| FILES-06 | **PASS** | `GET /workspace/download` sin `path` | **422** |

## §13 Anuncios (ADS) — incluye las superficies nuevas de la spec 026

| id | resultado | cómo | evidencia |
|---|---|---|---|
| ADS-01 | **PASS** | ver §Recreación final (`--no-companion`) | fila resuelta al final |
| ADS-02 | **FALLA (bloqueante de US1 / SC-002)** | `POST /api/v1/ads/bridge/session` (bearer) → `GET /ads/` con la cookie | la cookie se emite bien (`set-cookie: ads_bridge=65b4218d…; HttpOnly; Max-Age=2592000; Path=/ads; SameSite=strict`) y el cuerpo dice `{"status":"unavailable","reason":"no_accounts"}`, pero **`GET /ads/` responde 503** `{"error":{"code":"ADS_SESSION_UNAVAILABLE"}}`. Causa raíz confirmada en vivo: `busctl … MintCompanionOwnerAssertion s "safent-ads"` (como uid `hermes`) → `Call failed: SSO private key at /etc/hermes/companions/ads-sso.key could not be loaded (PermissionError)`. El bind es `-r-------- root root` y el daemon corre como `User=hermes` (uid 880). El **bearer** del companion sí tiene su stage-in root (`ops/agents-os-edition/scripts/hermes-companion-bearer` → `/run/hermes/companions/safent-ads.bearer` `0440 root:hermes`, journal `1 bearer(s) staged`), pero **la clave SSO de 026 no tiene equivalente**: se lee directa del montaje. Ningún `POST /api/v1/auth/exchange` llega jamás al companion (`podman logs safent-ads-ads-api-1`: sólo `/mcp` y `/api/v1/health`). Mismo patrón que el bug histórico de `leaf.key` en `fresh-install-verification.md` |
| ADS-03 | **[DUEÑO]** | OAuth de Google Ads / Meta Ads dentro del panel | además, hoy inalcanzable por ADS-02 |
| ADS-04 | **[DUEÑO]** | requiere modelo real + cuentas conectadas | — |
| ADS-05 | **PASS** (código) | `AdsView.tsx:47-92` | estados honestos por `reason` (`ads.state.<reason>.title/desc` + reintento + CTA) y fallback `ads.iframe.fallback` con enlace de apertura directa |
| **026 · entrada "Ads" en el sidebar** | **PASS** | `Layout.tsx:448,572` (`useAdsAvailability` + `<AdsNavItem>`) y `GET /api/v1/mcp` | la entrada es **incondicional** (ya no depende de `useAdsPanelOrigin`) y el hook resuelve `loading → ready \| unavailable(reason)`. Con el companion arriba, `companion_status` pasa de ausente a `listo` en ~5 s desde el arranque; `GetCompanionHealth` devuelve `{"state":"no_accounts","reachable":true,"http_status":200,"contract_version":"1.0.0","accounts_linked":{"google":false,"meta":false}}` — estado honesto, no pantalla vacía |
| **026 · `/ads/` mismo origen, cero inicios de sesión (SC-002)** | **FALLA** | 20 aperturas no llegan a intentarse: la primera ya da 503 | por ADS-02. Lo que **sí** cumple el puente: sin cookie → **401**; `/ads/mcp` → **403**; `/ads/api/v1/auth/login` y `/ads/api/v1/auth/totp` → **403** (nada de formulario de acceso embebido); ninguna respuesta reenvía `Set-Cookie: ads_session` al navegador (0 coincidencias) |
| **026 · `GET /api/v1/cockpit` por el puente** | **FALLA** | `GET /ads/api/v1/cockpit` con la cookie de puente | **503** `{"error":{"code":"ADS_SESSION_UNAVAILABLE"}}` — misma causa raíz (la petición nunca sale hacia el companion) |

## §14 CLI `safent` (CLI) y §18 Copias (BKP)

Ejecutado desde el host con `SAFENT_NAME=matriz-final-1`, `SAFENT_DATA_VOLUME=matriz-final-1-data`,
`SAFENT_PORT=18090`, `SAFENT_IMAGE=localhost/safent-runtime:latest` y `HOME` redirigido al
scratchpad (para no tocar `~/.safent`, que en esta máquina sólo tenía un `safent-seccomp.json`
previo, intacto al terminar).

| id | resultado | cómo | evidencia |
|---|---|---|---|
| CLI-01 | **PASS** | `./safent open` | imprime `Safent is ready: http://localhost:18090/?k=b21d2ca…`; sin navegador en el host, no revienta |
| CLI-02 | **PASS** | `./safent url` | una sola línea con la URL y `?k=` en stdout, progreso a stderr |
| CLI-03 | **PASS** | `./safent start` | arranca sin abrir navegador; `/healthz` 200 a los 2 s |
| CLI-04 | **PASS** | `./safent stop` | `[ok] Safent stopped` en **7 s**, `Exited (0)` — parada ordenada, sin el SIGKILL de la 1ª pasada |
| CLI-05 | **PASS** | `./safent restart` | stop+open, `/healthz` 200 a los 14 s |
| CLI-06 | **PASS** | `./safent status` | `[ok] Safent running at http://localhost:18090/` |
| CLI-07 | **PASS** | `./safent logs` | `podman logs -f` en streaming |
| CLI-08 | **FALLA** | `./safent companion status` con los 5 contenedores del companion `Up`/`healthy` | responde `containers: 0/0 running` y `/mcp/health: unreachable`. Causa raíz reproducida: `_companion_container_counts` llama a `podman compose … ps -q -a` **sin** `_companion_env`, así que la interpolación falla (`required variable ADS_POSTGRES_PASSWORD is missing a value`) y la lista sale vacía — con el env exportado, la misma orden devuelve los contenedores. Además, en podman **rootless** la sonda `/mcp/health` del CLI no puede alcanzar `10.201.0.10:8443` desde el host (red del companion en su propio netns), así que siempre dirá "unreachable" |
| CLI-09 | **[DUEÑO]** | `safent companion update` (pull + recreate) | no ejecutado: haría `pull` de la imagen publicada del dueño y recrearía el companion; el defecto de imagen que lo rompe está medido en CLI-10 |
| CLI-10 | **FALLA (grave)** | `./safent companion rotate` sobre un companion sano | rota los ficheros del host y luego **rompe el companion**: `ads-migrate` sale `Exited (255)` con `FAILED: Can't locate revision identified by '0033_crm_bridge_health'` y `ads-api` se queda en `Created` (caído); el CLI acaba en `[x] Bearer/SSO files updated but ads-api restart failed — restart it by hand`. Causa raíz: `_companion_env()` fija `SAFENT_ADS_IMAGE="${SAFENT_ADS_IMAGE:-ghcr.io/devwspito/safent-ads:latest}"` mientras que `run-safent.sh` aprovisiona con `safent-ads:local` si existe → el rotate **descarga y mezcla otra imagen** (`podman inspect safent-ads-ads-migrate-1` → `ghcr.io/devwspito/safent-ads:latest`, mientras `ads-worker` seguía en `localhost/safent-ads:local`), y esa imagen publicada no conoce la cabeza de alembic que la local ya aplicó. Recuperado a mano con `podman compose … up -d` y `SAFENT_ADS_IMAGE=localhost/safent-ads:local` (migrate exit 0, `ads-api healthy`). Efecto colateral observado: mientras `ads-api` estuvo caído, `GET /api/v1/mcp` siguió diciendo `healthy`/`listo`/61 tools |
| CLI-11 | **PASS** | ver §Destructivo | — |
| CLI-12 | **PASS** | ver §Destructivo | — |
| CLI-13 | **PASS** | `./safent --no-companion status` | acepta la bandera como primer argumento y salta el provisioning |
| CLI-14 | **PASS** | `./safent pair CODIGO-FALSO-MATRIZ` | `[x] Pairing failed: Error de red al contactar el control plane.` — error claro, sin dejar estado |
| CLI-15 | **PASS** | `./safent unpair` | `[ok] Not associated — nothing to unpair` + `[ok] Instance unpaired`; `GET /instance/status` sigue `community` |
| BKP-01a (`safent backup`) | **PASS** | `./safent backup $SCRATCH/out/backups` | para la instancia, exporta volumen + estado del companion + caché de seccomp y la rearranca; `safent-backup-20260910T150842Z.tar.gz` de **157 MB** en 21 s, `0600` |
| BKP-01b (`safent restore`) | **FALLA (grave, silenciosa)** | `./safent --no-companion restore <archivo>` con `SAFENT_NAME=matriz-final-2` | imprime `[ok] Restored from …` **pero no crea ningún contenedor**: `podman ps -a` no lista `matriz-final-2` tras un restore "correcto". Causa raíz reproducida con `sh -x`: `cmd_restore` importa primero el volumen y después llama a `cmd_start`, cuyo `_exists()` hace `podman inspect "$NAME"` — y esa orden **casa con el VOLUMEN** (`podman inspect matriz-final-2` → `"Name": "matriz-final-2-data"`), así que el CLI cree que la instancia ya existe, hace `podman start matriz-final-2` (falla, silenciado con `\|\| true`) y anuncia `[ok] Safent started`. El dueño se queda sin instancia y con un mensaje de éxito. Reproducible también con `./safent --no-companion start` a secas sobre un nombre que sólo tiene volumen |
| BKP-01c (datos del backup) | **PASS** | arrancando a mano el volumen restaurado (`run-safent.sh … 18091 --no-companion`) | `/healthz` 200 a los 2 s y **todo el estado viaja**: las 2 skills (`native:git`, `native:gitnexus-explorer`), el cron con su UUID, el proveedor activo (`gemini/gemini-2.5-flash`), `mfa.enrolled:true`, `egress deny`, los MCP de oficina y **el mismo bearer** (misma `master.key`). `restore` repetido sin `--force` → se niega correctamente: `Volume … already exists — refusing to overwrite it` |
| BKP-02 | **PASS** | equivalente `podman volume export/import` (lo que hace `safent backup` por dentro) | verificado en BKP-01c: el volumen importado conserva providers, skills, MCP, memoria y cron |

## §15 Desktop shell (DESK) — 4 filas

| id | resultado | motivo |
|---|---|---|
| DESK-01 | **NO-APLICA-DGX** | no hay build de Tauri ni sesión gráfica en este host |
| DESK-02 | **NO-APLICA-DGX** | idem (webview nativo) |
| DESK-03 | **NO-APLICA-DGX** | idem (`__safentLatestVersion` lo inyecta el shell nativo) |
| DESK-04 | **NO-APLICA-DGX** | idem (comando de portapapeles `#[tauri::command]`) |

## §16 Distribución (DIST)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| DIST-01 | **NO-APLICA-DGX** | `get-safent.sh` en VM Linux limpia | no hay VM limpia; ejecutarlo aquí instalaría el CLI en el PATH del host y tiraría de la imagen publicada |
| DIST-02 | **NO-APLICA-DGX** | macOS Apple Silicon | — |
| DIST-03 | **NO-APLICA-DGX** | macOS Intel | — |
| DIST-04 | **NO-APLICA-DGX** | Windows + WSL2 | — |
| DIST-05 | **PASS (contradicción documental confirmada)** | `docker --version`; `docker run --rm --systemd=always alpine true` | Docker **29.2.1** presente en el host: `docker run --help` no lista `--systemd` (0 coincidencias) y el intento real responde `unknown flag: --systemd`. `run-safent.sh` es, por tanto, **podman-only**; el README que ofrece "Podman o Docker Desktop" sigue siendo incorrecto |
| DIST-06 | **PASS** | toda esta pasada corrió con **podman rootless** (`podman info … Rootless=true`, uid 1000) | arranque, Landlock (`enforcing=True`), seccomp de PID1 (`Seccomp: 2`), netns del navegador/MCP, nftables, companion y CLI funcionan igual que en las verificaciones rootful previas. Única diferencia observada: desde el host **no** se alcanza `10.201.0.10:8443` (red del companion en el netns del usuario) — de ahí el "unreachable" de CLI-08 |

## §17 Enterprise (ENT)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| ENT-01 | **PASS** | `GET /api/v1/instance/features` y `/instance/status` | `edition:"community"`, `associated:false`, 10 vistas expuestas |
| ENT-02 | **[DUEÑO]** | requiere código de tenant + control plane alcanzable | `safent pair` con código falso da error de red claro (CLI-14) |
| ENT-03 | **[DUEÑO]** | depende de ENT-02 | — |
| ENT-04 | **PASS** (camino inerte) | `./safent unpair` sin emparejar | `[ok] Not associated — nothing to unpair`; sigue `community` |

## §1 Actualización / Desinstalación (UPD)

| id | resultado | cómo | evidencia |
|---|---|---|---|
| UPD-01 | **PASS** | `POST /api/v1/system/update` | 200 `{"ok":true,"updating":true}`; `ls /var/lib/hermes/instance/` muestra `.update-requested` (17:13) |
| UPD-02 | **[DUEÑO]** | `safent agent` consumiendo el marcador | no ejecutado: haría `podman pull` de la imagen publicada y recrearía el contenedor; además esta máquina ya tiene un `safent-agent.service` de usuario ajeno a esta pasada |
| UPD-03 | **PASS** | `GET /api/v1/system/update` cada 2 min durante 18 min | `updating:true` a las 17:19/17:21/17:23/17:25/17:27 y **`updating:false` a las 17:29** — exactamente el `_FLAG_STALE_S = 15*60` de `system_update.py:37`, se limpia solo sin agente |
| UPD-04 | **PASS** | `POST /api/v1/system/uninstall` | 200 `{"ok":true}` y aparece `.uninstall-requested` junto al marcador de update |
| UPD-05 | **[DUEÑO]** | `safent update` | no ejecutado a propósito: hace `_self_update` desde GitHub, `podman pull` de `ghcr.io/devwspito/safent:latest` **y `_reclaim_space` (rmi de imágenes safent viejas del host)** — tocaría imágenes que no son de esta pasada |
| UPD-06 | **[DUEÑO]** | `safent uninstall` | **no ejecutado, y es un hallazgo en sí**: el verbo no está acotado a la instancia. Con `SAFENT_NAME=matriz-final-1` seguiría haciendo `systemctl --user disable --now safent-agent.service` (esta máquina tiene uno **preexistente**, de agosto, ajeno a esta prueba), `rm -rf $HOME/.safent` y borraría `/usr/local/bin/safent`, `$HOME/.local/bin/safent`… El desmontaje equivalente se hizo a mano (`podman rm -f` + `volume rm`) |

## §Destructivo (orden exacto de la matriz)

| paso | resultado | evidencia |
|---|---|---|
| SEG-02..05 (freno) | **PASS** (ver §8) | probado y liberado antes de continuar; ninguna fila posterior corrió con el freno echado |
| CLI-11 `companion remove` | **PASS** | `[ok] Companion removed. State kept at …`; 0 contenedores `ads-*`; el estado (`bearer caps.yaml companions.json pg_password secrets sso tls`) intacto |
| CLI-12 `companion remove --purge` | **PASS con matiz** | `[ok] Companion removed and state purged`; el directorio de estado queda vacío. **Matiz**: deja atrás los tres volúmenes del companion (`safent-ads-companion-{db-data,broker-sock,credential-store}` — incluida la base de datos con las campañas) y la red `safent-companions`; los quitamos a mano en el desmontaje |
| SKL-05 desinstalar skill | **PASS** | `DELETE /skills/hub/gitnexus-explorer` → 202 `op_id`; `GET /skills` baja a `['native:git']` |
| MCP-10 eliminar MCP | **PASS** | `DELETE /mcp/memory` y `/mcp/gh-test` → 204 (§6) |
| AGT-05 eliminar agente | **PASS** | `DELETE /agents/{id}` → 204; el roster vuelve a `['CEO']` |
| AGT-13 eliminar tarea | **PASS** | `DELETE /tasks/scheduled/{uuid}` → 204; `GET /tasks/configured` → `[]` |
| ENT-04 unpair | **PASS** | inerte, sigue `community` |
| UPD-04 / UPD-06 | ver §1 | marcador creado; `safent uninstall` no ejecutado por invadir estado ajeno |
| destruir la instancia | **PASS** | `podman rm -f matriz-final-1` + `podman volume rm matriz-final-1-data` |

**Comportamiento observado durante el destructivo (menor, pero engañoso):** con el companion
**eliminado**, `GET /api/v1/mcp` siguió listando `safent-ads` como `healthy`, `tool_count: 61`,
`companion_status: "listo"` durante minutos. La verdad sí está disponible por el camino nuevo:
`GetCompanionHealth` devolvió `{"state":"unreachable","reachable":false}` y
`POST /api/v1/ads/bridge/session` respondió `{"status":"unavailable","reason":"unreachable"}`.
Es la vista de Herramientas la que se queda mintiendo.

## §Recreación final — INST-07, INST-08 y ADS-01

Con la instancia principal ya destruida se relanzó **el mismo nombre** (nunca dos contenedores a la vez):

```sh
SAFENT_NAME=matriz-final-1 SAFENT_VOLUME=matriz-final-1-data \
./ops/container/run-safent.sh localhost/safent-runtime:latest 18090 --no-companion --codex-auth $SCRATCH/out/auth.json
```

| id | resultado | evidencia |
|---|---|---|
| INST-07 | **PASS** | sin línea de provisioning en la salida; `/healthz` 200 a los **3 s**; `GET /mcp` = `excel 25 / word 54 / powerpoint 37` y **sin `safent-ads`** |
| INST-08 | **PASS** | `podman exec … cat /var/lib/hermes/hermes-home/.codex/auth.json` **idéntico** al fichero del host (`diff` vacío) y `CODEX_HOME=/var/lib/hermes/hermes-home/.codex`; con una cuenta real de Codex sería `[DUEÑO]` |
| ADS-01 | **PASS** | `POST /api/v1/ads/bridge/session` → `{"status":"unavailable","reason":"not_installed"}` y `GetCompanionHealth` → `{"state":"not_installed","reachable":false}`; `/app/anuncios` sirve la SPA con el estado honesto (`ads.state.not_installed.*` + CTA), no una pantalla vacía. Junto con el `listo`/`ready` de la instancia con companion, queda verificada la transición de estados **not_installed → ready** de la entrada "Ads" del sidebar |

## §Desmontaje

`podman rm -f matriz-final-1` · `podman volume rm matriz-final-1-data` · `podman network rm
safent-companions` · `podman volume rm safent-ads-companion-{db-data,broker-sock,credential-store}`
· `podman rmi ghcr.io/devwspito/safent-ads:latest` (imagen que **descargó el propio CLI** en CLI-10;
no estaba en el host antes de esta pasada).

Estado final verificado: `podman ps -a` → sólo los tres `safent-*` preexistentes y apagados
(`safent-diag`, `safent-v839`, `safent-audit`); `podman network ls` → sólo `podman`;
`podman volume ls` → ningún volumen `matriz-*`/`safent-ads-*`; `~/.safent/` → sólo su
`safent-seccomp.json` del 23-ago, intacto; ninguna credencial real usada ni escrita.

---

## Resumen

**168 filas · 114 PASS · 11 FALLA · 29 [DUEÑO] · 12 NO-APLICA-DGX · 2 NO-APLICA (capacidad retirada).**
Más 6 filas extra fuera de la numeración de la matriz (superficies nuevas de 022/026): 3 PASS
(flujo de UI de `force` con UN solo TOTP, WebSockets con token, entrada "Ads" del sidebar) y 3 FALLA
(las tres, la misma causa raíz de ADS-02).

### Los 11 FALLA

1. **MCP-04 — `uvx` con paquete no cacheado sigue muriendo con EXDEV.** El hallazgo #5 de la 1ª pasada **no está arreglado**: ningún MCP de Python nuevo es instalable.
2. **MCP-05 — el ejemplo del propio formulario de env es rechazado.** `BRAVE_API_KEY=…` (placeholder de `McpView.tsx:1148`) no está en `_MCP_BYOK_ENV_KEYS`; el usuario recibe un 400 crudo del backend.
3. **SEG-15 — desactivar "verificación en acciones sensibles" deja Seguridad rota.** Con `mfa_on_dangers:false` la UI manda `totp:""` y el backend sigue devolviendo 401 en presets, lote de permisos **y en el propio interruptor**: no se puede volver a activarlo desde la UI.
4. **PROV-02 — conectar un proveedor nativo desde la UI no deja modelo.** `config.yaml` queda con `model.provider` y sin `model.default` → el primer turno muere con `HermesModelNotConfiguredError`.
5. **PROV-03 — `POST /providers/{id-nativo}/test` responde 200 `{"ok":false,"error":"daemon_unavailable"}`** (parsea el id como UUID) → la tarjeta **siempre** marca "conexión fallida" y nunca activa.
6. **PROV-05 — el cambio de proveedor tarda ~30 s en aplicarse** (turno a +2 s con el proveedor viejo, a +35 s con el nuevo): regresión parcial del "efectivo en el turno siguiente" que R5 daba por cerrado.
7. **MEM-06 — `DELETE /memory/{id}` de una entrada inexistente devuelve 200** (GET da 404, PUT 400).
8. **ADS-02 (+ las dos filas 026) — la clave SSO del companion es ilegible para el daemon** (`ads-sso.key` 0400 root vs `User=hermes`) → `MintCompanionOwnerAssertion` PermissionError → `/ads/*` 503, panel de Ads inalcanzable, SC-002 sin cumplir. Al bearer sí se le hizo stage-in root; a la clave SSO de 026 no.
9. **CLI-08 — `safent companion status` dice `0/0 running` con el companion sano** (llama a compose sin `_companion_env`).
10. **CLI-10 — `safent companion rotate` rompe el companion**: recrea con `ghcr.io/devwspito/safent-ads:latest` (hardcodeado en `_companion_env`) mezclando imágenes → `ads-migrate` exit 255 y `ads-api` caído.
11. **BKP-01 — `safent restore` dice `[ok] Restored` sin crear el contenedor**: `_exists()` (`podman inspect $NAME`) casa con el **volumen** importado. Los datos del backup sí están íntegros (verificado arrancando el volumen a mano).

### Frente a la reejecución anterior (25 PASS / 5 FALLA)

- **Cerrados:** R11 (el `force` de skills ya es alcanzable desde la UI con UN solo TOTP, vía `reauth_grant`), R14 (`/tasks/recent` y `last_run_at` poblados tras disparar el cron), R26 (`POST /tailnet/connect` con clave falsa ya deja `configured:false` + `last_attempt.ok:false`).
- **Cerrados además:** los dos GAP de SSH — `tailnet_ssh`/`tailnet_file_get`/`tailnet_file_put` **están** en el esquema de tools del LLM (24 capacidades) y en el catálogo de políticas (88), y `GET`/`DELETE /api/v1/tailnet/ssh-hosts` existen con TOTP y UI.
- **A medias:** R17 (freno sin MFA) — ya hay ruta REST con contraseña de dispositivo vía el helper PAM, pero en una instalación nueva **no hay contraseña de dispositivo** y el gate falla cerrado (403); la única salida sigue siendo enrolar MFA con el freno echado.
- **Regresiones nuevas:** PROV-02/03/05 (conectar y cambiar de proveedor desde la UI), y todo el carril de companion/CLI que la pasada anterior no ejercitó (CLI-08/CLI-10/BKP-01) más el bloqueante de 026 (ADS-02).

### Menores anotados

`tool_count` del alta de MCP (9) ≠ el de la lista (11) · `POST /providers/native` con `set_active`
sin `model` devuelve `{ok:false}` bajo **201** · el error del proveedor se entrega como respuesta del
modelo con `outcome: completed` · `GET /usage/summary?period=day` se coerciona a `30d` sin avisar ·
`last_status` del cron se queda en `pending` aunque el bucle reporte `task_failed` · `GET /mcp`
sigue diciendo `healthy/listo` con el companion eliminado · `POST /mfa/enroll` enrola en la primera
llamada sin que nadie confirme que el dueño escaneó el QR · `safent companion remove --purge` deja
tres volúmenes y la red · `safent uninstall` no está acotado a la instancia.
