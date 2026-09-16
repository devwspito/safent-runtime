# 025 — Matriz de verificación completa (imagen fresca, post-fusión)

Plan de pruebas para un tester humano contra una imagen **fresca, sin estado**, una vez fusionadas las ramas en vuelo: `specs/025-safent-repaso/{radiografia.md, oleada-1.md}` y `specs/022-tailnet-connectivity/{contracts.md, ssh-v2.md, verificacion-ops.md}`, más los fixes ya mergeados (bearer obligatorio en todo `/api/v1/*` con SSE vía `?token=`, Landlock fail-closed, egress `{ok:false}`→4xx, cambio de proveedor efectivo sin reinicio, `GET /agents/active`, ids de cron unificados en `trigger_id`, freno de emergencia / kill switch).

Fuentes leídas: `frontend/src/{App.tsx,components/Layout.tsx,components/KillSwitchBanner.tsx,views/*.tsx}`, `frontend/src/lib/i18n.ts`, `frontend/src/api/client.ts`, `src/hermes/shell_server/main.py` + routers de `cowork/`, `egress_api.py`, `tailnet/api.py`, `training/api.py`, `system_update.py`, `instance/api.py`, `desktop/src-tauri/src/main.rs`, el CLI `safent` (shell, raíz del repo) + `get-safent.sh` + `ops/container/run-safent.sh`, y `specs/025-safent-repaso/matriz-en-vivo.md` (formato y hallazgos previos).

**Nomenclatura de ids:** `INST`=instalación/arranque · `UPD`=actualización/desinstalación · `CHAT` · `AGT`=Agentes (enjambre/tarjetas/live/tareas) · `SKL`=Habilidades · `INTG`=Integraciones · `MCP`=Herramientas · `LIVE`=En vivo · `SEG`=Seguridad · `COST`=Coste · `PROV`=Modelo de IA · `MEM`=Memoria · `FILES`=Archivos · `ADS`=Anuncios · `CLI`=CLI `safent` · `DESK`=shell de escritorio · `DIST`=distribución multiplataforma · `ENT`=Enterprise/pairing · `BKP`=copias/restauración · `SSH`=SSH gobernado sobre tailnet.

Todas las rutas de API son relativas a `http://127.0.0.1:$PORT/api/v1` (bearer en `Authorization: Bearer <k>`, obtenido de `GET /app/?k=<webui-bootstrap>` o leyendo `/var/lib/hermes-bootstrap/bootstrap/webui-bootstrap` dentro del contenedor). Todas las URL de UI son relativas a `http://127.0.0.1:$PORT/app`.

---

## 0. Instalación y arranque (INST) — 8 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| INST-01 | Arranque hardened (`run-safent.sh`) | `SAFENT_NAME=safent-repaso2 SAFENT_COMPANION_STATE=/var/tmp/safent-repaso2/companions/ads ./ops/container/run-safent.sh <imagen> 18082` (ver comando exacto en §Orden de ejecución) | Companion `ads-migrate` exit 0; `hermes-runtime`/Uvicorn `:7517` arrancan; `GET /healthz` → 200 en el primer sondeo | `podman logs`, timestamps de arranque, `curl -s localhost:18082/healthz` | alto | ninguna (imagen local u OCI accesible) |
| INST-02 | Systemd sano dentro de la jaula | `podman exec safent-repaso2 systemctl is-system-running` | `running`, 0 unidades failed; `hermes-runtime/shell-server/egress-proxy/browser-netns/mcp-launcher/companion-egress` `active` | salida completa del comando | alto | — |
| INST-03 | Landlock fail-closed (fix mergeado) | `podman exec … cat /sys/kernel/security/lsm` y `systemctl status hermes-landlock-assert`; adicionalmente, forzar el fallo del assert (kernel sin Landlock si hay banco de pruebas) y comprobar que el arranque se **detiene**, no degrada silencioso | `landlock` en la lista de LSM; `hermes-landlock-assert` `Result=success`; en el caso negativo, el servicio dependiente no arranca (fail-closed, no fallback a jaula débil) | journal completo del assert + de la unidad dependiente | alto | banco con kernel sin Landlock (opcional, solo para el caso negativo) |
| INST-04 | Handshake `?k=` / bearer estable | `curl -s localhost:$PORT/app/?k=$(podman exec … cat /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap)` y confirmar que el bearer queda inyectado en `index.html` | 200; el bearer inyectado funciona en llamadas subsiguientes a `/api/v1/*` | respuesta HTTP + primera llamada autenticada | alto | — |
| INST-05 | Bearer obligatorio en TODO `/api/v1/*` (fix mergeado) | Repetir sin `Authorization`: `curl -i localhost:$PORT/api/v1/agents`, `curl -i "localhost:$PORT/api/v1/runtime/agent-stream"` (SSE sin `?token=`), y si existe alguna ruta WebSocket expuesta por `/api/v1/*` (ver `SEG-VNC` más abajo), idem sin `?token=` | Las tres → 401 `unauthorized: operator token required`; con `?token=<bearer>` en la SSE → 200 y stream | tres respuestas HTTP con status y body | alto | — |
| INST-06 | Companion Ads preinstalado en el arranque | Sin tocar nada, `GET /mcp` tras el arranque | `safent-ads` listado con `tool_count` > 0, `companion_status: listo`/`ready` a los pocos segundos | payload JSON + timestamp de "listo" | medio | red saliente al registry de imágenes (o `safent-ads:local` precargada) |
| INST-07 | `--no-companion` | Relanzar una instancia nueva con `--no-companion` | Arranca igual de sano, `GET /mcp` no incluye `safent-ads`; Herramientas muestra la tarjeta "Safent Ads" en estado "conectar por URL" | `podman logs` sin provisioning de ads, captura de McpView | bajo | — |
| INST-08 | `--codex-auth <path>` | Relanzar con `--codex-auth /ruta/a/auth.json` (fichero dummy con estructura válida) | El fichero se monta RO en `hermes-home/.codex/auth.json`; `podman exec … cat /var/lib/hermes/hermes-home/.codex/auth.json` coincide con el original | diff de contenido | bajo | fichero `auth.json` de una sesión real de `codex login` — `[DUEÑO]` si se quiere probar con cuenta real |

