# Radiografía de safent-runtime — mapa, duplicación, partición y plan por oleadas

Rama `feat/safent-next`, HEAD `90a7f86`, 2026-09-10. Solo lectura. Entrada: `inspeccion-estatica.md`,
specs 022/023/024 y el árbol upstream fijado (`Containerfile:67`, `HERMES_AGENT_COMMIT=2237be35…` =
hermes-agent 0.21.1). Método: grafo de imports por AST (absolutos, relativos y perezosos),
alcanzabilidad transitiva desde los 34 entrypoints reales (`ExecStart`, `[project.scripts]`,
`python3 -m` de `ops/`), `ruff` y `mypy`.

---

## 1. Mapa

**131.558 líneas · 636 módulos · 38 paquetes** bajo `src/hermes` (+3 raíz, 276 L). Alcanzable desde
un entrypoint: **110.061 L (368 módulos)**. No alcanzable: **21.497 L (268 módulos, 16 %)**, de las
cuales **18.328 sólo las sostienen sus propios tests** y **3.169 no las referencia nada**.

| Paquete | L / F | Responsabilidad | Depende de | Tests f/casos | No alc. |
|---|---:|---|---|---|---:|
| `agents_os` | 23.229 / 60 | Verbos D-Bus, consentimiento, roster, OTA, skill-scan | security_center 23, shell_server 21, runtime 16 | 154 / 2.188 | 2.858 |
| `shell_server` | 19.936 / 102 | Plano de control HTTP (FastAPI) | agents_os 35, tasks 18, capabilities 11 | 80 / 1.160 | 997 |
| `runtime` | 14.891 / 26 | Daemon: composición, motor Nous, hook de seguridad | capabilities 48, shell_server 22, agents_os 20 | 59 / 939 | 0 |
| `capabilities` | 9.701 / 40 | Broker: riesgo, HITL, aprobación, auditoría | agents_os 26, runtime 6 | 92 / 1.358 | 289 |
| `tasks` | 9.618 / 46 | Cola, worker pool, triggers autorizados | agents_os 7, capabilities 4 | 79 / 1.110 | 636 |
| `browser` | 9.249 / 53 | Navegador gobernado: CDP, selectores firmados, replay | tokenizer 4, security 2 | 40 / 347 | **8.103** |
| `lumen` | 5.964 / 33 | Compositor Wayland/QML, overlay, driver CUA | shell_server 7 | 2 / 73 | 69 |
| `tui` | 4.298 / 28 | Consola local del cage | shell 1 | **1 / 11** | 32 |
| `config_sync` | 4.239 / 8 | Config firmada, bandeja de delegaciones | instance 10, shell_server 5 | 16 / 396 | 13 |
| `training` | 3.553 / 29 | Compilación/evolución (GEPA) de skills | workspace 3 | 22 / 237 | 1.839 |
| `platforms` | 2.841 / 16 | Modelos de plataforma por tenant | capabilities 2 | 7 / 111 | 413 |
| `security_center` | 2.798 / 21 | Escaneo de paquetes/skills/MCP, CVE | agents_os 2 | 6 / 67 | 92 |
| `workspace` | 2.751 / 21 | Escritorio remoto, audio, WebRTC | training 1 | 9 / 75 | **2.751** |
| `egress_proxy` | 2.286 / 14 | Proxy de salida auditado | agents_os 1 | 8 / 122 | 20 |
| `shell` | 2.262 / 10 | Cliente del runtime para superficies | — | 8 / 184 | 1.045 |
| `mcp` | 1.990 / 15 | Cliente MCP stdio, registro, clasificador | capabilities 3, agents_os 3 | 13 / 183 | 11 |
| `agents` | 1.896 / 9 | Roster por defecto, personas | prompts 3 | 14 / 273 | 11 |
| `instance` | 1.278 / 7 | Identidad, edición, features | agents_os 4 | 8 / 122 | 17 |
| `security` | 1.160 / 6 | Landlock, launcher MCP | agents_os 2 | 6 / 114 | 8 |
| `execution` | 1.079 / 9 | Contexto de ejecución persistido | — | 6 / 138 | 785 |
| `memory` | 937 / 8 | Memoria por tenant con puerta PII | agents_os 2 | 4 / 88 | 59 |
| `providers` | 843 / 11 | Catálogo canónico, vault | shell_server 7 | 5 / 85 | 90 |
| `autonomous` | 783 / 13 | Navegador de sólo lectura | — | 3 / 24 | **783** |
| `package_store` · `integrations` · `clipboard_bridge` · `prompts` | 1.818 / 19 | Paquetes gobernados, Composio, portapapeles, prompt | domain | 13 / 314 | 38 |
| `domain` · `cli` · `policies` · `notifications` · `tokenizer` | 1.216 / 14 | Tipos compartidos, CLI, políticas, PII | domain | 58 / 857 | **262** |
| 6 daemons finos + `testing` | 666 / 15 | Auditoría WORM, whisper, consentimiento, OTA, dobles | agents_os | 11 / 78 | **199** |

