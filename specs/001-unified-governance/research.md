# Unificación del subsistema de gobernanza — research.md

Estado: BORRADOR para revisión del dueño (sin código hasta aprobación).
Contexto: el dueño diagnosticó "muchos sistemas paralelos; deberíamos tener UN HITL y UNA jaula kernel para todo". Esta spec colapsa la fragmentación en un bounded context `hermes.governance` con 3 primitivos detrás de un único chokepoint, + 2 ejes adyacentes (scan, estado).

## Fragmentación confirmada en código (file:line)

### HITL — 4 mecanismos paralelos
- H1 native-danger: `security_hook.py:1389-1507` (`_resolve_native_danger_approval`), `:1463` `event.wait`, `_pending_events`. NO durable (vive en el hilo; muere si el turno acaba).
- H2 cola autónoma: `dbus_runtime_service.py:2946-2968` (`re_enqueue_after_approval`) + `work_item.py:106-165` (state machine `PENDING_APPROVAL→PENDING`). DURABLE — patrón canónico a generalizar.
- H3 gateway nativo: `approval_gateway.py` (once/session/always/deny, sesión YOLO). NO durable.
- H4 install scan modal: `dbus_runtime_service.py:2122` `scan_install_draft` → `InstallScanModal`. Flujo+UX aparte.
- broker: `capability_broker.py:244` (`register_pending` + `ExecutionStatus.PENDING_APPROVAL`). Durable, otro camino.
- (+ teatro) confirmación conversacional del LLM ("responde sí") — sin garantía.

### Clasificación — 5 fuentes que no coinciden
- `nous_tool_risk_map.py:42-197` (READ/WRITE; `None`=default-deny). HUECO: delete_file/create_dir/list_dir NO existen → `None` → BLOCKED, pero el FS adapter SÍ los soporta.
- `tool_delicacy.py:47-182` (tiers + overlays MFA).
- `tool_policy.py:218-393` (preset + overrides + mfa_on_dangers + is_owner_disabled).
- `filesystem_surface_adapter.py:53-55` (`_SUPPORTED_OPS` propio, desalineado).
- `security_hook.py:238-967` (hardline/self-jailbreak/command/code guards/denylist) — el "single-source dangerous" de terminal, pero NO único.

### Jaula — 3-4 arranques distintos (de ahí que fallen distinto)
- terminal/exec: `hermes-exec-launcher` — netns + systemd properties. SIN landlock_loader, SIN seccomp propio.
- navegador: `hermes-browser-jail` — netns + Landlock BROWSER + seccomp `chromium-browser.json`. (El bug del seccomp EACCES vivió aquí porque el allowlist BROWSER es una lista a mano separada — `landlock_ruleset_builder.py:220-365`.)
- filesystem: openat2 RESOLVE_BENEATH in-daemon (`filesystem_surface_adapter.py:149-166`).
- MCP: `hermes-mcp-launcher` (4ª jaula).

### Scan — 2 composiciones
- `security_center/application/composition.py:52-65` (7 scanners, heurístico, sin trivy).
- `dbus_runtime_service.py:1656-1749` `_scan_service_lazy` (mismos 7 + trivy condicional). El mismo artefacto puntúa distinto según el path = "teatro en un path, real en otro".

## Diseño objetivo — bounded context `hermes.governance`

### Eje 1 — PolicyEngine (motor de política único)
Una consulta server-side determinista. `PolicyRequest(tool,args,surface,origin,preset,context)` → `PolicyDecision(outcome∈{AUTO,REQUIRES_APPROVAL,DENIED}, approval_tier, cage_profile, reason_code, human_summary)`.
Pipeline fail-closed (primero que dispara gana): 1) floor inapelable (self-jailbreak/hardline — único DENIED no-elevable), 2) kill-switch/pausa, 3) riesgo READ/WRITE (risk_catalog ampliado), 4) delicadeza (tier), 5) preset/override del dueño, 6) cage profile, 7) AUTO vs REQUIRES_APPROVAL.
Colapso: risk_map+overlays+floor→dominio (DATO+lógica pura); tool_policy→puerto OwnerPolicyStore; FS `_SUPPORTED_OPS` deja de clasificar (ejecutor puro). Cierra el hueco delete_file (=WRITE, +destructivo).