## 1. Actualización / Desinstalación (UPD) — 6 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| UPD-01 | Footer "Actualizar" (sidebar) | Click en el botón `sysupdate.action` del pie del sidebar → confirmar en el diálogo | `POST /system/update` 200; toast `sysupdate.toast.started`; marcador `/var/lib/hermes/instance/.update-requested` creado (`podman exec … ls -la /var/lib/hermes/instance/`) | captura del toast + `ls` del marcador | medio | — |
| UPD-02 | `safent agent` consume el marcador | Con `safent agent` corriendo en el host (o simulado a mano), verificar que aplica `_self_update → _ensure_agent → _reclaim_space → podman pull → recreate` | Contenedor recreado con el mismo volumen; datos intactos tras `podman restart` equivalente | logs del agente host + estado antes/después | medio | red de salida del HOST (no del contenedor) hacia GitHub raw / ghcr.io |
| UPD-03 | `GET /system/update` sin agente | Dejar el marcador sin consumir >15 min (`_FLAG_STALE_S`) | `updating:true` durante la ventana, se limpia solo pasado el timeout | dos lecturas de `GET /system/update` separadas por el timeout | bajo | tiempo de espera largo (15 min) |
| UPD-04 | Footer "Desinstalar" | Click en `sysuninstall.action` → confirmar | `POST /system/uninstall` 200; toast `sysuninstall.toast.started`; marcador `.uninstall-requested` (o equivalente) creado | captura + inspección del marcador | alto (destructivo) | **ejecutar en último lugar** |
| UPD-05 | `safent update` (CLI) | `safent update` desde el host contra la instancia de prueba (`SAFENT_NAME=safent-repaso2`) | Auto-actualiza el propio script si `_CLI_REV` del remoto es mayor; hace `pull` + recrea preservando `$VOLUME` | salida completa del CLI, versión antes/después | medio | red saliente del host |
| UPD-06 | `safent uninstall` (CLI, destructivo) | `SAFENT_NAME=safent-repaso2 safent uninstall` | Contenedor, volumen `safent-repaso2-data`, companion, caché de seccomp y agente de actualización eliminados; `podman/docker` intactos | `podman ps -a`, `podman volume ls`, `systemctl --user status safent-agent.service` (debe no existir) | alto (destructivo) | **ejecutar en último lugar** |

## 2. Chat (CHAT) — 12 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| CHAT-01 | Sin modelo conectado | `/app/chat`, enviar un mensaje sin proveedor configurado | Aviso in-chat `chat.nomodel.text` + CTA `chat.nomodel.cta` hacia Modelo de IA; el turno no se envía a ningún proveedor | captura del aviso | bajo | — |
| CHAT-02 | Enviar mensaje (con modelo real) | Configurar un proveedor (ver `PROV-*`), escribir y `Enter`/botón `chat.send` | `POST /chat` 201 con `task_id`; stream SSE `GET /chat/stream/{task_id}?token=` entrega `delta`→`done`; burbuja de respuesta se renderiza | grabación de red (SSE frames) + captura del chat | medio | proveedor de modelo activo (clave real o Codex OAuth) `[DUEÑO]` si se usa cuenta del dueño |
| CHAT-03 | Detener generación | Durante un stream activo, click `chat.stop` | El SSE se cierra desde cliente (`stopStream`), no reintenta; el turno queda marcado como interrumpido | captura antes/después + estado en `EnVivoView`/`tareas` | bajo | modelo activo |
| CHAT-04 | Reconexión tras refresco | Enviar mensaje, refrescar la pestaña a media respuesta | `chat.reconnecting` visible brevemente; el SSE retoma con `Last-Event-ID`, sin frames duplicados ni perdidos | grabación de red con `id:` de los eventos | medio | modelo activo |
| CHAT-05 | Adjuntar archivo | Menú `chat.menu.attach` → subir fichero < límite | `POST /workspace/files` (multipart) 200; chip de adjunto visible; el mensaje se envía con la ruta subida | captura + `GET /workspace/files` mostrando el fichero | bajo | — |
| CHAT-06 | Adjunto que excede el límite | Subir fichero por encima del tamaño máximo | Error `chat.err.attach` inline, sin bloquear el resto del compositor | captura del error | bajo | fichero de prueba grande |
| CHAT-07 | Seleccionar carpeta (folder bridge) | Menú → `chat.menu.folder` (requiere Chrome/Edge) | `chat.folder.ready` con conteo de ficheros; tras un turno, botón `chat.folder.save_aria` guarda cambios de vuelta | captura + diff de la carpeta local antes/después | medio | navegador Chrome/Edge; sistema de ficheros local |
| CHAT-08 | Selector de carpeta en navegador no soportado | Repetir CHAT-07 en Firefox | `chat.folder.picker_unsupported` | captura | bajo | Firefox instalado |
| CHAT-09 | Delegación a especialista | Enviar un prompt que dispare `delegate_task`/`mixture_of_agents` (roster con departamentos) | Tarjeta de delegación `chat.delegation.delegating_to` → `chat.delegation.delegated_to`; `aria-live` anuncia el cambio | captura + inspección de accesibilidad (aria) | medio | roster con ≥2 agentes activos |
| CHAT-10 | Chip "usando el navegador" → En vivo | Prompt que dispare `browser_navigate` con éxito | Chip `chat.live.card` visible; `chat.live.open` navega a `Capacidades → En vivo` con el frame VNC activo | captura del chip + captura de En vivo | medio | `browser_navigate` habilitado en policies; egress que permita el destino |
| CHAT-11 | Recientes (sidebar) | Crear 5 conversaciones, comprobar recorte a 3 + "ver más" | Lista muestra 3, botón `layout.recents.more` expande a 5, `layout.recents.less` contrae | captura de ambos estados | bajo | — |
| CHAT-12 | Nueva conversación | Botón `layout.new_chat` en sidebar | Limpia `convId`/`agentId`/mensajes, navega a `/chat` | captura antes/después | bajo | — |

## 3. Agentes — `/agentes` (AGT) — 14 filas

### 3.1 Tarjetas (roster) — `?tab=tarjetas`

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| AGT-01 | Listar roster | `/agentes?tab=tarjetas` | `GET /agents/roster` 200; tarjetas agrupadas por departamento (`groupDepartmentsByKind`) | captura | bajo | — |
| AGT-02 | Toggle "roster por defecto" | Switch `defaultRosterEnabled` | `POST /agents/default-roster {enabled}` 200; roster se repuebla/oculta según CE (community solo siembra `default`) | captura antes/después + `GET /agents/default-roster` | medio | — |
| AGT-03 | Crear agente | Formulario "nuevo agente" → nombre, departamento, prompt | `POST /agents` 201; aparece en el roster y en el selector de "Agentes" del chat | captura + `GET /agents` | medio | — |
| AGT-04 | Editar agente | Editar un agente existente → guardar | `PATCH /agents/{id}` 200; cambios reflejados sin recargar | captura antes/después | bajo | agente creado en AGT-03 |
| AGT-05 | Eliminar agente | Botón eliminar → confirmar diálogo | `DELETE /agents/{id}` 200; desaparece del roster y de conversaciones nuevas (las existentes conservan `agentName`) | captura + `GET /agents` | medio | agente creado en AGT-03 |
| AGT-06 | Activar agente desde tarjeta | `setActiveAgent` sobre una tarjeta | `POST /agents/{id}/activate` 200; `GET /agents/active` (fix mergeado, antes 405) refleja el id activo sin `.catch` silencioso | captura + `curl GET /agents/active` | medio | — |