### Ciclos

**Un único SCC de 16 paquetes**: `agents_os · browser · capabilities · config_sync · instance ·
mcp · memory · platforms · providers · runtime · security · security_center · shell_server ·
tasks · training · workspace`. Es el 78 % del código. Dentro hay **19 pares mutuos**; los cuatro
que lo sostienen:

- `runtime ↔ capabilities` (48 / 6) — el daemon construye el broker y el broker vuelve a `runtime`.
- `shell_server ↔ agents_os` (35 / 21) — la API llama al wiring D-Bus y el wiring importa routers
  de la API (`dbus_runtime_service.py:1129 → shell_server.setup.api`, `:2529 →
  shell_server.remote_access_tunnel.api`): **infraestructura importando presentación**.
- `runtime ↔ shell_server` (22 / 2) y `capabilities ↔ agents_os` (26 / 6).

Ningún ciclo es accidental: son el síntoma de que **no hay raíz de composición**. Romperlos no exige
rediseñar el dominio, sino sacar el cableado a un paquete que nadie importe (oleada 4).

### Violaciones de capa

- **application → infrastructure: 29.** La peor, por ser el corazón de la gobernanza:
  `capability_broker.py:73,83,84,85` importa `surface_adapter_dispatcher`,
  `composio_surface_adapter`, `os_native_dispatcher` y `mcp.infrastructure.mcp_surface_adapter`;
  `:745,778`, `enterprise_approval_routing`. El broker declara puertos y elige adaptadores.
- **infrastructure → presentation: 2** (los dos `dbus_runtime_service` citados arriba).
- **domain → application: 1** — `capabilities/domain/capability.py:13`.
- **domain con framework: 1** — `training/domain/skill_md_document.py:48` importa `yaml`.

El resto respeta domain → application → infrastructure: la deuda de capas es pequeña y localizada.
**La deuda real es el tamaño de clase y el cableado disperso.**

### Cableado disperso (no hay composition root)

`runtime/__main__.py::_run` (**629 L**) + 20 funciones `_build_*`; `shell_server/main.py::create_app`
(**914 L**); `security_center/application/composition.py` (65, capa equivocada);
`providers/infrastructure/factory.py` (54, muerto); `shell_server/training/in_session_factory.py`
(141, muerto, con el `FakeWhisperBackend`). Cinco sitios construyen el mismo grafo de objetos.

---

## 2. Duplicación con hermes-agent 0.21.1

