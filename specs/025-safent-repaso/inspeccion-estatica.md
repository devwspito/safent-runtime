# Inspección estática — safent-runtime

Rama `feat/safent-next`, HEAD `34c463e`, 2026-09-10. Solo lectura, sin contenedores, sin red salvo `gh`.
Intérprete del sistema: Python 3.12.3, ruff 0.15.13, mypy 2.1.0. `hermes_agent` NO instalado en el host.

## 1. Verdad de las herramientas

**Tests.** `tests/unit`: **4508 pasan, 9 saltan, 38 deseleccionados** (63 s) — línea base +1. Resto de `tests/`
(agents_os, capabilities, contract, e2e, integration, providers, security, security_center, tasks, tui, vm):
**2 fallan, 1255 pasan, 6 saltan, 207 deseleccionados**. Los 9 skips de unit son drift de entorno (composio sin
`ComposioError`, contratos spec-003 ausentes); los 4 de `tests/contract/` son puertos spec-002 nunca migrados
(`test_replay_preview_port.py:27`, `test_skill_package_port.py:27`, `test_training_session_port.py:27`,
`test_workspace_lifecycle_port.py:27`): contratos afirmados sin implementación. Cero `skip` incondicional, cero `xfail`.

**Los 2 fallos son contaminación de orden — y ocultan una aserción de seguridad.**
`tests/providers/test_openai_resolution.py:24` inserta *globalmente y sin limpiar* una ruta absoluta de la máquina
del dueño en `sys.path`: `"/home/luiscorrea-dev/Desktop/oposads-agent/.venv/lib/python3.12/site-packages"`. Ese venv
ajeno trae un paquete top-level `tools/`. A partir de ahí `import tools.memory_tool` deja de lanzar `ImportError`,
el skip defensivo de `tests/security/test_broker_gate_hardening_iter3.py:502` y `:612` no salta, y las dos pruebas
que verifican que **memory-write y clarify pasan por el broker** fallan con «broker.dispatch called 0 times»
(`:490`, `:594`). Aislado el fichero, todo verde. Verificado por bisección: solo `tests/providers` lo provoca.

**Ruff:** **3684 errores, 1383 autofixables** (+178 unsafe). Top 15: E501 501 · I001 494 · F401 485 · ARG002 469 ·
ARG001 293 · UP037 245 · PLR2004 143 · PLC0415 135 · SIM105 107 · ARG005 93 · SIM117 75 · S110 72 · E402 64 ·
F841 52 · PLR0911 36. Los 25 F821 triados uno a uno: **todos son anotaciones-string de imports perezosos**
(`"aiohttp.ClientSession"`, `"UUID"`, `"np.ndarray"`, firmas D-Bus), ningún `NameError` real. Ruff no corre en CI.

**Mypy:** `strict = true` en `pyproject.toml:137`, **nunca ejecutado**. Corre: **1885 errores en 235 de 636 ficheros**
— type-arg 709, no-untyped-def 222, no-any-return 203, name-defined 202, no-untyped-call 125, import-not-found 95.
Los 184 `name-defined` de `agents_os/infrastructure/dbus_fast_runtime_adapter.py` son firmas D-Bus de dbus-fast
(`"s"`, `"ss"`, `"i"`): falsos positivos. Sin `overrides` por módulo un `name-defined` real sería indistinguible del ruido.

**Frontend** (`frontend/`, la UI oficial): `tsc --noEmit` **limpio**; `vitest run` **25 pruebas / 5 ficheros, verde**.
75 fuentes `.ts/.tsx`, 5 de prueba (~7 %). 203 `onClick`, **cero handlers vacíos**, cero `console.log`.

**Workflows.**
- `agents-os-edition.yml` (unit 7 de 13 dirs, integration, threat-model gate, build/push 4 imágenes, cosign, SBOM,
  smoke ISO) — **ROJO desde al menos 2026-07-10**: las 3 últimas fallan en `Install` a los 18-41 s. Causa exacta
  (run 34464991148): `Could not find a version that satisfies the requirement hermes-agent==0.21.1 (from versions:
  … 0.19.0)`. `pyproject.toml:19` lo declara dependencia dura pero **no existe en PyPI**:
  `ops/container/Containerfile:53-70` lo instala del tarball de GitHub (`HERMES_AGENT_COMMIT=2237be35…`). **Dos meses
  sin puerta de calidad.**