### 3.2 Enjambre (`?tab=enjambre`) y Live (`?tab=live`)

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| AGT-07 | Enjambre — snapshot en vivo | `/agentes?tab=enjambre`, lanzar 2+ tareas en paralelo | `GET /runtime/agent-stream` (SSE) empuja `RuntimeSnapshot` cada cambio; el mapa de "cerebro sináptico" se actualiza sin polling | grabación de red SSE + captura | medio | ≥2 agentes trabajando a la vez |
| AGT-08 | Enjambre — reconexión SSE | Cortar red del navegador 20 s durante el stream | Backoff exponencial 1s→15s tope; al reconectar, `reconnectDelayMs` vuelve a 1s | grabación de red (timestamps de reintento) | bajo | throttling de red del navegador (DevTools) |
| AGT-09 | Piso pixel (Live) | `/agentes?tab=live` con un agente trabajando | Personajes/animaciones reflejan `agentStats`/`runtimeStatus`; burbujas de actividad por tarea | vídeo corto | bajo | motor de canvas (WebGL/2D) del navegador |

### 3.3 Tareas / cron (`?tab=tareas`, `CalendarView`)

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| AGT-10 | Crear tarea programada | `cal.new_task` → cron `* * * * *`, instrucción, agente destino | `POST /tasks/scheduled` 201 con `trigger_id` (UUID); toast `cal.toast.created`; aparece en board/list | captura + `GET /tasks/configured` | medio | — |
| AGT-11 | Detalle/edición por id (fix mergeado — antes 404 por `trigger_id` corto) | Click en la tarjeta creada → `cal.view` | `GET /tasks/scheduled/{trigger_id}` 200 con el UUID completo, no el id corto de 12 hex | captura del payload | medio | tarea de AGT-10 |
| AGT-12 | Pausar/activar tarea (fix mergeado — antes `{"ok":false}` con HTTP 200) | Botón `cal.pause`/`cal.activate` | `POST /tasks/scheduled/{trigger_id}/enabled {enabled}` 200 real (`ok:true`), badge `cal.badge.paused` cambia | captura antes/después | medio | tarea de AGT-10 |
| AGT-13 | Eliminar tarea | `cal.delete` → confirmar (`cal.delete.confirm.title`) | `DELETE /tasks/scheduled/{trigger_id}` 204; desaparece de board/list | captura + `GET /tasks/configured` | medio | tarea de AGT-10 |
| AGT-14 | Runs recientes (fix mergeado — antes `/tasks/recent` siempre `[]`) | Dejar disparar el cron 1 min → pestaña `cal.tab.runs` | `GET /tasks/recent` devuelve entradas con `last_run_at`/`last_status` poblados | captura del listado + payload | medio | esperar ≥1 disparo del cron (1-2 min) |

## 4. Capacidades → Habilidades (SKL) — 10 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| SKL-01 | Buscar en el catálogo | `/capacidades?tab=skills`, buscar "git" | `GET /skills/hub/search?q=git` 200 con resultados | captura | bajo | red de salida del daemon hacia el hub de skills |
| SKL-02 | Instalar skill limpia | Instalar un resultado sin bandera de riesgo | `POST /skills/hub/install {identifier, force:false}` → `op done`; toast `skills.install.installed`; aparece en "Activas" | captura + `GET /skills` | medio | — |
| SKL-03 | Instalar skill bloqueada por escaneo | Instalar `gitnexus-explorer` (o equivalente con `score` bajo) | Modal `skills.install.blocked.title` con score/riesgos; **sin** `force`, la instalación no continúa | captura del modal | medio | — |
| SKL-04 | Forzar instalación bloqueada — verificar el fix de MFA (antes: bypass sin TOTP) | Confirmar `skills.install.blocked.confirm` (force:true) | **Esperado tras el fix:** exige TOTP (igual que `/security/decisions`) antes de completar; **si no lo exige, es regresión del hallazgo #4 de `matriz-en-vivo.md`** | captura del flujo completo + `GET /security/scans` con la decisión auditada | alto | MFA activada (ver `SEG-02`) |
| SKL-05 | Desinstalar skill | `skills.uninstall.aria` → confirmar | `DELETE /skills/hub/{name}` 204; desaparece de "Activas"; agente ya no la ve | captura + `GET /skills` | bajo | skill instalada |
| SKL-06 | Ver detalles / docs | `skills.view.aria` sobre una skill instalada | `GET /skills/{id}/details` 200; modal con instrucciones; link `skills.docs.aria` abre en pestaña nueva | captura | bajo | skill instalada |
| SKL-07 | Promover a autónoma | Botón `skills.promote` | `POST /skills/{id}/promote {confirm:true}` 200; toast `skills.promote.toast`; deja de pedir confirmación en el chat | captura antes/después de un uso en chat | medio | skill instalada |
| SKL-08 | Verificar skill | Botón `skills.verify` | Inserta en el chat `skills.verify.msg` con el nombre de la skill; el agente la ejecuta y muestra el resultado | captura del turno de chat | medio | modelo activo |
| SKL-09 | Enseñar habilidad por demostración (navegador enjaulado) — **ver nota en el informe: puede NO estar retirada, verificar contra la instrucción de fusión** | `skills.teach.open` → `TeachModal` a pantalla completa → interactuar con `TeachPanel` (noVNC) → `skills.teach.stop` | `POST /training` inicia sesión; `POST /training/{id}/start` abre navegador enjaulado (Landlock+netns); `POST /training/{id}/sign` compila y persiste el `SkillPackage`; aparece etiquetada "en vivo" (`skills.live.badge`) | vídeo de la demostración + `GET /skills` con la nueva entrada | alto | navegador enjaulado operativo; egress que permita el sitio demostrado |
| SKL-10 | Abandonar/cancelar enseñanza a medio camino | Abrir `TeachModal`, cerrar con Escape/✕ antes de firmar | `POST /training/{id}/abandon` (o cierre limpio); no queda skill a medias en `GET /skills` | captura + verificación de que no aparece skill huérfana | bajo | — |

## 5. Capacidades → Integraciones (INTG) — 8 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| INTG-01 | Sin clave Composio | `/capacidades?tab=integraciones` en fresco | Estado `no-key`; no llama a `connected`/`toolkits` (evita colgarse minutos) | captura + inspección de red (0 llamadas a esos endpoints) | bajo | — |
| INTG-02 | Configurar clave Composio | Introducir API key → guardar | `POST /integrations/composio/key {api_key}` 200; pasa a estado `ready`, carga `connected`+`toolkits` | captura | medio | clave Composio real `[DUEÑO]` |
| INTG-03 | Conectar app (OAuth) | Elegir un toolkit no conectado → conectar | `POST /integrations/composio/connect {toolkit_slug}` 200 con `redirect_url`; se abre el flujo OAuth del proveedor | captura del `redirect_url` | medio | cuenta real del servicio a conectar `[DUEÑO]` |
| INTG-04 | Desconectar app | Sobre una app conectada, desconectar | `DELETE /integrations/composio/connected/{slug}` 200; desaparece de "conectadas" | captura antes/después | bajo | app conectada en INTG-03 |
| INTG-05 | Web search — estado inicial | Ver bloque de búsqueda web | `GET /web-search/status` con `brave/tavily/exa:false`, `ddgs_fallback:true` | captura + payload | bajo | — |
| INTG-06 | Activar Brave Search | Introducir clave → `int.brave.activate_btn` | `POST /web-search/key {provider:"brave", api_key}` 200; toast `int.brave.activated_toast`; estado pasa a activo | captura | bajo | clave de `api.search.brave.com` `[DUEÑO]` |
| INTG-07 | Clave Brave vacía | Click en activar sin rellenar | `int.brave.err.enter_key` inline, sin llamada de red | captura | bajo | — |
| INTG-08 | Fallback DDGS sin ninguna clave | Con `brave/tavily/exa` todos `false`, disparar `web_search` desde el chat | El agente busca vía DuckDuckGo (ddgs) sin error | captura del turno de chat + resultado | bajo | modelo activo, `web_search` habilitada en policies |

