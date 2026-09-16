# Oleada 1 — registro por carril (append-only)

Cada carril añade su propia sección al final. No editar secciones de otros carriles.

---

## L1b — `browser/` (refactoring-specialist)

Rama de trabajo: `w1-browser-delete` (worktree `lumen-runtime-w1b`), partida de
`feat/safent-next` @ `d169f9a`.

**Archivo, no cancelado.** Owner decision (2026-09-10): el navegador autónomo
spec-002 está PARCADO, no cancelado. Antes de borrar nada, se fija una rama que
conserva el código completo, alcanzable para siempre:

- Rama: `archive/browser-spec-002`
- SHA: `469d57447aeabaa188e5118ca36ddd1362fe49fa` (== `feat/safent-next` en el
  momento del corte, 2026-09-10 13:14 +0200)

### Borrado

Ámbito: **sólo** los 8 módulos de §2 fila 11 + 1 huérfano directo. Nada fuera de
`browser/` tocado salvo 3 referencias colgantes (documentadas abajo). No se tocó
`ops/agents-os-edition/` — verificado por grep: ningún fichero de `systemd/`,
`netns/` ni script de `ops/` menciona ninguno de los 9 módulos borrados. Los
ficheros con "browser" en `systemd/` y `netns/` (`browser-host.nft`,
`browser-ns.nft`, `hermes-browser-netns.service`, `hermes-browser-launcher.service`,
`agents-os-browser.slice`) son el **plano vivo** (herramienta `browser` de
Hermes vía CDP, netns propio) — invariante rojo de la spec 022, no spec-002.
Dejados intactos, tal y como pedía la instrucción.

**Src — 9 módulos borrados (3.163 L brutas, −3.144 netas tras editar 3 `__init__.py` + 1 docstring):**

| Fichero | L | Motivo |
|---|---:|---|
| `browser/application/session.py` | 570 | #1 de los 8 — `BrowserSession` orquestador |
| `browser/application/orchestrator.py` | 397 | #2 — `BrowserOrchestrator`, loop LLM discovery |
| `browser/application/self_healing.py` | 385 | #3 — `SelfHealer` |
| `browser/application/hitl_loop.py` | 336 | #4 — `HitlLoop`, escalado live-view |
| `browser/application/replay_runner.py` | 279 | #5 — replay determinista |
| `browser/application/discovery_runner.py` | 265 | #6 — `DiscoveryRunner` |
| `browser/infrastructure/openshell_sandbox_provider.py` | 487 | #7 — sandbox de egress OpenShell |
| `browser/infrastructure/ocr/azure_di_pipeline.py` | 212 | #8 — OCR cloud opt-in |
| `browser/application/two_fa_captcha_detector.py` | 196 | **no está en los 8** — único import es `self_healing.{InterventionReason, OperatorInterventionRequest}`, cero otros consumidores en `src` o `tests`; borrarlo era la única forma de no dejar un módulo que siempre lanza `ImportError` |

Editados (cadena de import eager, no borrado): `browser/__init__.py`,
`browser/application/__init__.py`, `browser/infrastructure/__init__.py` — dejaban
de existir los símbolos que reexportaban. `browser/domain/ports/ocr_pipeline.py`
— puntero de docstring al fichero borrado (el enum `OcrEngine.AZURE_DI_EU` se
deja intacto, es un valor de dominio, no una referencia a código).

**KEPT** (con consumidores reales fuera de `browser/`, verificado por grep):
adaptadores CDP (`cdp_input_adapter`, `cdp_screenshot_source`,
`cdp_screencast_source`, `browser_liveness`, `agent_browser_cli`),
`signed_selector_registry`, `storage_state_crypto`, `step_recorder`,
`log_filter`, el puerto `OcrEngine`/`TesseractOcrPipeline`.

**No tocado a propósito** (bucket "sólo lo sostienen sus propios tests",
explícitamente fuera de la oleada 1 per radiografia.md §7): `dom_sanitizer.py`,
`download_handler.py`, `upload_handler.py`, `expiration_detector.py`,
`multi_tab_manager.py` + `domain/browser_tab.py`, `browser_session_registry.py`,
`confidence.py`, `domain/ports/live_view_channel.py` + su fake en memoria,
`domain/ports/replay_store.py` + `replay_codec.py` + `domain/replay_script.py`,
`domain/ports/intervention_store.py` — ninguno importa los 9 borrados, todos
siguen pasando sus propios tests dedicados sin cambios.