### Eje 2 — ApprovalCoordinator (HITL durable único) — suspend, no bloquear
Generaliza el patrón H2 (cola, durable) a TODAS las superficies. En REQUIRES_APPROVAL: registra `PendingApproval` durable (idempotente por action_digest, single-use token) y **suspende** la unidad de trabajo (turno chat o tarea) — el hilo se libera; se emite la tarjeta como evento durable (SSE, como el resume incremental del chat, commit bcbd118). Al aprobar **días después**: re-dispatch en un turno NUEVO (re_enqueue/resume_conversation), consume el token, ejecuta la acción exacta — sin re-prompt, sin event.wait. Tras reinicio: reconcile al boot. Caducidad: NO por defecto (persiste hasta que el dueño actúa). Borra H1/`_pending_events`/`event.wait`/`_NATIVE_DANGER_OWNER_WAIT_S`. Una sola tarjeta `ApprovalCard.tsx`.

### Eje 3 — CageLauncher (jaula kernel única)
Un `CageProfile` declarativo (landlock+seccomp+netns+openat2+uid) por superficie, generado de UN allowlist base+delta. El BASE común incluye `/usr/share/hermes/seccomp` → el bug del navegador es estructuralmente imposible. UN launcher parametrizado aplica el pipeline fijo (netns→landlock→seccomp→drop uid→execv con herencia Landlock). exec-launcher/browser-jail/mcp-launcher → invocaciones del mismo launcher con profile distinto. Terminal GANA seccomp (sub-flag, validar). Ninguna superficie puede fallar distinto.

### Eje 4 — Scan único
Un solo composition root `build_scan_service(*, allow_trivy)`; trivy pasa a parámetro (no 2 funciones). El offload a hilo vía puerto. Mismo artefacto, mismo score en todo path.

### Chokepoint único
`nous_engine._invoke_tool` → `GovernanceGate.evaluate→suspend_or_execute`. El `pre_tool_call` hook deja de ser gate paralelo → thin adapter sobre el mismo PolicyEngine. Flag `pre_tool_block_checked` evita doble-gate.

### Módulos (DDD)
`hermes/governance/{domain/{policy,approval,cage},application/{governance_gate,approval_coordinator,ports},infrastructure/...}`. Domain puro (sin framework). REUSA: tools.approval (floor nativo), sqlite_approval_gate, sqlite_work_queue+work_item, audit_hash_chain, ScanService, landlock_loader/ruleset_builder.

## Migración incremental (no big-bang) — flag `HERMES_GOVERNANCE_UNIFIED` (default OFF)
- F0 Andamiaje + shadow-mode (viejo vs nuevo, CI falla si divergen).
- F1 Clasificación (mover al dominio, ampliar delete_file/create_dir/list_dir).
- F2 Chokepoint (_invoke_tool→GovernanceGate; hook→thin adapter).
- F3 HITL durable (borrar event.wait; suspend/resume; aprobar tras reinicio).
- F4 Jaula única (CageLauncher; allowlist base+delta; seccomp terminal sub-flag).
- F5 Scan único (trivy parámetro).
- F6 Limpieza (borrar dead code de los 4 HITL; quitar flag).
Dependencias: F0→F1→F2→{F3,F4}; F5 independiente.

## Invariantes preservadas
No debilitar la jaula (superconjunto de confinamiento; terminal gana seccomp, nadie pierde). "Nada prohibido, todo elevable" (único DENIED no-elevable = floor anti-autopirateo; deshabilitado-por-dueño = DENIED con vía de reactivación). Fail-closed en error/no-clasificado. Sin teatro (solo HITL durable con MFA server-side autoriza). Audit WORM intacto. PII redaction.

## Eje 5 (pendiente de diseño) — Capa de estado única (pedido del dueño)
Fuera del alcance del arquitecto de gobernanza; subsistema distinto. Síntomas: mensajes de chat duplicados (render optimista vs SSE vs espejo; refresh corrige) y skills auto-creadas que no se listan (disco vs BD). Objetivo: una fuente de verdad de estado (espejo autoritativo, sin dobles, sin disco-vs-BD). Requiere su propio diseño (pendiente).

## Decisiones del dueño (cambian contrato/postura)
1. delete_file/destructivos → tier MFA (no aprobación simple). [cambia fricción]
2. Seccomp en el terminal del agente (hoy solo netns). [endurece confinamiento; riesgo de ajustar perfil]
3. HITL sin caducidad por defecto (persiste hasta aprobar). [dueño YA lo pidió: "no deben caducar"]
4. "always/session" del gateway viejo → overrides durables del dueño (visibles/revocables en UI).

## Procedencia
Diseño producido por software-architect sobre el código real (24 tool-uses). Mapea a research.md (este) + plan.md (módulos/migración) + data-model.md (PolicyDecision/CageProfile/PendingApproval) de `specs/001-unified-governance/`.