## 6. Capacidades → Herramientas / MCP (MCP) — 12 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| MCP-01 | Listar activas | `/capacidades?tab=mcp` | `GET /mcp` 200; seeds `excel/word/powerpoint` healthy + `safent-ads` si el companion está arriba | captura | bajo | — |
| MCP-02 | Añadir del catálogo curado (npx) | Click en tarjeta "GitHub"/"Context7"/"Archivos locales" | `POST /mcp {argv:["npx","-y",...]}` 200/201; herramienta conectada, `tool_count`>0 | captura + `GET /mcp` | medio | red de salida hacia npm registry |
| MCP-03 | Buscar en el registro MCP | Campo de búsqueda → término libre | `GET /mcp/registry?q=...&limit=30` 200 con resultados externos | captura | bajo | red de salida hacia el registro MCP |
| MCP-04 | Añadir servidor personalizado (uvx, paquete NO cacheado) | Formulario manual, runner `uvx`, paquete nunca instalado antes | **Verificar el fix del hallazgo #5** (`Invalid cross-device link, os error 18`): con el fix, prefetch en unidad transitoria o `uv tool install --no-cache` debe completar sin EXDEV | captura del resultado + journal del prefetch | alto | red de salida hacia PyPI |
| MCP-05 | Añadir servidor personalizado (npx, con env vars) | Formulario manual, runner `npx`, rellenar `mcp.env.label` | `POST /mcp` 200/201 con `env` inyectado; validación `mcp.env.required` si falta un campo obligatorio | captura de ambos casos (válido/incompleto) | bajo | — |
| MCP-06 | Runner no permitido | Intentar runner `bash` a mano vía curl: `curl -X POST .../mcp -d '{"argv":["bash","-c","echo hi"]}'` | 400 (allowlist `npx/pipx/uvx`) | respuesta HTTP | medio | — |
| MCP-07 | Slug managed-remote desconocido | `POST /mcp/managed-remote/no-existe/connect {url}` | 400 | respuesta HTTP | bajo | — |
| MCP-08 | Conectar Safent Ads (managed-remote, con companion arriba) | Tarjeta "Safent Ads" → `mcp.managed.connect` con la URL autodetectada del companion | `POST /mcp/managed-remote/safent-ads/connect {url, force:false}` 200; toast `mcp.managed.ads.toast.connected`; aparece sidebar "Anuncios" | captura + navegación a `/anuncios` | medio | companion `safent-ads` arriba (INST-06) |
| MCP-09 | Conectar Safent Ads self-hosted (`--no-companion`) | Con la instancia de INST-07, pegar URL propia `https://.../mcp` | Validación de esquema (`mcp.managed.err.scheme` si no empieza por `https://`); conecta si la URL es alcanzable | captura del error + del éxito | medio | servidor MCP de Ads propio accesible `[DUEÑO]` |
| MCP-10 | Eliminar servidor MCP | Sobre una herramienta activa, eliminar | `DELETE /mcp/{id}` 204; desaparece; si era un seed (`excel`), no resucita hasta añadirlo de nuevo | captura + `GET /mcp` | medio | herramienta añadida en MCP-02 |
| MCP-11 | Detalles técnicos | `mcp.details.show` sobre una entrada | Expande argv/env/estado sin exponer secretos en claro (env values enmascarados) | captura | bajo | — |
| MCP-12 | Servidor bloqueado por escaneo de seguridad | Añadir un servidor cuyo `identifier` dispare `scanInstall` con verdict FAIL | `POST /security/scans/install {kind:"mcp", identifier}` 200 con verdict; instalación bloqueada sin decisión explícita registrada vía `recordSecurityDecision` | captura del bloqueo | alto | identificador de MCP con verdict FAIL conocido (o fabricar uno) |

## 7. Capacidades → En vivo (LIVE) — 5 filas (solo lectura)

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| LIVE-01 | Sin actividad | `/capacidades?tab=en-vivo` en fresco | `EmptyState` con `envivo.no_tasks`; sin frame VNC | captura | bajo | — |
| LIVE-02 | Navegador real activo | Disparar `browser_navigate` desde el chat y abrir En vivo | Frame `VncFrame viewOnly` visible SOLO cuando `runtime_status.browser_live` es real (no por el mero nombre de la tool) | vídeo corto | medio | modelo activo, `browser_navigate` habilitado |
| LIVE-03 | Tool de navegador que falla (no abre página real) | Forzar un `browser_navigate` a una URL bloqueada por egress | El frame VNC **no** se muestra (browser_live sigue false) — verificar que no "miente" | captura + log de egress denegado | medio | dominio en denylist/egress deny |
| LIVE-04 | Tareas en ejecución + detener | Con una tarea larga corriendo, ver sección `envivo.running_tasks`, click `envivo.stop` | `POST /tasks/{id}/cancel` 200; toast `envivo.stopping`; la tarea desaparece de la lista tras `~1.5s` | captura antes/después | medio | tarea en curso |
| LIVE-05 | Ausencia de "Enseñar" en esta vista | Confirmar que En vivo NO tiene ningún control de enseñanza (solo monitor + detener) | El único punto de entrada a enseñar es `SKL-09` (Habilidades) — consistente con el docstring `"Teaching now lives in Habilidades"` | captura de toda la vista | bajo | — |

## 8. Sistema → Seguridad (SEG) — 26 filas

### 8.1 Freno de emergencia (kill switch — feature nueva de esta oleada)

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| SEG-01 | Estado inicial | `/sistema?tab=seguridad`, sección "Freno de emergencia" | `GET /security/kill-switch` `engaged:false`; botón `seg.killswitch.engage` visible | captura | bajo | — |
| SEG-02 | Activar el freno (sin MFA — es un freno) | Click `seg.killswitch.engage` | `POST /security/kill-switch {engaged:true, reason}` 200 **sin pedir TOTP**; banner rojo global (`KillSwitchBanner`) visible en TODAS las vistas, no solo Seguridad | captura del banner en `/chat`, `/agentes`, `/capacidades` | alto | — |
| SEG-03 | Efecto real: el agente no ejecuta nada | Con el freno activado, intentar enviar un mensaje de chat | El turno nuevo es rechazado/no se admite; ninguna tool se ejecuta | captura del rechazo | alto | modelo activo |
| SEG-04 | Liberar el freno (con TOTP — acción soberana) | Click `seg.killswitch.release` → `MfaModal` | Sin TOTP válido, rechaza; con TOTP correcto, `POST /security/kill-switch {engaged:false, totp}` 200; banner desaparece | captura del modal + del banner desaparecido | alto | MFA enrolada (SEG-08) |
| SEG-05 | Link "Liberar" desde el banner | Con el freno activo en `/chat`, click en el link del `KillSwitchBanner` | Navega a `/sistema?tab=seguridad`, foco en la sección del freno | captura | bajo | freno activado |