- `publish-image.yml` — verde histórico; la de hoy `in_progress`. Es el único que funciona.
- `secret-scan.yml` — **ROJO desde 2026-09-09**. Descargué el SARIF: **3 hallazgos**, todos en
  `tests/unit/ops/test_companion_provision.py:53,54,72` (commit `f1cdc9b`), valores `ZmFrZS1zaWduaW5nLWtleS1iNjQ=` /
  `ZmFrZS1wdWJsaWMta2V5LWI2NA==` = base64 de "fake-signing-key-b64". **No hay fuga real**, pero `.gitleaks.toml` no se
  actualizó y la puerta que protege el repo open-source está rota.

## 2. Hotspots (>800 líneas en `src/hermes/`)

130.922 líneas en 636 módulos; **20 ficheros superan 800**. `C901`: **71 funciones >10**; peores
`tui/bridge.py:506` `call` (20) y `workspace/infrastructure/ws_control_plane_channel.py:198` `_receive_loop` (11).
El problema no es la complejidad por función sino el tamaño de clase.

**`agents_os/infrastructure/dbus_runtime_service.py` — 8506 líneas.** `DbusRuntimeServiceWiring` va de la línea
**225 a la 5406** con **184 métodos**; después ~3.100 líneas de helpers de módulo. Extraer a
`agents_os/infrastructure/wiring/`, por responsabilidad: (a) `oauth_workers.py` — `_nous_oauth_poller:5916`,
`_xai_loopback_worker:5989`, `_codex_oauth_worker:6059` (flujos OAuth dentro de un servicio D-Bus);
(b) `mcp_registry.py` — `_neus_*` (`:6834`-`:7124`) y el seed de companion; (c) `package_argv_allowlist.py` —
`_npm_argv_matches_shape:6311`, `_pypi_argv_matches_shape:6325`, `_git_*` (`:6372`-`:6429`), `_prefetch_*`
(`:6551`, `:6712`); (d) `scheduled_tasks.py` — `_neus_cron_*` (`:7160`-`:7341`); (e) `hermes_env_io.py` —
`_write_hermes_env:6166`, `_write_hermes_model_config:6186`. Queda el despacho de verbos.

`runtime/nous_engine.py` (4350) → los 3 parches de tools (`_patch_skill_manage_tool:3841`, `_patch_memory_tool:3885`,
`_patch_clarify_tool:3946`) a `runtime/agent_tool_patches.py`: es la superficie que verifican las pruebas rotas de §1
y hoy está enterrada. `runtime/__main__.py` (2981) → builders `_build_*` a `runtime/composition.py`.
`runtime/security_hook.py` (2264) y `dbus_fast_runtime_adapter.py` (2337), segundo turno.

## 3. Código muerto y huérfano

Grafo de imports AST cruzado con menciones textuales en todo el repo: **23 módulos huérfanos, ~2.050 líneas**:
`workspace/infrastructure/selkies_gateway.py` (297) y `kasmvnc_gateway.py` (208) — **dos gateways de escritorio
remoto rivales, ambos muertos**; `browser/infrastructure/ocr/azure_di_pipeline.py` (212);
`workspace/infrastructure/alsa_audio_capture.py` (205); `platforms/domain/events.py` (240);
`training/application/training_orchestrator.py` (179); `shell_server/training/in_session_factory.py` (141);
`training/infrastructure/silero_vad.py` (127); el paquete `native_capabilities/` entero (`domain/ports.py` 105 +
4 `__init__`); `providers/infrastructure/factory.py` (54); `tui/screens/placeholder.py` (25).
`apps/agentic-panel/agentic_panel.py` y `apps/agentic_panel/agentic_panel.py` son **byte-idénticos** (186 líneas);
solo el segundo lo importa `tests/unit/apps/test_agentic_panel_model.py`.

**TODO/FIXME:** filtrando el «TODO» español (=«todo»), quedan **15 marcadores reales**, 0 FIXME, 1 XXX. Los que
señalan trabajo pendiente: `egress_proxy/application/ports.py:23` («cablear al hash-chain real»: la auditoría de
egress no está encadenada); `shell/infrastructure/shell_backend_client.py:526` (endpoint `/api/v1/setup/account` sin
implementar); `egress_proxy/infrastructure/blocklist_loader.py:15` (sin timer de refresco: la blocklist se congela);
`runtime/nous_engine.py:4339` (TODO DEVOPS del bake). Los 5 `TODO(H0-HARDWARE)` de `lumen/compositor/` son
verificaciones diferidas en RK3588, legítimas. No hay `NotYet` ni `ruflo` cableado en `src/`.
**Config:** **138 variables `HERMES_*`** en el código, 56 en README+`ops/`: ~82 sin documentar.