| # | Safent | Upstream (commit fijado) | Veredicto |
|---|---|---|---|
| 1 | `mcp/` cliente stdio (1.990 L; `stdio_mcp_client.py` 660) | `tools/mcp_tool*.py` (16 módulos, ~4 k) | **KEEP** — `nous_engine.py:46`: todo MCP pasa por el broker; es el chokepoint del producto. |
| 2 | Catálogo/registro MCP en `dbus_runtime_service.py:6834-7124` + `mcp/infrastructure/registry_client.py` | `hermes_cli/mcp_config.py` (913), `mcp_catalog.py` (677) | **REPLACE** — Safent ya persiste en `config.yaml → mcp_servers`: upstream como única fuente, Safent sólo allowlist + seed del compañero. Riesgo bajo (mismo fichero). |
| 3 | `providers/domain/catalog.py` (240) + `canonical.py` (64) · `infrastructure/factory.py` (54) | `hermes_cli.auth.PROVIDER_REGISTRY` | **KEEP** el catálogo (documenta un bug upstream: `tests/providers/test_catalog.py:88`, `openai-api`→`openrouter`) · **DELETE** `factory.py`, muerto. |
| 5 | Workers OAuth Codex/xAI/Nous: `dbus_runtime_service.py:5916,5989,6059` (~300 L, `threading.Thread`) | `hermes_cli/auth_codex.py` (804), `auth_device_flow.py` (337), `auth_xai.py` (582), `auth_nous.py` (1.487) | **REPLACE** — port congelado de `web_server.py` (comentario en `:5991`). Dejar sólo la máquina de estados D-Bus. Riesgo medio: upstream asume TTY, hace falta adaptador no interactivo. |
| 6 | `memory/` (937): `TenantMemoryStore` + `NousMemoryBridge` | `tools/memory_tool.py` (350), `memory_tool_store.py` (417) | **KEEP** — upstream escribe global y sin escaneo PII (`nous_memory_bridge.py:8-20`, opción B). El aislamiento por tenant es requisito. |
| 7 | `tasks/triggers/` (cron one-shot) | `cron/jobs.py` (3.172), `cron/scheduler.py` (3.919) | **REPLACE parcial** — `timer_trigger_source.py:260-272` ya llama a `cron.jobs`: único catálogo. Safent conserva `trigger_gate.py` (432), la autorización. |
| 8 | `shell_server/skills/` (1.369) + `cowork/skills_api.py` (316) | `tools/skills_hub*.py` (14 módulos, 389 L el núcleo) | **KEEP** — ya delega búsqueda/instalación; lo propio es firma y gobernanza. |
| 9 | `training/` GEPA + skill compiler (3.553) | sin equivalente | **KEEP** el núcleo · **DELETE** las 1.839 L no alcanzadas (`training_orchestrator.py`, `silero_vad.py`, mitad de `evolution/`). |
| 10 | `agents_os/.../terminal_surface_adapter.py` | `tools/terminal_tool_backends.py` (325) | **KEEP** — upstream no confina; Safent envuelve en `systemd-run --scope` + Landlock. |
| 11 | `browser/` (9.249; **8.103 no alcanzables**) | `tools/browser_tool*.py` (20 módulos), ya usados vía `runtime/cycle_cdp_context.py:52` | **DELETE** la mitad spec-002 (`application/session.py` 571, `orchestrator.py` 398, `self_healing.py` 386, `hitl_loop.py` 337, `replay_runner.py` 280, `discovery_runner.py` 266, `openshell_sandbox_provider.py` 488, `ocr/azure_di_pipeline.py` 213) · **KEEP** adaptadores CDP, `signed_selector_registry`, `storage_state_crypto`. |
| 12 | Delegación: `capabilities/tool_delicacy.py`, `tasks/.../delegation_approval_service.py` | `tools/delegate_tool*.py` (7 módulos, 724 L el núcleo) | **KEEP** — sub-agente con broker propio (`tool_delicacy.py:56`). |
| 13 | Config · `shell_server` + `frontend/` (75 TS) · `tui/` (4.298) | `hermes_cli/config.py` (3.891, ya usado) · `gateway/` (81 k) + `web/` · `tui_gateway/` (25.883) | **KEEP** los tres — superficies distintas: upstream es relay de chat, Safent es plano de control y consola del cage. `tui/` es el paquete peor probado del repo (1 fichero, 11 casos) y suma 164 errores mypy. |
| 16 | `workspace/` (2.751, **100 % no alcanzable**): `selkies_gateway.py` y `kasmvnc_gateway.py` rivales | — | **DELETE** el paquete entero. |
| 17 | `autonomous/` (783, **0 consumidores en `src`**) · `native_capabilities/` (120, 0 tests) · `policies/layer.py` (237) · `apps/agentic-panel/` byte-idéntico a `apps/agentic_panel/` | `tools/browser_*` | **DELETE** — plegar `click_intent_classifier` en `browser/`; verificar `policies` contra despacho dinámico antes de borrar. |