### 8.2 Aprobaciones (HITL) y delegaciones entrantes

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| SEG-06 | Aprobación pendiente — aprobar | Disparar una tool "most_delicate" (p.ej. `write_file` fuera del workspace) → tarjeta de aprobación en Seguridad Y en el chat (`PendingApprovalsInChat`) | `POST /approvals/{id} {decision:"approve", totp}` 200; el agente continúa | captura de ambas ubicaciones + resultado del turno | alto | MFA enrolada si la tool exige TOTP |
| SEG-07 | Aprobación pendiente — rechazar | Idem, rechazar | `decision:"reject"` 200; el agente se detiene, toast `approval.toast.denied` | captura | medio | — |
| SEG-08 | Enrolar MFA | Sección `seg.mfa.label`, `MfaEnroll` → escanear QR (`otpauth_uri`) → introducir código | `POST /mfa/enroll {totp}` 200; `mfa_enroll.done`; a partir de aquí todas las acciones sensibles piden TOTP | captura del QR + confirmación | alto | app TOTP (Google Authenticator/Authy) |
| SEG-09 | TOTP incorrecto | En cualquier `MfaModal`, introducir código erróneo | 401 `invalid_totp`, modal permanece abierto con error inline, foco vuelve al input | captura | medio | MFA enrolada |
| SEG-10 | TOTP reutilizado | Reenviar el mismo código TOTP dos veces seguidas | 401 `totp_replayed` en el segundo intento | captura | medio | MFA enrolada |
| SEG-11 | Delegación entrante (A2A) — aprobar/rechazar | Requiere un segundo Safent emparejado que delegue una tarea | `POST /inbound-delegations/{message_id} {decision}` 200; tarjeta `InboundDelegationCard` se resuelve | captura de ambos Safent | alto | segunda instancia Safent emparejada `[DUEÑO]`/lab |

### 8.3 Gobernanza — presets y catálogo de permisos

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| SEG-12 | Cambiar preset (equilibrado→permisivo→bloqueado) | Botones de preset → banner de confirmación → `seg.preset.save` | `POST /policies/preset {preset, totp}` 200; catálogo entero refleja el preset (`bloqueado`: el agente solo conversa) | captura antes/después de cada preset | alto | MFA (o `mfa_on_dangers:false` para bypass, ver SEG-15) |
| SEG-13 | Toggle individual de capacidad | Expandir una categoría (`CategoryGroup`) → togglear una tool | Cambio queda en `toolPending`, banner "cambios pendientes" aparece | captura | bajo | — |
| SEG-14 | Guardar cambios de permisos en lote | Con varios toggles pendientes, `seg.changes.save` | `POST /policies/tools {tools, totp}` 200; toast `seg.save.ok`; catálogo persiste tras recarga | captura + recarga de página | medio | MFA |
| SEG-15 | Desactivar verificación en acciones sensibles | Toggle `seg.policies.dangers.label` a off | `POST /policies/mfa_on_dangers {enabled:false, totp}` 200; a partir de aquí, presets/toggles NO piden TOTP (`mfaDisabled`) | captura antes/después + repetir SEG-12 sin modal | alto | MFA |
| SEG-16 | Descartar cambios pendientes | Con toggles pendientes, `seg.changes.discard` | Vuelve al estado persistido, sin llamada de red | captura | bajo | — |
| SEG-17 | Categoría "Defensas del sistema" | Expandir `defenseGroups` (categoría `security`) | Nota explicativa "no son capacidades del agente"; togglear no afecta el catálogo LLM-visible | captura | bajo | — |

### 8.4 Egress (red)

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| SEG-18 | Modo por defecto en fresco | `GET /egress/mode` recién arrancado | **Verificar el fix del hallazgo #3**: debe ser `deny` por defecto (antes `allow`, contradecía `SECURITY.md`); si sigue en `allow`, es regresión | captura del payload + de `example.com` accedido/bloqueado desde una tool | alto | — |
| SEG-19 | Cambiar de modo (requiere TOTP siempre) | `EgressModeToggle` → `allow`↔`deny` | `POST /egress/mode {mode, totp}` 200 SIEMPRE con TOTP, incluso con `mfa_on_dangers:false` (mirar código: `setEgressMode` no tiene bypass) | captura del modal en ambos casos | alto | MFA |
| SEG-20 | Modo `allow` — bloquear/desbloquear dominio manual | `AllowModePanel`: introducir dominio → `seg.network.block`/`unblock` | `POST /egress/deny/add`/`remove` 200 SIN TOTP (bloqueo manual en modo allow no lo exige) | captura | medio | — |
| SEG-21 | Modo `deny` — autorizar/revocar dominio | `DenyModePanel`: introducir dominio → `seg.network.authorize`/`revoke` | `POST /egress/domains/grant`/`revoke` 200 | captura | medio | — |
| SEG-22 | Contador de bloqueados por el sistema | En modo `allow`, ver `blocklistBadge` | Refleja `blocklist_count` del backend (dominios maliciosos conocidos) | captura | bajo | — |

### 8.5 Tailnet (spec 022) y escaneos

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| SEG-23 | Tailnet no configurada | Sección "Tailnet" en fresco | `GET /tailnet` `configured:false`; formulario de conexión (`tskey-auth-…`) visible | captura | bajo | — |
| SEG-24 | Conectar a la tailnet | Introducir auth key real → `Conectar` | `POST /tailnet/connect {auth_key}` 200 `staged:true`; polling cada 5s hasta `online:true`; MagicDNS suffix + peers listados | captura del ciclo completo | alto | clave `tskey-auth-...` de Tailscale real `[DUEÑO]` |
| SEG-25 | Desconectar de la tailnet | Introducir contraseña del dispositivo → `Desconectar` | `POST /tailnet/disconnect {password}` 200 `staged:true`; vuelve a `not_configured` | captura | alto | contraseña del dispositivo host (PAM) `[DUEÑO]` |
| SEG-26 | Escaneos de seguridad — permitir instalación bloqueada | Sección "Análisis de seguridad", sobre un scan FAIL/WARN, `seg.scan.allow` → TOTP | `POST /security/decisions {scan_id, decision:"allow", totp}` 200; badge pasa a `seg.scan.allowed`; entrada auditada en el hash-chain (`GET /security/audit/head` cambia) | captura + comparación del hash de auditoría antes/después | alto | un scan bloqueado real (usar SKL-03/MCP-12) |

