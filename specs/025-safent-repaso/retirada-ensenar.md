# Retirada — "enseñar skills mediante navegador"

## Qué era

Bajo `Habilidades → Enseñar habilidad`, el operador nombraba una skill, pulsaba
"Empezar a enseñar" y demostraba la tarea en el navegador enjaulado real (noVNC,
`teach_vnc.py` + `training_live.py`). Un observador CDP capturaba los pasos, un
`TrainingSessionOrchestrator` llevaba el estado RECORDING→REVIEWING→SIGNED, y al
guardar un `SkillCompiler` compilaba la demostración en un `SkillPackage`
(`SKILL.md` firmado, `teaching_origin=teaching_live`). La capa de aislamiento
(`agents_os/application/teaching/`: `TeachingSessionOrchestrator`,
`InputOwnershipLedger`, `AgentBrowserTeachingContext`) arbitraba quién poseía el
input del navegador (operador vs. agente) durante la sesión.

## Por qué se retira

Decisión del dueño, 10-sep-2026: funcionalidad sin probar y obsoleta. Ya se había
identificado como fuera de alcance de oleada 1 (`oleada-1.md §L1c`, "escalado, no
ejecutado"). Se ejecuta ahora como tarea propia.

## Frontera — qué se queda vs. qué se borra

**Se borra (backend):** `shell_server/training/` completo, `shell_server/cowork/
{training_live,teach_vnc}.py`, `agents_os/application/teaching/` completo,
`agents_os/infrastructure/agent_browser_teaching_context.py`,
`agents_os/testing/fake_teaching_context.py`, `agents_os/application/
training_session_orchestrator.py` (quedaba huérfano tras lo anterior — cero
importadores), el `.compile()` de `agents_os/application/skill_compiler.py`
(su `SkillPackage`/`.verify()` los sigue usando el replay de skills, ajeno a
enseñar), `workspace/application/audio_pipeline.py` (100% acoplado a
`training_session_id`/`StepRecord`, sin uso fuera de enseñar) y sus puertos
`training/domain/ports/transcription_port.py` + `training/testing/
fake_transcription.py`. `shell/infrastructure/shell_backend_client.py.
start_teaching()` (cero llamadores). El wiring de `shell_server/main.py`
(routers de `/api/v1/training`, `/teach/*`). Frontend: `TeachModal.tsx`,
`TeachPanel.tsx`, el botón "Enseñar habilidad" y la sección "Enseñadas en vivo"
de `SkillsView.tsx`, `startTeaching`/`signTeaching` de `api/client.ts`,
`teaching_origin` de `api/types.ts`, claves `teach.*`/`skills.teach.*`/
`skills.section.{live,rest}` de `i18n.ts`.

**Se queda:** la vista "En vivo" de solo lectura (`VncView.tsx`, `EnVivoView.tsx`,
`cowork/vnc_proxy.py`, `cowork/watch_live.py`, `runtime/jailed_browser_manager.py`,
`cowork/clipboard_bridge.py`) — se les extrajo `_verify_token` (vivía en
`training_live.py`) a `vnc_proxy.py`. `session_agent/input_bridge.py` sigue
arbitrando input agente/operador con un `InputOwner`/`InputOwnershipLedger`
mínimo reubicado en `agents_os/domain/input_ownership.py` (tipo puro, sin
acoplar a enseñar). Los value objects de firma/gobernanza de skills
(`SkillPackage`, `SkillState`, `SkillMdDocument`, `SkillSigner`) se reubicaron
de `training/{domain,application}/` a `capabilities/{domain,infrastructure}/`
porque el escritor de gobernanza real (`skill_store_adapter.py`) no tiene nada
que ver con enseñar. `training/evolution/` (CLI `hermes-evolution`, GEPA
offline) se queda — es un pipeline genérico de autoevolución desde el log de
auditoría, ya marcado KEEP por `oleada-1.md`; retirarlo rompería un
entrypoint público no relacionado con "enseñar por navegador" (ver informe
final para la discrepancia frente a la instrucción original).

## Verificación

`PYTHONPATH=src python3 -m pytest tests/unit -q -p no:cacheprovider` verde;
`npm test && npx tsc --noEmit && npm run build` verde; test estructural que
falla si aparece una ruta `/api/v1/training*` o un módulo `*teach*`/`*training*`
bajo `src/hermes` fuera de la lista de excepciones explícita.
