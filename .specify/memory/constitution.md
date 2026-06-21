# hermes-runtime — Constitución del Proyecto

> Principios no-negociables que gobiernan TODO trabajo en este repo. Se chequean en cada `/team-plan` (gate) y se re-chequean al final del diseño. Cualquier violación debe justificarse explícitamente en `plan.md → Complexity Tracking`.

> **Heredamos los principios globales de team-software** (Security first · SOLID · DDD · SRP · Clean Code · Modularidad · Orquestación). Esta constitución los **especializa** y los **endurece** para este proyecto concreto. No los repite — los extiende.

## Principios del Proyecto

### 0. SUPREMO — Somos un Sistema Operativo, no un backend ni una app

Hermes **es un sistema operativo agéntico**. El agente (`hermes-runtime`, el daemon) es el núcleo; todo lo demás se engancha a él. Este principio **prevalece sobre todos los siguientes** y se chequea en CADA `plan.md` (gate pre-diseño y post-diseño). Reglas duras, concretas y verificables:

1. **El trigger del agente es SU PROPIA COLA en el daemon, jamás HTTP.** Todo origen (chat, timer, system-event, self-enqueue) solo deposita un `WorkItem`; quien lo ejecuta es el loop del daemon que drena la cola por iniciativa propia. Ningún endpoint HTTP dispara, ejecuta ni razona acciones del agente.
2. **Los endpoints HTTP del shell-server son SOLO supervisión** (lectura de estado del SO). Prohibido alojar en el shell-server: lógica de negocio, máquinas de estado, razonamiento LLM, firma criptográfica, o CRUD que mute la gobernanza/capacidades del agente. Un `POST` que sea passthrough fino a D-Bus es tolerable solo si hay un consumidor remoto real; si la UI es local, va por D-Bus.
3. **La GOBERNANZA del agente es estado NATIVO del daemon, gestionada por el control-plane D-Bus** (`org.hermes.Runtime1`), no por REST. Esto incluye: consents (otorgar/revocar autoridad), ciclo de vida de skills (firmar/promover/deprecar), config del cerebro (providers/modelo), teaching, gestión del browser del agente, y **los agentes mismos** (crear/editar/eliminar/activar). Si una operación cambia *qué puede hacer, qué sabe, con qué cerebro razona o cómo se le enseña* al agente → es gobernanza → va al daemon + D-Bus.
4. **El control-plane es FINO y por D-Bus** (la IPC del SO): encolar + supervisión read-only + gobernanza (pause/resume, HITL approve/reject). La autoría se deriva SIEMPRE del `sender_uid` del bus, nunca del payload.
5. **Las capacidades se apoyan en PRIMITIVAS DEL SO** (systemd/systemd-run, mutter ScreenCast + PipeWire, D-Bus, AT-SPI, Landlock/seccomp/netns/cgroups, nftables egress). Prohibido reimplementar en Python lo que el SO ya ofrece, o tratar un SDK externo como el núcleo de un efector.
6. **El confinamiento es del KERNEL, aplicado a nivel de unit** (`SystemCallFilter`, Landlock, seccomp, netns), no allowlists de string en la app. Prohibida la **seguridad-teatro**: código que aparenta confinar (rulesets, denylists) sin tocar el kernel. Los efectores agénticos (filesystem/terminal/red) se confinan por operación con `systemd-run --scope` o Landlock real, no con `str.startswith` ni basename denylists.
7. **Boot: el agente es el proceso primario**; la presentación (shell GTK, escritorio) se engancha a él. Objetivo firme: el `default.target` tira del target del agente, no al revés.
8. **Empaquetado: imagen de SO** (bootc Image Mode + units endurecidos + OTA cosign), no contenedor de app.

**Gate de revisión (obligatorio en cada `plan.md`):** ¿Esta feature añade un endpoint HTTP que sea mecanismo o gobernanza? ¿Hace razonar al sistema fuera de la cola del daemon? ¿Reimplementa una primitiva del SO o mete un SDK como núcleo? ¿Confía en allowlists de app en vez del kernel? **Si la respuesta a cualquiera es "sí" → es violación del Principio 0.** Debe moverse al daemon / D-Bus / primitiva del SO, o justificarse explícitamente en `plan.md → Complexity Tracking` con la alternativa SO-nativa rechazada por una razón concreta. "Más SO, menos backend" no es eslogan: es el criterio de aceptación.