## 9. Sistema → Coste (COST) — 4 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| COST-01 | Resumen por periodo | `/sistema?tab=coste`, cambiar periodo (día/semana/mes) | `GET /usage/summary?period=` refleja coste/tokens/ciclos acumulados de los turnos ya ejecutados en este plan | captura por cada periodo | bajo | turnos de chat previos (CHAT-02, etc.) |
| COST-02 | Desglose por agente | Vista "por agente" | `GET /usage/by-agent?period=` lista coste por `agent_id` | captura | bajo | ≥2 agentes usados |
| COST-03 | Serie temporal | Selector de dimensión (por día/por modelo) | `GET /usage/timeseries?period=&dimension=` puntos coherentes con el resumen | captura | bajo | — |
| COST-04 | Coste por conversación | Abrir una conversación concreta desde "recientes" y ver su coste | `GET /chat/conversations/{id}/usage` coincide con lo mostrado | captura | bajo | conversación con turnos reales |

## 10. Sistema → Modelo de IA / Proveedores (PROV) — 12 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| PROV-01 | Catálogo nativo | `/sistema?tab=proveedores` | `GET /providers/native` lista ids nativos (openai, anthropic, gemini, zai, openai-codex, xai-oauth, qwen-oauth, nous…) | captura | bajo | — |
| PROV-02 | Configurar proveedor con API key | Elegir `anthropic`/`openai-api`/`gemini`/`zai`, introducir clave real | `POST /providers/native {provider_id, api_key}` 200; **verificar fix del hallazgo #2**: SDK correspondiente presente (sin `ImportError`) | captura del primer turno de chat funcionando | alto | clave real del proveedor `[DUEÑO]` |
| PROV-03 | Probar proveedor | Botón "Probar"/`testProvider` | `POST /providers/{id}/test` 200; **verificar hallazgo menor**: con clave inválida debe distinguirse de un 404 genérico opaco | captura de clave válida e inválida | medio | clave real (una válida, una inventada) |
| PROV-04 | Activar proveedor — **verificar fix del hallazgo #1 (routing)** | Activar `anthropic` tras tener `gemini` ya configurado | `POST /providers/{id}/activate` 200; el SIGUIENTE turno de chat usa realmente Anthropic, no el proveedor previamente activo por variable de entorno | captura de red del turno + modelo real usado en la respuesta | alto | 2 proveedores configurados con claves reales `[DUEÑO]` |
| PROV-05 | Cambiar de proveedor sin reinicio | Repetir PROV-04 alternando 2-3 veces seguidas sin recrear el contenedor | Cada cambio es efectivo en el turno inmediatamente siguiente | captura de 3 turnos consecutivos con proveedores distintos | alto | 2+ proveedores con claves reales `[DUEÑO]` |
| PROV-06 | Eliminar proveedor | `DELETE /providers/{id}` desde la tarjeta | 200; desaparece de la lista; si era el activo, el chat vuelve a `chat.nomodel` | captura | medio | proveedor configurado |
| PROV-07 | OAuth device-code — Codex/ChatGPT | Card OpenAI Codex → `providers.codex.login_btn` | `POST /providers/openai-codex/oauth/start` → `{flow:"device_code", user_code, verification_url}`; polling hasta `connected` o `expired` | captura del código + del polling | alto | cuenta ChatGPT/OpenAI del dueño `[DUEÑO]` |
| PROV-08 | OAuth expirado | Dejar expirar el `device_code` sin completar login | `providers.oauth.expired`; botón vuelve a estado inicial | captura tras esperar `expires_in` | bajo | tiempo de espera (~15 min) |
| PROV-09 | Fallback a API key tras oauth_required | Un proveedor que devuelve `{ok:false, error:"oauth_required"}` al intentar API key | UI ofrece `providers.oauth.fallback_notice` y el flujo de conectar OAuth en su lugar | captura | bajo | — |
| PROV-10 | Custom provider (kind personalizado) | `POST /providers` con `kind` no nativo (p.ej. `openrouter`, `vllm`, `ollama`) y `default_model` | 201; distinto de la ruta nativa (rechaza si falta `default_model`) | captura + respuesta 422 si falta el campo | medio | endpoint OpenAI-compatible accesible (self-hosted o clave real) `[DUEÑO]` |
| PROV-11 | Modelo gestionado por Enterprise (associate edition) | Solo aplicable si la instancia está emparejada (`ENT-*`) | El chip muestra `chat.model.org_managed_label`, no editable desde Proveedores | captura | bajo | instancia emparejada (`ENT-02`) — normalmente NO PROBABLE en CE |
| PROV-12 | Estado tras `podman restart` | Reiniciar el contenedor (no recrear) | Proveedores configurados, el activo y las claves persisten (HKDF de `master.key`) | `podman restart` + `GET /providers/native/active` antes/después | medio | proveedor configurado |

## 11. Sistema → Memoria (MEM) — 6 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| MEM-01 | Listar memoria | `/sistema?tab=memoria` tras varios turnos de chat con hechos memorables | `GET /memory` 200 con entradas | captura | bajo | turnos de chat previos |
| MEM-02 | Buscar en memoria | Campo de búsqueda | `GET /memory/search?q=` filtra correctamente | captura | bajo | — |
| MEM-03 | Ver entrada completa | Abrir un item de memoria (Drawer) | `GET /memory/{entry_id}` con `{target}:{entry_index}` | captura | bajo | — |
| MEM-04 | Editar entrada | Modificar contenido → guardar | `PUT /memory/{entry_id} {content}` 200 en caso normal | captura antes/después | medio | — |
| MEM-05 | Editar entrada con contenido inyectado/PII | Introducir un patrón que dispare el guard de PII/inyección | 400, la edición se rechaza con mensaje claro | captura del rechazo | medio | payload de prueba con PII simulada (no real) |
| MEM-06 | Olvidar entrada | Botón eliminar sobre un item | `DELETE /memory/{id}` 200; desaparece de la lista y de `GET /memory` | captura | bajo | — |

## 12. Sistema → Archivos (FILES) — 6 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| FILES-01 | Listar workspace | `/sistema?tab=archivos` | `GET /workspace/files` 200, árbol navegable | captura | bajo | — |
| FILES-02 | Navegar a subcarpeta | Click en una carpeta | `GET /workspace/files?path=` actualiza el listado | captura | bajo | — |
| FILES-03 | Subir fichero | Drag&drop o selector → subir | `POST /workspace/files` (multipart) 200; aparece en el listado | captura | bajo | — |
| FILES-04 | Subir varios a la vez | Subir 3 ficheros de golpe | Los 3 se suben, contador de éxitos correcto (`ok += 1` por fichero) | captura | bajo | — |
| FILES-05 | Descargar fichero | Click en descarga | `GET /workspace/download?path=` sirve el fichero binario correcto | comparación de hash local vs descargado | bajo | fichero subido en FILES-03 |
| FILES-06 | Descargar sin `path` | `curl -i .../workspace/download` sin query param | 422 | respuesta HTTP | bajo | — |