## 4. Fakes en rutas de producción

**40 clases** `Fake*`/`InMemory*`/`Noop*` en `src/hermes`, casi todas en subpaquetes `testing/` (aceptable).
Una se cablea de verdad: `shell_server/training/in_session_factory.py:118-119` sustituye el backend por
`FakeWhisperBackend()` si falta `faster-whisper`, con solo un `logger.info`: transcripción sintética presentada como
real. Atenuante: **ese módulo es huérfano** (§3).
`runtime/__main__.py:2002-2003` degrada a stub el snapshot de contexto («sin AT-SPI real»): el agente opera sobre
una foto vacía del escritorio. Hay **86 líneas «degraded»** en `src/`; ninguna llega al usuario.
`InMemoryAuditSink` (`egress_proxy/infrastructure/audit_sink.py:170`) e `InMemoryRuntimeService`
(`dbus_runtime_service.py:123`) **no** se cablean desde composición — correcto.

## 5. Manejo de errores

**1 `except:` desnudo** en todo `src/`. **72 `except…: pass`** (S110): `runtime/nous_engine.py` (6),
`shell_server/cowork/teach_vnc.py` (5), `tui/app.py` (4), `shell_server/training/api.py` (4),
`mcp/infrastructure/stdio_mcp_client.py` (3), `lumen/compositor/sys_manager.py` (3). **625 `# noqa: BLE001`**:
capturar `Exception` a ciegas es la norma de casa.

**`{ok: false}` con HTTP 2xx** (regla permanente del dueño) — 4 handlers, por AST sobre decoradores de ruta:
`shell_server/egress_api.py:291` (`POST /deny/add`), `:323` (`POST /domains/grant`), `:350`
(`POST /mcp/domains/grant`), `shell_server/cowork/providers_api.py:213` (`POST /{provider_id}/test`). Los tres
primeros son de **egress**: un cliente que no inspeccione `ok` cree que concedió o denegó un dominio y no ocurrió.
En el mismo fichero `:275` sí usa `HTTPException(422)`; la incoherencia es interna.
**Timeouts:** limpio; el único candidato, `runtime/model_health_monitor.py:291`, fija `ClientTimeout` en `:276`.

## 6. Seguridad estática

Limpio en lo grave: **cero `yaml.load` sin Loader, `verify=False`/`CERT_NONE`, `eval()`/`exec()` de cadena**
(los `app.exec()` son Qt) y **cero secretos en logs** (los 12 `logger.*token|secret` registran ausencia o un path,
nunca el valor); sin tokens en argv; un único path absoluto de máquina de desarrollo, y está en un test (§1).
`0.0.0.0` solo sale en comentarios y en listas de redes prohibidas (`http_control_plane_client.py:54`,
`config_sync/applier.py:209`). Dos cosas reales:

**Fail-open del confinamiento.** `security/landlock_loader.py:420-434`: si el kernel no trae Landlock (`abi is None`)
o la arquitectura no está soportada (`UnsupportedArchError`), `load_and_apply` **devuelve 0 — el mismo código que el
éxito**. El llamador `runtime/__main__.py:2963` ya sospecha: `"runtime_landlock.applied rc=%d NOT_ENFORCING — /boot
legible (¿degrade?)"`. El contrato confunde «confinado» con «omitido», y el objetivo RK3588 de los
`TODO(H0-HARDWARE)` es exactamente el caso de arquitectura no soportada.

**Shell arbitrario expuesto a QML.** `lumen/compositor/sys_manager.py:181` (`runCommandAsync`, `Popen(cmd,
shell=True)`), `:190` (`runCommandQuick`) y `:194` (`runCommand`) son `@Slot` de un `QObject` publicado en el contexto
QML entero. Los 9 llamadores usan comandos fijos o interpolan una resolución (`Desktop.qml:502-512`), así que hoy no
es explotable — pero es un `sh -c` sin gate del broker al alcance de cualquier superficie QML que pinte contenido del
agente. Los 7 `S108` son de `landlock_ruleset_builder.py` (allowlist, correcto) y dos prefijos `mkdtemp`.