**Recuento: KEEP 9 · REPLACE 3 · DELETE 5** (más los 2 borrados parciales de las filas 9 y 11).

---

## 3. Hotspots y partición

20 ficheros superan 800 líneas. El patrón es siempre el mismo — **clase-dios o función de
composición**, no complejidad ciclomática (peor `C901` = 20).

**`agents_os/infrastructure/dbus_runtime_service.py` — 8.506 L.** `DbusRuntimeServiceWiring` ocupa
`:225-5406` con 184 métodos; después ~3.100 L de helpers. Destino `agents_os/infrastructure/wiring/`,
en este orden (strangler, un módulo por PR, el original re-exporta hasta el final):
1. `oauth_workers.py` ← `:5916, :5989, :6059` (~300 L). Costura: los tres workers sólo tocan
   `_SESSIONS`; extraer primero un `OAuthSessionStore` explícito. Pinea: `tests/agents_os/`
   (verbos `StartProviderOAuth`/`PollProviderOAuth`).
2. `package_argv_allowlist.py` ← `:6311, :6325, :6372-6429, :6551, :6712`. Puro y sin estado, el
   corte más barato. Pinea: `tests/security/test_skill_signature_hardening.py`.
3. `mcp_registry.py` ← `_neus_*` `:6834-7124` + seed del compañero, y `scheduled_tasks.py` ←
   `_neus_cron_*` `:7160-7341`: **después** de la oleada 2 (filas 2 y 7), porque encogen al delegar.
4. `hermes_env_io.py` ← `:6166, :6186`.
Queda el despacho de verbos (~5.100 L): se parte por dominio (providers · skills · chat · seguridad ·
sistema) sólo cuando exista la raíz de composición.

**`runtime/nous_engine.py` — 4.351 L.** `NousReasoningEngine` (1.205 L, 22 métodos) +
`GovernedAIAgent` (798 L, 16). Primer corte, barato: los tres parches de tools (`:3841, :3885, :3946`)
a `runtime/agent_tool_patches.py` — es la superficie que verifican las dos pruebas de broker rotas por
la inyección de `sys.path`. Segundo: `_wire_sequential_gate:3582`, `_wire_inline_branch_gates:3764`,
`_make_external_sequential_wrapper:4211` → `runtime/tool_gating.py`.

**`runtime/__main__.py` — 2.982 L.** `_run` (629 L) + `_build_*`. Destino `runtime/composition/`:
`brokers.py` (`_build_real_broker:967`, 215 L), `engine.py` (`_build_nous_engine:551`),
`adapters.py` (`_start_dbus_adapter_if_available:2009`, 232 L), `triggers.py`
(`_start_trigger_sources:2300`), `confinement.py` (`_assert_confinement_active:2540`). `_run` queda como orquestador de ~120 L. Es el paquete que **nadie debe importar** — con eso
desaparecen los ciclos `runtime ↔ capabilities` y `runtime ↔ shell_server`.

**`shell_server/main.py` — 1.573 L**, `create_app` 914 L: un módulo por familia de routers en
`shell_server/composition/`. Pinea: `tests/contract/` + matriz en vivo.

**`agents_os/infrastructure/dbus_fast_runtime_adapter.py` — 2.338 L**, `Runtime1ServiceInterface`
con **140 métodos**. No partir por tamaño: partir por **puerta de validación** (§4.6).

Segundo turno: `runtime/security_hook.py` (2.264), `agent_loop_orchestrator.py` (1.357; clase de
959 L, 27 métodos), `capability_broker.py` (1.006; 691 L de clase), `lumen/__main__.py` (1.234).

---

## 4. Endurecimiento (fail-closed)

1. **REST: la autorización es por método, no por recurso.** `shell_server/main.py:775` —
   `if request.method in _MUTATING_METHODS and path.startswith("/api/v1/")`. **Todo GET a
   `/api/v1/*` va sin credencial**: cadena de auditoría, memoria, conversaciones, proveedores,
   dominios de egress, estado MFA. Sólo lo mitiga el bind a `127.0.0.1` (`main.py:1553`), que es un
   env override (`HERMES_SHELL_BIND_HOST`) sin validación. Arreglo: dependencia de autenticación en
   el `APIRouter` (33 routers, **0** con `dependencies=`) y rechazo de binds no-loopback.