## 13. Anuncios (ADS) — 5 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| ADS-01 | Sin MCP de Ads conectado | Instancia con `--no-companion` y sin conectar managed-remote | Sidebar NO muestra "Anuncios"; navegar a `/anuncios` a mano muestra `ads.empty.title` con CTA a Herramientas | captura | bajo | instancia INST-07 |
| ADS-02 | Panel embebido tras conectar (MCP-08) | `/anuncios` con `safent-ads` conectado | `iframe` carga el panel de Safent Ads (`origin` del managed-remote); quick-links `ads.panel.open/connections/proposals` abren en pestaña nueva | captura del iframe + de las 3 pestañas nuevas | medio | companion Ads arriba |
| ADS-03 | Conectar cuentas Google/Meta dentro del panel | Dentro del iframe, flujo de conexión de cuentas publicitarias | El panel de Safent Ads completa OAuth de Google Ads/Meta Ads | captura del panel (fuera del alcance de este repo, es la SPA externa) | alto | cuentas reales de Google Ads/Meta Ads `[DUEÑO]` |
| ADS-04 | Elegir modelo para el agente de Ads | `Proveedores` con un modelo activo, luego volver a Anuncios | El agente de Ads puede razonar sobre campañas | captura de una propuesta generada | alto | proveedor de modelo real `[DUEÑO]`, cuentas Ads conectadas |
| ADS-05 | Iframe bloqueado por X-Frame-Options | Simular un origin que deniegue el framing | El `<iframe>` queda en blanco SIN evento JS detectable; el fallback `ads.iframe.fallback` + link de apertura directa sigue disponible | captura del iframe vacío + del link funcionando | bajo | — |

## 14. CLI `safent` (CLI) — 15 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| CLI-01 | `safent` / `safent open` | `SAFENT_NAME=safent-repaso2 safent open` | Arranca si estaba parado, abre el navegador en la URL con `?k=` | captura de terminal + navegador | medio | — |
| CLI-02 | `safent url` | `safent url` | Imprime la URL lista con `?k=` sin abrir navegador; arranca si estaba parado | salida de terminal | bajo | — |
| CLI-03 | `safent start` | `safent start` | Arranca sin abrir navegador | salida + `podman ps` | bajo | — |
| CLI-04 | `safent stop` | `safent stop` | Contenedor detenido, datos en el volumen intactos | `podman ps -a` | medio | — |
| CLI-05 | `safent restart` | `safent restart` | Equivalente a `stop`+`open`; healthz vuelve a 200 | timestamps | medio | — |
| CLI-06 | `safent status` | `safent status` | Reporta corriendo/parado + puerto | salida de terminal | bajo | — |
| CLI-07 | `safent logs` | `safent logs` | `podman logs -f` en streaming | captura de unas líneas | bajo | — |
| CLI-08 | `safent companion status` | `safent companion status` | Estado del companion (contenedores + `/mcp/health`) | salida de terminal | bajo | companion arriba |
| CLI-09 | `safent companion update` | `safent companion update` | Pull de imagen del companion + recreate | salida + timestamps | medio | red de salida del host |
| CLI-10 | `safent companion rotate` | `safent companion rotate` | Nuevo bearer `/mcp` en ambos lados; `ads-api` reiniciado; `GET /mcp` en Safent sigue conectado tras la rotación | captura del bearer antes/después (redactado) | medio | companion arriba |
| CLI-11 | `safent companion remove` | `safent companion remove` | Companion parado y eliminado; estado (`~/.safent/companions/ads`) conservado | `podman ps -a` + `ls` del estado | medio | companion arriba |
| CLI-12 | `safent companion remove --purge` | `safent companion remove --purge` | Companion eliminado Y estado (TLS/bearer) purgado | `ls` del directorio de estado (debe no existir) | alto (destructivo) | companion arriba |
| CLI-13 | `--no-companion` como primer argumento | `safent --no-companion status` | Salta el provisioning del companion para ese comando | salida de terminal | bajo | — |
| CLI-14 | `safent pair <code>` | `safent pair CODIGO-FALSO` | Error de red/tenant inválido esperado (no hay control plane real); con código real de tenant, pasaría a `associate` | captura del error | medio | código de tenant real + `cloud.safent.run` alcanzable `[DUEÑO]` |
| CLI-15 | `safent unpair` | `safent unpair` | Revierte a `community`; `hermes-config-sync.service` vuelve a inactive | `GET /instance/features` antes/después | medio | instancia emparejada (CLI-14 exitoso) |

## 15. Desktop shell (Tauri) (DESK) — 4 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| DESK-01 | Arranque del shell nativo | Lanzar el binario de `desktop/` (o `pnpm tauri dev`) apuntando a `safent url` | Webview nativo (WKWebView/WebView2/WebKitGTK) carga la SPA sin chrome de navegador | captura de la ventana nativa | medio | build de Tauri para la plataforma del tester |
| DESK-02 | Round-trip de chat dentro del webview | Enviar un mensaje de chat dentro del shell nativo | SSE funciona dentro del webview embebido (no solo en Chrome/Firefox) | vídeo corto | alto | modelo activo |
| DESK-03 | Versión inyectada / aviso de actualización | Con una versión más nueva publicada, abrir el shell | `__safentLatestVersion` inyectado desde el host (que sí tiene internet) hace aparecer el aviso `sysupdate.available` aunque el contenedor esté aislado de red | captura del footer con el aviso | bajo | acceso a internet del HOST (no del contenedor) |
| DESK-04 | Comando de clipboard nativo (`#[tauri::command]`) | Copiar/pegar en el navegador enjaulado vía VNC desde el shell nativo | El comando de clipboard corre fuera del hilo de UI (no cuelga la ventana si la herramienta de clipboard se bloquea) | vídeo + verificar que la ventana sigue respondiendo durante la operación | bajo | — |

## 16. Distribución multiplataforma (DIST) — 6 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| DIST-01 | `get-safent.sh` en Linux | `curl -fsSL .../get-safent.sh \| sh` en una VM Linux limpia con podman/docker ya instalado | Instala `safent` en el PATH, arranca con la jaula de seguridad, abre el navegador | captura completa de la instalación | alto | VM Linux limpia |
| DIST-02 | `get-safent.sh` en macOS (Apple Silicon) | Idem en Mac ARM | `podman machine init --rootful --cpus 4 --memory 8192 --disk-size 60` automático si no existe; agente `launchd` instalado (`run.safent.agent.plist`) | captura + `launchctl list \| grep safent` | alto | Mac Apple Silicon `[DUEÑO]`/lab |
| DIST-03 | `get-safent.sh` en macOS Intel | Idem en Mac Intel | Según `INSTALL-mac.md`, **NO soportado oficialmente** (solo Apple Silicon) — confirmar que el instalador aborta con mensaje claro, no falla a medias | captura del mensaje | medio | Mac Intel `[DUEÑO]`/lab |
| DIST-04 | `safent.ps1` en Windows (WSL2) | Ejecutar `safent.ps1` en PowerShell con WSL2 kernel ≥6.6 y Podman Desktop instalados | Detecta WSL2, valida versión de kernel (Landlock), delega en el podman de la distro | captura completa | alto | máquina Windows con WSL2 `[DUEÑO]`/lab |
| DIST-05 | Docker en vez de Podman | `docker run` con las flags de `run-safent.sh` adaptadas (`--systemd`/`unmask` no soportadas por Docker) | **Verificar la contradicción documental**: README dice "Podman o Docker Desktop" pero `docker run --help` no admite `--systemd`/`unmask` — confirmar si el arranque falla o degrada | captura del error o del arranque degradado | medio | Docker Desktop instalado |
| DIST-06 | Rootless podman en Linux | `run-safent.sh` con el podman del usuario en modo rootless (sin sudo) por defecto | Toda la verificación previa (`matriz-en-vivo.md`) fue rootful — confirmar si rootless arranca igual o si Landlock/nft fallan | captura completa + comparación con INST-01..03 | alto | Linux con podman rootless por defecto, ~8GB libres para copiar la imagen a otro storage si hace falta |