> Estado conocido (auditoría 2026-06-03): el esqueleto cumple (trigger=cola, D-Bus fino, estado daemon-owned, bootc+kernel hardening) pero la gobernanza (consents/skills/providers/training/browser + wizard LLM) está hoy desviada a backend HTTP. Es **deuda constitucional** a migrar; ninguna feature nueva puede ampliarla.

### I. BrowserPort y contratos públicos son inmutables

El contrato `BrowserPort` (driver de navegador), el contrato `SelectorRegistry` (Protocol con HMAC), el contrato `StepRecorder` (artifact store + sink) y la firma pública de `BrowserSession` (`open`, `act`, `extract`, `observe`, `navigate`, `_execute`) son superficie estable. Cualquier cambio que rompa consumidores externos (`gestoria-agent`, `familywealth-agent`, `oposads-agent`) exige:

- Versión nueva semver-mayor del paquete `hermes-runtime`.
- Migration guide en el `CHANGELOG.md`.
- Aprobación explícita del usuario para esa breaking change.

Lo interno (estructura de módulos, nombres de clases privadas, librerías auxiliares como `playwright-extra`, `stagehand-py`) puede cambiar sin previo aviso, **siempre que no se filtre por la API pública**.

### II. HITL gate para EXTERNA_IRREVERSIBLE es inquebrantable

Ningún step `risk=HIGH` se ejecuta sin `hitl_approval_token` válido. La regla aplica en TODOS los modos del runtime:

- **Discovery** (LLM razonando en vivo).
- **Replay** (PlaywrightDriver puro ejecutando un `ReplayScript`).
- **Self-healing** (re-discovery por selector fallido).
- **Take-control** (operador opera el browser context; los steps HIGH siguen exigiendo token).

`BrowserSession._needs_hitl(step)` devuelve `True` para `StepRisk.HIGH` siempre y para `StepRisk.MEDIUM` si la configuración lo pide. Sin token → `HitlApprovalRequired` antes de tocar el driver. Saltar este gate, aunque sea "para testear", es violación constitucional.

### III. PII tokenization siempre antes de provider LLM

Cualquier dato PII del cliente (NIF, IBAN, nombre, email, importes específicos, IDs internos) se tokeniza con `DefaultPIITokenizer` o un `PIITokenizer` equivalente **antes** de incluirse en cualquier prompt enviado a LiteLLM. La des-tokenización ocurre **solo** dentro del browser context al rellenar el campo del sitio target. El mapping reverso vive en memoria de la sesión; jamás se persiste fuera, jamás aparece en logs.

Esto vale para:
- Stagehand-real en discovery.
- Self-healing (la re-discovery también pasa contexto al LLM).
- HITL profundo (el live-view stream al operador es PII y va por canal autorizado, no por el provider LLM).
- Auditoría (los `StepRecord` con DOM contienen PII; storage cifrado at-rest mandatorio).

### IV. Fail-closed por defecto en toda decisión de seguridad

Cuando una verificación de seguridad no puede dar respuesta definitiva, la respuesta es **negar**. Casos concretos:

- **HMAC selector no valida** → descarta el `Selector`, entra en discovery como si no existiera, evento `selector_tampered`.
- **Dominio fuera de `domains_whitelist`** → navegación bloqueada, evento `domain_violation`.
- **Confianza LLM < umbral** → no ejecutar, abrir `OperatorInterventionRequest`.
- **CAPTCHA / 2FA detectado** → HITL inmediato; nunca bypass automático.
- **`StorageState` corrupto o no descifrable** → estado limpio + pedir reauth; no asumir que es válido.
- **Provider LLM devuelve tool-call mal formado** → `StepOutcome.failed`, no reintentar más de N veces, degradar a HITL profundo.
- **Sin `HERMES_MODEL` configurado** → `StepOutcome.failed` con `llm_not_configured` explícito, no caída silenciosa.
- **Sandbox**: cap_drop ALL es el default; cada capability concreta se justifica.

"Por defecto inseguro" no es opción ni en dev.

### V. Tests base sin Chromium, sin red salida, deterministas

La suite que corre en CI **base** (sin marker) no requiere `playwright install chromium`, no requiere internet, no requiere API key real, no requiere DB. Cualquier test que viole alguna de esas condiciones lleva el marker correspondiente:

- `requires_chromium` — tests E2E reales contra Chromium.
- `integration` — tests contra Postgres efímero.
- `requires_llm` — tests que consumen LLM real (raros, opt-in).

Las dependencias externas en tests base se mockean con `FakeBrowserDriver`, `InMemorySelectorRegistry`, `InMemoryArtifactStore`, `InMemoryRecordSink`, y `monkeypatch` sobre `litellm.acompletion`. Si un test falla de forma flaky, se rompe el principio — no se acepta hasta diagnosticar la fuente de no-determinismo.

## Restricciones Técnicas Adicionales

- **Stack obligatorio**: Python 3.12+, `litellm>=1.50`, `pydantic>=2.5`, `structlog>=24.0`. Browser opcional: `playwright>=1.59`, `stagehand-py>=3.20`.
- **Lazy-import obligatorio para deps de `[browser]`**: `playwright`, `stagehand`, `playwright-extra` se importan dentro de las funciones que los usan. Importar `hermes` o `hermes.browser` sin las deps instaladas no debe fallar; solo fallan los puntos de uso reales con error claro.
- **Dependencias prohibidas**:
  - Skyvern y derivados (AGPL-3.0 incompatible con SaaS multi-tenant).
  - Cualquier dependencia que requiera cert client TLS PKCS#11 / softhsm2 / NSS DB — esta feature explícitamente sacó eso del scope.
  - Anti-detection que viole TOS conocidos del sitio target (rotación de huellas para evadir controles legítimos del sitio donde no estamos autorizados).
- **Compliance**: si una vertical opera datos personales bajo GDPR, el storage de `StorageState` y `StepRecord` debe estar en región EU y cifrado at-rest con key del KMS del consumer. El runtime no toma esa decisión, pero la exige por contrato (puerto de cifrado obligatorio).
- **Performance**: tests base completos < 5 minutos. Si la suite excede, se prioriza acelerar antes de añadir features.
- **Provider LLM-agnostic**: nada en el código del runtime puede asumir un provider concreto. Anthropic/OpenAI/Azure/Mistral/local pasan por LiteLLM.

## Flujo de Desarrollo y Gates de Calidad

- **Spec-Driven**: toda feature nueva no trivial entra por `/team-feature`. Sin `spec.md` aprobado → no hay `plan.md`. Sin `plan.md` con Constitution Check PASS → no hay `tasks.md`. Sin `tasks.md` → no se implementa.
- **Threat-model obligatorio** si la feature toca: persistencia de secretos, browser context, take-control, replay determinista, signed selectors. El `security-engineer` produce `threat-model.md` antes de implementación.
- **Code review pre-merge**: `code-reviewer` revisa contra spec + plan + constitución. Verdict explícito (APPROVE / APPROVE WITH NITS / BLOCK con razones).
- **CI gates**:
  - `ruff check` clean.
  - `pytest` base verde (sin markers de exclusión).
  - `bandit` clean (Low máximo con `# noqa` justificado).
  - `pip-audit` sin vulnerabilidades críticas/altas (skip-editable permitido).
  - Cobertura ≥80% (hoy `pyproject.toml` lo configura).
- **Tests de aceptación explícitos**: cada feature shipped debe tener al menos un test que ejerza el "Test independiente" de cada user story P1 ejecutada.
- **Deploy del runtime**: tags semver (`v0.X.Y`). Las verticales declaran versión exacta en sus `pyproject.toml`. Breaking changes empujan a `v1.0.0` cuando llegue.

## Governance

- Esta constitución **prevalece sobre cualquier otra práctica** del repo.
- Toda excepción se justifica en `plan.md → Complexity Tracking` con (a) por qué la necesita y (b) por qué la alternativa más simple no sirve.
- Las enmiendas requieren: PR dedicado, documentación del cambio, y plan de migración para código existente que dependa del principio modificado.
- `code-reviewer` y `security-engineer` verifican cumplimiento antes de cada merge.
- El `CLAUDE.md` del repo (si existe) y los slash commands de team-software heredan estos principios automáticamente.

**Version**: 1.1.0 | **Ratificada**: 2026-05-28 | **Última enmienda**: 2026-06-03 (añadido Principio 0 SUPREMO — Somos un SO, no un backend)