2. **Las claves del proveedor viven en el entorno del daemon y las hereda la terminal del agente.**
   `dbus_runtime_service.py:816, 2405, 2449` y `nous_engine.py:1046` hacen `os.environ[env_var] = key`;
   `terminal_surface_adapter.py:442` lanza `create_subprocess_exec` **sin `env=`** y la ruta
   "confinada" (`:435`, `systemd-run --scope`) también hereda. El agente lee `OPENAI_API_KEY` con un
   `env`. Arreglo: resolver la clave por ciclo desde el vault y pasar `env=` allowlisteado, como ya
   hace `stdio_mcp_client._build_mcp_env`.
3. **Fail-open del confinamiento.** `security/landlock_loader.py:429` (kernel sin Landlock) y `:435`
   (`UnsupportedArchError`) devuelven `0`, el mismo código que el éxito, mientras `:414` sí devuelve
   `2` con seccomp. Contrato de tres estados: aplicado / omitido / fallo. *(En vuelo.)*
4. **Egress abierto por defecto.** `egress_proxy/__main__.py:70` — el default es `OPEN_LOGGED`. Un
   valor inválido falla cerrado (`:165-172`), pero la **ausencia** falla abierto. Invertirlo.
5. **`{ok:false}` con 200 — y su versión peor.** `egress_api.py:291, 323, 350` y
   `providers_api.py:213` *(en vuelo)*. Debajo queda lo que ese arreglo no cubre: `:294, :328, :355,
   :366` devuelven `{"ok": True, …, "pushed": False}` cuando `_apply_network_mode()` falló — **el
   dominio se persistió pero no se aplicó a nftables** y el cliente ve éxito. Resultado tipado
   `GrantOutcome(persisted, enforced)` y 5xx si `enforced` es falso.
6. **D-Bus: 140 verbos, 4 validan.** El adaptador ya tiene la maquinaria (`_parse_json_bounded:111`,
   `_assert_allowed_keys:130`, `_assert_string_lengths:139`, `DbusInputValidationError:104`) pero es
   opt-in: hay `json.loads` crudo sobre cadenas del llamante en `:546, :688, :821, :1210`. Decorador
   obligatorio por verbo, con esquema. El uid sí se comprueba (`:2278`): falla la **validación**.
7. **`sh -c` arbitrario expuesto a QML.** `lumen/compositor/sys_manager.py:181, 190, 194` —
   `Popen(cmd, shell=True)` en `@Slot` de un `QObject` publicado en todo el contexto QML.
8. **9 subprocess sin timeout**: `os_native_dispatcher.py:200`, `trivy_cve_scanner.py:198, 252`,
   `terminal_surface_adapter.py:450`, `bootc_updater.py:101, 154`, `agent_browser_cli.py:258`,
   `ocr/tesseract_pipeline.py:210, 243`.
   Las llamadas HTTP **sí** tienen timeout en todos los sitios; no hay `verify=False` ni `CERT_NONE`.
9. **Auditoría de egress sin encadenar** (`egress_proxy/application/ports.py:23`), **blocklist que
   nunca se refresca** (`blocklist_loader.py:15`) y **sin rotación de logs** (ni `RotatingFileHandler`
   ni `logrotate` en `src/` ni `ops/`: SQLite y cadena WORM crecen sin poda). Con 72 `except…: pass`
   y 625 `noqa: BLE001`, un fallo de escritura de auditoría se traga en silencio.

---

## 5. Ratchet de tooling

**Ruff.** `src`: 1.740 errores, **624 autofixables**; repo completo 3.789 / 1.407. (a) Commit
mecánico `ruff check --fix .` sin tocar nada más; (b) job de lint bloqueante desde ese commit;
(c) las reglas ruidosas a mano, por rentabilidad: `E501` 242, `PLR2004` 143, `PLC0415` 135,
`ARG002` 93, `SIM105` 83, `S110` 72 — `S110` y `PLC0415` se arreglan, no se silencian. Los 22
`F821` son anotaciones-string de firmas D-Bus: `per-file-ignores`, no `noqa` línea a línea.