## 17. Enterprise / pairing (ENT) — 4 filas

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| ENT-01 | Estado por defecto (community) | `GET /instance/features` en fresco | `edition:"community"`, `associated:false` | captura | bajo | — |
| ENT-02 | Emparejar con tenant real | `SAFENT_CLOUD_ENDPOINT=<endpoint-tenant> safent pair <code-real>` | `hermes-config-sync.service` pasa a activo (`ConditionPathExists` satisfecha); `edition:"associate"` | captura + `systemctl --user status hermes-config-sync` | alto | tenant + `cloud.safent.run` (o endpoint propio) alcanzable `[DUEÑO]` |
| ENT-03 | Modelo gestionado tras emparejar | Con ENT-02 hecho, ver Proveedores | Chip `chat.model.org_managed_label`, no editable (ver PROV-11) | captura | medio | ENT-02 |
| ENT-04 | Desemparejar | `safent unpair` | Vuelve a `community`; el modelo gestionado deja de aplicarse | captura antes/después | medio | ENT-02 |

## 18. Copias / exportación (BKP) — 2 filas — verificar si sigue FALTA

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| BKP-01 | Backup/restore vía CLI o API | Buscar `safent backup`/`safent restore` en el CLI y `POST /backup`/`/restore` en la API | **Verificar si sigue sin existir** (hallazgo #9 de `matriz-en-vivo.md`); si sigue faltando, documentar como gap abierto, no como bug nuevo | `safent --help` completo + `grep` de rutas en `main.py` | medio | — |
| BKP-02 | Exportación manual del volumen (workaround documentado) | `podman volume export safent-repaso2-data -o backup.tar` → recrear instancia nueva → `podman volume import` | El volumen exportado/importado conserva providers, skills, MCP, memoria, cron | comparación de `GET /skills`, `/mcp`, `/tasks/configured` antes/después | medio | espacio en disco para el tar |

## 19. SSH gobernado sobre tailnet (SSH) — 3 filas — capacidad backend, sin disparador todavía

| id | capacidad | pasos exactos | resultado esperado | evidencia | riesgo | dependencias |
|---|---|---|---|---|---|---|
| SSH-01 | Binario `ssh` disponible en la imagen | `podman exec … which ssh && ssh -V` | Presente (`openssh-client` — verificar si ya se añadió al `Containerfile` según `ssh-v2.md` "deferred/follow-ups", o si sigue faltando) | salida de terminal | medio | — |
| SSH-02 | Ruta de invocación desde el agente — **NO PROBABLE en esta oleada** | Intentar disparar `tailnet_ssh`/`tailnet_file_get`/`tailnet_file_put` desde un prompt de chat | Según `specs/022-tailnet-connectivity/ssh-v2.md`: el caso de uso (`TailnetSshUseCase`) existe en `src/hermes/tailnet_ssh/` pero **NO está cableado al esquema de tools del LLM** (vive en el paquete externo `hermes-agent`) — el agente no puede invocarlo todavía; documentar como GAP, no repetir el intento | captura del intento fallido (el agente no reconoce la capacidad) | bajo | — |
| SSH-03 | Gestión del allow-list de hosts SSH — **NO PROBABLE, sin UI/API** | Buscar en Seguridad o en `/api/v1` alguna ruta para listar/revocar `ssh-allowlist.json` | Confirmado en `ssh-v2.md`: `JsonHostAllowlistStore.revoke()` existe en código pero sin endpoint HTTP ni frontend — no hay nada que probar en UI todavía | captura del intento de búsqueda (nada encontrado) | bajo | — |

---

## Orden de ejecución

1. **Instalación limpia** (§0 INST-01..08) sobre una instancia nueva, nombre nunca reutilizado.
2. **Smoke test** — `INST-01/02/04/05`, `CHAT-01`, `AGT-01`, `MCP-01`, `SEG-01/18` — confirma que la imagen respira antes de entrar en detalle.
3. **Por sección**, en el orden del sidebar: Chat → Agentes → Capacidades (Habilidades → Integraciones → Herramientas → En vivo) → Sistema (Seguridad → Coste → Modelo de IA → Memoria → Archivos) → Anuncios → CLI → Desktop → Distribución → Enterprise → SSH.
4. **Positivas antes que negativas** dentro de cada fila relacionada (p.ej. `SEG-08` MFA antes que `SEG-09/10` códigos incorrectos).
5. **Destructivo al final, en este orden exacto:** `SEG-02..05` (freno de emergencia, ya que bloquea todo lo demás mientras esté activo — probarlo temprano en su sección pero SIN dejarlo activado; si algo falla al liberarlo, todo el resto de pruebas posteriores queda contaminado) → `CLI-12` (`companion remove --purge`) → `SKL-05`/`MCP-10` (desinstalar) → `AGT-05`/`AGT-13` (eliminar agente/tarea) → `ENT-04` (unpair) → `UPD-04`/`UPD-06` (desinstalar vía UI y CLI) → destruir la instancia (`podman rm -f`, `podman volume rm`).
6. Si `SEG-02` (activar freno) se prueba fuera de orden, ejecutar `SEG-04` (liberar) inmediatamente después, antes de continuar con cualquier otra fila — ninguna prueba de `CHAT-*`/`AGT-*`/`SKL-*`/`MCP-*` puede pasar con el freno activo.

### Comando exacto para lanzar el contenedor fresco

`matriz-en-vivo.md` narra los parámetros de su instancia aislada (`safent-repaso`, volumen `safent-repaso-data`, puerto `127.0.0.1:18081`) pero no deja el one-liner; reconstruido a partir de las variables reales de `ops/container/run-safent.sh` (`SAFENT_NAME`→`NAME`, `VOLUME="${NAME}-data"`, `HOST_PORT` posicional, siempre publicado en `127.0.0.1:${HOST_PORT}:7517`):

```sh
cd /home/luiscorrea-dev/Desktop/lumen-runtime-next
SAFENT_NAME=safent-repaso3 \
SAFENT_COMPANION_STATE=/var/tmp/safent-repaso3/companions/ads \
./ops/container/run-safent.sh ghcr.io/devwspito/safent:<tag-a-probar> 18083
```

Desmontaje al final: `podman rm -f safent-repaso3 && podman volume rm safent-repaso3-data && podman network rm safent-companions 2>/dev/null; rm -rf /var/tmp/safent-repaso3`. Usar SIEMPRE un `SAFENT_NAME` nuevo (nunca `safent` a secas) para no tocar ninguna instancia de producción/desarrollo existente en la máquina.