**Tests — 13 ficheros borrados (2.861 L brutas), 2 editados quirúrgicamente:**

Borrados (probaban exclusivamente uno de los 9 módulos borrados, no podían
colectar sin él): `tests/unit/browser/application/{test_domain_whitelist,
test_hitl_loop, test_llm_budget, test_replay_hitl_gate,
test_replay_pre_execute_guards, test_replay_runner, test_self_healing,
test_us1_form_flow_fake, test_us1_invariants, test_2fa_captcha_detection}.py`,
`tests/unit/browser/test_browser_session.py`,
`tests/unit/browser/infrastructure/test_openshell_sandbox_provider.py`,
`tests/integration/browser/test_pii_tokenizer_flow.py`.

Editados:
- `tests/security/test_public_contracts_frozen.py` — fuera de `browser/` pero
  es un test de contrato congelado cruzado (`BrowserPort`, `SelectorRegistry`,
  `StepRecorder`, `BrowserSession`, `StorageStatePort`,
  `ReasoningEngine.run_cycle`). Se quitó sólo `TestBrowserSessionFrozen` + el
  import de `BrowserSession`; los otros cinco contratos, intactos.
- `tests/e2e/browser/test_smoke_full_stack.py` — se quitó una línea de import
  defensivo (`import hermes.browser.application.orchestrator`) dentro de un
  test marcado `requires_chromium` (deseleccionado por defecto); habría
  lanzado `ModuleNotFoundError` en cuanto alguien corriera esa marca.

**Delta total (`git diff --shortstat d169f9a..HEAD`):** 28 ficheros,
**+27 / −6.024** líneas → **neto −5.997** (src −3.144, tests −2.853, specs +20
de este mismo informe).

### Verificación

- **Grafo de imports (script AST sobre los 627 ficheros de `src/`):** cero
  importadores supervivientes de los 9 módulos borrados.
- **Grep de todo el repo** (`src/`, `tests/`, `ops/`, `apps/`, `*.toml`,
  `*.yml`, `*.service`, `*.nft`) por los 9 nombres de módulo: cero coincidencias
  fuera de este informe.
- **Los 22 entrypoints reales** (`ExecStart` `python3 -m …`/`-c …` de
  `ops/agents-os-edition/systemd/*.service`, los 4 `[project.scripts]`, los
  `Exec=` de `ops/agents-os-edition/desktop/*.desktop`, los wrappers horneados
  por `ops/container/Containerfile`) importan limpio con
  `PYTHONPATH=src python3 -c "import …"`.
- **Suite completa**, con la misma partición que el baseline del encargo
  (`tests/unit` aparte, resto aparte — así corrieron los números de baseline):
  - `tests/unit`: **4.426 passed, 9 skipped** (baseline 4.508/9; la diferencia,
    −82, son exactamente los tests borrados). Cero fallos nuevos.
  - resto: **1.244 passed, 2 failed (conocidos, `test_broker_gate_hardening_iter3.py`,
    ajenos a este carril), 6 skipped** (baseline 1.255/2/6; la diferencia,
    −11, son los tests borrados de `test_pii_tokenizer_flow.py` +
    `TestBrowserSessionFrozen`). Mismos 2 fallos, mismos nombres, cero nuevos.
- **Hallazgo colateral, NO de este carril:** invocando `pytest tests` como UN
  solo comando (en vez de partido como el baseline) aparecen 6 fallos nuevos
  en `tests/unit/mcp/test_mcp_sdk2_launcher_bridge.py`, causa raíz
  `tests/providers/test_openai_resolution.py:96` —
  `sys.path.insert(0, ".../oposads-agent/.venv/.../site-packages")` sin
  `monkeypatch` ni reversión, contamina el resto de la sesión de pytest y hace
  que la primera importación fresca de `mcp.types` en esa sesión resuelva un
  `attrs`/`attr` incompatible de ese venv ajeno. Reproducible en aislamiento →
  pasa limpio (9 passed). Es exactamente el bug ya fichado en
  `radiografia.md §5.a` ("inyecta un venv ajeno en sys.path", *en vuelo*), y es
  sensible al orden de colección — borrar ficheros de `tests/unit/browser/`
  desplaza qué test importa `mcp.types` por primera vez. No se tocó
  `tests/providers/test_openai_resolution.py` (fuera de `browser/`, ya
  asignado a otro carril). Reportado, no arreglado.
- **Ruff `src`:** 1.732 errores (baseline `radiografia.md` §5: 1.740) — baja,
  no sube.