**Mypy.** `strict = true` (`pyproject.toml:137`) y nunca ejecutado: **1.885 errores en 235 de 636
ficheros**. Baseline por paquete (sólo puede bajar): `agents_os` 577 · `shell_server` 405 ·
`runtime` 169 · `tui` 164 · `lumen` 156 · `shell` 118 · `capabilities` 68 · `tasks` 52 · `browser` 46 ·
`training` 37 · `config_sync` 37 · `instance` 25 · `platforms` 19 · `integrations` 15 ·
`security_center` 13 · resto ≤9. **13 paquetes ya están a cero** (`agents`, `autonomous`,
`bootc_updater_service`, `cli`, `clipboard_bridge`, `domain`, `native_capabilities`, `policies`,
`prompts`, `providers`, `testing`, `tokenizer`, `whisper_service`): puerta dura hoy mismo. El resto,
ratchet con fichero de baseline. Antes, un `[[tool.mypy.overrides]]` que apague `name-defined` en
`dbus_fast_runtime_adapter` (184 falsos positivos de dbus-fast); sin eso, un `name-defined` real es
indistinguible del ruido.

**Higiene de la suite.** (a) `tests/providers/test_openai_resolution.py:24` inyecta un venv ajeno en
`sys.path` → `monkeypatch.syspath_prepend` *(en vuelo)*. (b) Registrar el marcador `security` en
`pyproject.toml:157-166` (8 ficheros lo usan bajo `--strict-markers`). (c) `tests/security` (636
casos), `tests/tasks`, `tests/providers`, `tests/contract`, `tests/e2e`, `tests/security_center`,
`tests/tui` y `tests/vm` no están en ninguna puerta. (d) Reloj inyectable en el coalescer para
`tests/tasks/test_chat_dbus_streaming.py` (54 s de 57). (e) `fail_under = 80` está configurado
(`pyproject.toml:167`) y nunca se ejecuta. Sólo 3 ficheros tocan red y no hay bombas de tiempo.

---

## 6. Plan por oleadas

Cada oleada ≤ 1 día de carriles paralelos, **sin solapamiento de ficheros** dentro de una oleada.
Garantía de comportamiento: suite completa (`PYTHONPATH=src python3 -m pytest tests -q
-p no:cacheprovider`) más `specs/025-safent-repaso/matriz-en-vivo.md` cuando exista.

**Oleada 0 — en vuelo, no replanificar.** `repaso-fixes-1` (pin de `hermes-agent` fuera de las deps
duras + tarball en CI, inyección de `sys.path`, allowlist de `gitleaks`, Landlock fail-closed,
`{ok:false}`→422) y los tres carriles de la spec 022 sobre `tailnet-022-base` (`tailnet-ops`,
`tailnet-egress`, `tailnet-ssh`). *Salida: `agents-os-edition.yml` y `secret-scan.yml` en verde.*
Todo lo demás depende de esto.

**Oleada 1 — borrar (menos código = menos que refactorizar).** L1a `refactoring-specialist` ·
`workspace/`, `native_capabilities/`, `autonomous/`, `apps/agentic-panel/`,
`providers/infrastructure/factory.py`; L1b · sólo `browser/` (los 8 módulos de §2 fila 11); L1c ·
sólo `training/` (1.839 L) y `shell_server/training/in_session_factory.py` (con él muere el
`FakeWhisperBackend`); L1d `qa-engineer` · sólo `tests/`, retira lo que sostenía lo borrado y los 4
puertos spec-002. *Salida: −13 k líneas; suite verde; cada entrypoint de `ExecStart` importa.*