## 7. Duplicación con hermes-agent 0.21.1

**Cliente MCP** — `src/hermes/mcp/` (1.975 líneas, `stdio_mcp_client.py` 660). `nous_engine.py:46` lo justifica:
«Nous no usa su MCP nativo (`mcp_tool.py`) — TODOS los MCP pasan por» el broker: duplicación deliberada, coste real.
**Providers** — `src/hermes/providers/` (832) envuelve el `PROVIDER_REGISTRY` de `hermes_cli`; el catálogo canónico
aporta valor (`tests/providers/test_catalog.py:88` documenta el bug `openai-api`→`openrouter`), el adaptador no.
**Tools** — solo 4 parches (`nous_engine.py:1854,3841,3885,3946`): mínimo, correcto.
Riesgo estructural: hermes-agent expone un paquete **top-level llamado `tools`**.
`ops/agents-os-edition/scripts/bake-validate-imports.py:57-59` documenta que el wheel de `cron_descriptor` lo pisaba
(`ModuleNotFoundError: tools.registry`). Es la misma colisión que rompe §1.

## 8. Huecos de capacidad (por código)

- **SSH / terminal remota: no existe.** Cero `paramiko`/`asyncssh`/`known_hosts`; los aciertos de «ssh» son Ed25519
  de firma (`config_sync/signature.py`). Sin almacén de claves de host ni grant de egress por host.
- **Windows: CLI a medias.** `safent` (bash, 783 líneas) despacha 15 verbos (`safent:767-783`); `safent.ps1` (361)
  cubre 10. Faltan **`companion` entero** (`status`/`update`/`rotate`/`remove`, `safent:701-710`), `agent`/`watch`,
  `uninstall` y `url`. El companion de ads es justo la novedad de la spec 024.
- **Backup / export: no existe** ningún `def backup|export_all|restore` sobre memoria, skills o auditoría.
- **Update/rollback: existe** — `bootc_updater_service/__main__.py`, decisiones en `OtaOrchestrator`.
- **Rotación de logs: no existe** `RotatingFileHandler`/`logrotate` en `src/` ni `ops/`; las bases SQLite y la cadena
  de auditoría crecen sin poda. **Health: existe** (`shell_server/main.py:931`, `remote_control/service.py:96`).

## 9. Calidad de las pruebas

**67 funciones `test_*` sin ninguna aserción**, casi todas smokes «does_not_raise» legítimos
(`tests/unit/test_lumen_new_slots.py:400,406,411,424`); pero `tests/unit/test_dbus_desktop_methods.py:323
test_captured_at_is_iso8601` promete validar un formato y no valida nada.
**Sleeps: 104.** `tests/tasks/test_chat_dbus_streaming.py` consume **54 de los 57 s** del directorio:
`test_emit_from_non_loop_thread_does_not_crash` 20 s, `test_text_order_preserved` 16 s,
`test_seq_monotonically_increasing` 6 s. `tests/security/test_confused_deputy_remediation.py:260,447` duermen 2 s.
**Bombas de tiempo: ninguna** (las fechas fijas son relojes congelados).
**Marcador `security` sin registrar:** `pytestmark = pytest.mark.security` en 8 ficheros de `tests/security/`
(`test_mcp_broker_route.py:28`, `test_skill_signature_hardening.py:37`, …) no está en `pyproject.toml:157-166` pese a
`--strict-markers`: no se puede seleccionar la suite con `-m security`.
**Cobertura en CI:** el workflow corre 7 dirs de `tests/unit` + `tests/integration/agents_os`. `tests/security` (636
pruebas), `tests/tasks`, `tests/providers`, `tests/contract`, `tests/e2e`, `tests/security_center`, `tests/tui` y
`tests/vm` **no están en ninguna puerta**.

## Top 15 (impacto × certeza)