## §L1c — `training/` (GEPA/teach-capture) sin alcanzar + `FakeWhisperBackend`

Rama `w1-training-delete` (worktree `lumen-runtime-w1c`), commit `a4adb5a`.

**Método.** Grafo de imports por AST (absolutos, relativos, perezosos) desde los 19 entrypoints
reales del repo (`[project.scripts]`, `python3 -m hermes.*` de `ExecStart`/scripts de `ops/`,
los tres wrappers `/usr/bin/hermes-{runtime,audit-tail,consent-manager}` del `Containerfile`).
Confirmado por grep cruzado en `src/`, `tests/`, `ops/`, `frontend/src/`: cero referencias fuera de
lo borrado, salvo las excepciones documentadas abajo.

**Borrado (1.706 L de fuente + 2.818 L netas con tests):**
`training/application/{llm_budget,narrative_aggregator,skill_compiler,training_orchestrator,
transcript_associator}.py`, `training/domain/{decision_rule,narrative_completeness,
training_session,voice_narrative}.py`, `training/infrastructure/{faster_whisper_transcription,
silero_vad}.py`, `training/testing/{in_memory_skill_package_store,in_memory_training_session}.py`,
y `shell_server/training/in_session_factory.py` (0 importadores en todo el árbol, ni siquiera de
test: con él muere la única `FakeWhisperBackend` cableada hacia una ruta que en teoría sería de
producción, per `inspeccion-estatica.md` hallazgo #11 — atenuado porque el módulo entero ya estaba
huérfano).

Tests borrados por ser exclusivos del código muerto: `tests/unit/training/` (8 ficheros),
`tests/contract/test_skill_package_port.py` y `test_training_session_port.py` (2 de los 4 puertos
spec-002 huérfanos; los otros 2 —`replay_preview_port`, `workspace_lifecycle_port`— son de
`autonomous/`/`workspace/`, fuera de este carril). Recortadas dos clases de test en ficheros
compartidos que sólo ejercitaban el código borrado: `TestTeachingPathSkillMdConvergence`
(`tests/unit/test_f3_unified_skill_store.py`) y `TestContentHashCoversExecutableContent`
(`tests/security/test_skill_signature_hardening.py`) — el resto de ambos ficheros prueba código
vivo (`SkillStoreAdapter`, `SkillGovernanceService`, `verify_skill_signature`) y queda intacto.

**Excepción documentada — NO borrado pese a ser inalcanzable desde entrypoints reales:**
`training/domain/ports/transcription_port.py` y `training/testing/fake_transcription.py`.
`workspace/application/audio_pipeline.py` (carril L1a, aún vivo en este worktree) los importa en
caliente; borrarlos habría roto `tests/unit/workspace/test_audio_pipeline.py`, fuera del alcance de
este carril. Queda para que L1a los retire junto con su único consumidor al borrar `workspace/`
entero.

**Se mantiene (núcleo alcanzable de `training/`, per radiografia.md §2 fila 9 KEEP):**
`application/{skill_evolution,skill_signer}.py`, `domain/{skill_md_document,skill_package,
skill_state}.py`, `evolution/` (entrypoint `hermes-evolution` → GEPA offline CLI),
`infrastructure/gepa_evolution_engine.py`.

**Verificación.** Los 15 entrypoints Python reales importan tras el borrado (incluido
`workspace.application.audio_pipeline`). Suite completa verde salvo 6 fallos preexistentes de
orden en `tests/unit/mcp/test_mcp_sdk2_launcher_bridge.py` (pasan en aislado; no tocados por este
carril; no son los 2 fallos históricos de `test_openai_resolution.py` citados en la baseline, que
ya estaban resueltos en este commit). `ruff check src`: 1.740 → 1.719 (baja).

**Fuera de alcance, escalado:** una instrucción recibida a media tarea pedía retirar toda la
funcionalidad viva de "enseñar skills por navegador" (`shell_server/main.py` wiring,
`agents_os/application/teaching/`, `shell_server/cowork/{training_live,teach_vnc}.py`, frontend
`TeachModal.tsx`/`TeachPanel.tsx`, verbos D-Bus). No se ejecutó en este carril: es cambio de
comportamiento sobre código vivo (no un refactor), toca `shell_server/main.py` — explícitamente de
otro carril — y abarca backend/frontend/D-Bus muy por encima de una tarea de
`refactoring-specialist`. Requiere spec propia (`requirements-analyst` → `tech-lead` →
`software-architect`) antes de tocar código.