**Oleada 2 — reemplazar por upstream.** L2a `backend-engineer` · `dbus_runtime_service.py:5900-6160`
→ `hermes_cli.auth_*`; L2b · `:6800-7130` + `mcp/infrastructure/registry_client.py` →
`hermes_cli.mcp_config`/`mcp_catalog` como única fuente; L2c ·
`tasks/triggers/application/timer_trigger_source.py` → `cron.jobs` como único catálogo.
*Salida: `test_broker_gate_hardening_iter3.py` verde; matriz en vivo sin regresión en OAuth de
proveedor, alta de MCP y tarea programada.*

**Oleada 3 — partir el wiring D-Bus.** `refactoring-specialist`, un carril por módulo de §3
(`oauth_workers`, `package_argv_allowlist`, `hermes_env_io` en paralelo; `mcp_registry` y
`scheduled_tasks` tras la oleada 2, ya encogidos). *Salida: `dbus_runtime_service.py` < 4.500 L,
ninguna prueba modificada.*

**Oleada 4 — raíz de composición y motor.** L4a `software-architect` + `backend-engineer` ·
`runtime/__main__.py` → `runtime/composition/`; L4b `backend-engineer` ·
`shell_server/main.py::create_app` → `shell_server/composition/`; L4c `backend-engineer` · parches de
tools de `nous_engine.py` → `runtime/agent_tool_patches.py`.
*Salida: el SCC de 16 baja a ≤ 8; `capability_broker` deja de importar adaptadores; prueba de
arquitectura que falla si alguien importa `composition`.*

**Oleada 5 — endurecimiento fail-closed.**
- L5a `security-engineer` · `shell_server/main.py` + los 33 `APIRouter` — autorización por ruta,
  default-deny en GET, validación del bind.
- L5b `security-engineer` · `dbus_runtime_service.py`, `nous_engine.py`,
  `terminal_surface_adapter.py` — secretos fuera de `os.environ`, `env=` allowlisteado.
- L5c `backend-engineer` · `egress_proxy/` + `egress_api.py` — `persisted`/`enforced`, default-deny,
  refresco de blocklist, hash-chain.
- L5d `security-engineer` · `dbus_fast_runtime_adapter.py` (validación como decorador) y
  `lumen/compositor/sys_manager.py` (allowlist de comandos).
- L5e `backend-engineer` · los 9 subprocess sin timeout + rotación de logs.
*Salida: threat-model gate en verde, con un caso de regresión por ítem.*

**Oleada 6 — ratchet de tooling.** L6a commit mecánico `ruff --fix`; L6b las 15 reglas a mano por
paquete; L6c `devops-engineer` — mypy con baseline + puerta dura en los 13 paquetes limpios; L6d
`qa-engineer` — marcador, directorios en la puerta, reloj inyectable, coverage.
*Salida: `ruff check .` = 0; mypy no sube nunca; suite < 40 s; coverage ≥ 80.*

**Oleada 7 — reverificación en vivo.** `qa-engineer` + `devops-engineer`: matriz en vivo completa
contra una imagen recién construida desde `ops/container/Containerfile` y arrancada con
`ops/container/run-safent.sh`, más cosign + SBOM y el invariante rojo de la spec 022 (desde cada
netns del agente, `127.0.0.1:1055` inalcanzable).

---

## 7. Supuestos y preguntas abiertas

**Supuestos** (decididos, documentados): el conjunto "no alcanzable" se calcula desde los entrypoints
declarados; los módulos cargados por despacho dinámico pueden aparecer como no alcanzables, así que la
oleada 1 sólo borra el subconjunto **sin entrypoint y sin prueba** (3.169 L) más lo que la inspección
estática ya confirmó. `policies/layer.py` y `execution/` van a verificación manual, no a la oleada 1.
La partición usa strangler con re-export, nunca big-bang.

**Para el dueño:**
1. ¿El navegador autónomo spec-002 (`browser/application/`, 8.103 L) está aparcado o cancelado? Si
   está aparcado, se archiva en una rama en vez de borrarse.
2. Las lecturas GET sin credencial (§4.1): ¿algún cliente legítimo depende de eso hoy (compositor
   QML, overlay)? Cerrarlo cambia un contrato externo.
3. `HERMES_EGRESS_MODE` a `default-deny` rompe el descubrimiento en primera instalación. ¿Se acepta
   a cambio de un asistente de concesión en el primer arranque?