| # | Hallazgo · file:line | Importa porque · arreglo mínimo | E | Dueño |
|---|---|---|---|---|
| 1 | CI roja 2 meses: `hermes-agent==0.21.1` no está en PyPI — `pyproject.toml:19` / `ops/container/Containerfile:53-70` | Sin tests, gate ni firma desde el 10-jul: nada mergeado está verificado. Mover la dep a un extra e instalarla en CI del tarball fijado | M | devops |
| 2 | Test inyecta un venv ajeno en `sys.path` — `tests/providers/test_openai_resolution.py:24` | Tumba las 2 pruebas de que memory-write y clarify pasan por el broker. `monkeypatch.syspath_prepend`; fuera la ruta absoluta | S | qa |
| 3 | `secret-scan` roja por 3 falsos positivos base64 — `tests/unit/ops/test_companion_provision.py:53,54,72` | La puerta anti-fugas del repo público está rota: la próxima fuga real pasará por ruido. Añadir los 2 valores a `allowlist.regexes` | S | security |
| 4 | Landlock fail-open: «no soportado» devuelve el mismo `0` que «aplicado» — `security/landlock_loader.py:420-434` | El confinamiento puede estar apagado y el daemon arranca igual; `runtime/__main__.py:2963` ya pregunta «¿degrade?». Retorno distinto para «omitido» | M | security |
| 5 | 3 endpoints de egress devuelven `{ok:false}` con HTTP 200 — `shell_server/egress_api.py:291,323,350` | El cliente cree que concedió/denegó un dominio y no ocurrió. `HTTPException(422)`, como ya hace `:275` | S | backend |
| 6 | `DbusRuntimeServiceWiring`: 5.181 líneas, 184 métodos — `agents_os/infrastructure/dbus_runtime_service.py:225-5406` | Verbos D-Bus, OAuth, MCP, paquetes y cron mezclados: irrevisable. Extraer los 5 módulos de §2 | L | refactoring |
| 7 | mypy strict configurado y nunca ejecutado: 1885 errores — `pyproject.toml:137` | Un `name-defined` real es indistinguible de las 184 firmas D-Bus falsas. `overrides` para dbus-fast + job no bloqueante | M | devops |
| 8 | `tests/security` (636 pruebas) fuera de toda puerta — `.github/workflows/agents-os-edition.yml:66-71` | El producto se vende como gobernanza y su suite de seguridad no corre. Añadir `tests/security/ tests/tasks/ tests/providers/` al job | S | devops |
| 9 | Ruff nunca corre: 3684 errores, 1383 autofixables | 485 F401 y 494 I001 esconden imports rotos reales. `ruff check --fix` en un commit propio + job de lint | M | refactoring |
| 10 | 23 módulos huérfanos (~2.050 líneas) — `workspace/infrastructure/selkies_gateway.py`, `kasmvnc_gateway.py`, `native_capabilities/` | Se audita código que nadie ejecuta y que sugiere capacidades inexistentes. Borrarlos, más el duplicado `apps/agentic-panel/` | S | refactoring |
| 11 | Fallback silencioso a `FakeWhisperBackend` — `shell_server/training/in_session_factory.py:118-119` | Transcripción sintética como real; hoy inerte por ser huérfano. Lanzar en vez de degradar, o borrar con §3 | S | polish |
| 12 | `sh -c` arbitrario expuesto a todo el contexto QML — `lumen/compositor/sys_manager.py:181,190,194` | Ejecución sin gate del broker desde cualquier QML que pinte contenido del agente. Allowlist + argv en lista | M | security |
| 13 | 54 s de suite en sleeps de un fichero — `tests/tasks/test_chat_dbus_streaming.py` | Un minuto extra por corrida anima a saltarse las pruebas. Inyectar reloj en el coalescer | M | qa |
| 14 | Marcador `security` sin registrar pese a `--strict-markers` — `pyproject.toml:157-166` | No se puede seleccionar la suite por marcador. Añadir `"security: …"` a `markers` | S | qa |
| 15 | CLI Windows sin `companion`, `agent`, `uninstall`, `url` — `safent:701-710,767-783` vs `safent.ps1` | El companion de ads (spec 024) es inoperable en Windows. Portar los 4 verbos al `.ps1` | M | devops |

**Huecos sin ticket:** SSH/terminal remota, backup/export, rotación de logs, los 4 puertos spec-002 de
`tests/contract/`, hash-chain de la auditoría de egress, refresco de la blocklist, 82 `HERMES_*` sin documentar.
**No inspeccionado:** ejecución real (sin contenedores ni red al daemon), `hermes-agent` 0.21.1 (no instalable en el
host), `vulture` (sin instalación offline), 245 pruebas deseleccionadas por marcador.
