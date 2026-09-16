# TASK-CE — admisión durable y recorrido nativo

Fecha: 2026-09-12. Lote Runtime acotado; sin otro ejecutor, sin cambiar el sobre firmado de doce campos y sin equiparar entrega/ACK con ejecución. Community sigue siendo aplicación nativa; las pruebas DOM no se presentan como QA del binario Tauri.

## Defectos cerrados

- Una aprobación podía empezar a encolar mientras Rechazar todavía ganaba la fila. Una reclamación SQLite única, durable y anterior a efectos serializa aprobar/aprobar y aprobar/rechazar, incluso entre procesos.
- Dos aprobaciones concurrentes del mismo remitente podían tomar la autorización del otro humano. TriggerGate exige el `trigger_instance_id` recién emitido para esa decisión; se revoca incluso al fallar el encolado.
- Una excepción después de insertar podía permitir repetir el encargo. La reclamación queda **sin confirmar**, visible, no rechazable ni reenviable. Tampoco se adopta una ejecución legacy deduplicada como recibo de una decisión nueva.
- El daemon no comprobaba forma/destino/frescura completos y los pendientes perdían la firma/vínculo originales. Guarda prueba privada firmada + instancia/tenant/paired_at/endpoint/clave pública; revalida firma, destino, frescura y correspondencia íntegra con la fila al aprobar. Una redelivery no completa pruebas de filas legacy. Sin prueba suficiente, no se aprueba ni se reatribuye; se conserva evidencia y se permite rechazo explícito.
- La comprobación final usa el mismo `configuration_lock` que revoke/re-pair y rodea exclusivamente validación + inserción SQLite síncrona. No hay await, red, inferencia ni auditoría dentro del lock. El resto de la cola conserva el mismo código de inserción. Revocar antes de esa frontera impide insertar; revocar después no equivale a cancelar trabajo previamente admitido.
- GET del buzón devuelve 503 al faltar/fallar el servicio; no lista vacía de éxito.
- La devolución de resultados exige destinatario local persistido, vínculo vigente antes de cada envío y recibo estructurado con correlación coincidente. Respuestas vacías/204/correlación distinta no marcan entregado. Las filas legacy sin destinatario probado no se exportan.

## UI focal (Emil)

| Before | After | Why |
| --- | --- | --- |
| Incertidumbre de admisión podía parecer una tarjeta disponible para repetir. | Estado «Admisión sin confirmar», sin aprobar/rechazar y con instrucción de actualizar/revisar. | Feedback honesto sin duplicar trabajo que puede haber empezado. |
| Un pendiente de un pairing anterior parecía aprobable. | Aviso de autorización no verificable; Aprobar deshabilitado, Rechazar explícito disponible. | Conservar la decisión humana y explicar el bloqueo sin simular disponibilidad. |
| Fallo del daemon se confundía con buzón vacío. | Error recuperable existente de Tareas mediante HTTP 503 real. | Vacío y desconocido son estados distintos. |

No nuevos gestos, animaciones o pantallas; se reutilizan componentes/foco/estados actuales. Los metadatos públicos no incluyen firma, snapshot del vínculo ni credenciales.

## Evidencia final

- **287 PASS**, 11.06 s: seguridad/provenance + nueva autoridad firmada + repositorio/reclamaciones multiproceso + config-sync inbox/status + dashboard + HTTP inbox + D-Bus + TriggerGate + taint/HITL/sequential/native-write. Un warning preexistente de marker `security` desconocido.
- **37 PASS**, 0.66 s: cola SQLite completa (`tests/tasks/test_work_queue.py -o addopts= -q`; no excluir sus 17 tests de integración).
- **27 PASS**, tres archivos frontend: `InboundDelegationCard.test.tsx`, `TasksView.test.tsx`, `api/inbound-delegations.test.ts`. `NODE_OPTIONS=--no-experimental-webstorage npm run build` pasa typecheck y Vite.
- Ruff de los tres archivos nuevos de autoridad/seguridad/arnés nativo: **PASS**. No se afirma lint global; otros archivos existentes tienen avisos previos.
- **Hermes real 0.21.1 en imagen local `365e584d7f5c`: PASS**. LLM ficticio loopback, `--network none`, datos/llaves efímeros. Enterprise real firma y entrega; Runtime verifica y persiste; antes de aprobación no hay tarea ni llamada LLM; aprobación → queued → running → `NousReasoningEngine`/`GovernedAIAgent` y `AgentLoopOrchestrator` reales → auditoría firmada local → completed → resultado separado → no replay.

Comando del arnés reproducible (montar fuentes actuales read-only, cambiar sólo las rutas de checkout):

```sh
podman run --rm --network none --entrypoint /usr/bin/python3 \
  -v /ruta/runtime:/review:ro -v /ruta/enterprise/src:/enterprise-src:ro \
  -e PYTHONPATH=/review/src -e HERMES_HOME=/tmp/task-ce-profile \
  365e584d7f5c /review/tests/integration/delegation_native_roundtrip.py
```

Ejecutado en scratch DGX `/tmp/safent-task-ce.vILFiC`, no en bases canónicas. El arnés crea y destruye todos sus recursos temporales; no inicia worker externo, no usa proveedores ni cuentas reales. Su adaptador HTTP de autenticación y el transporte hacia admisión son fixtures explícitos: **no** afirma ejecutar FastAPI/RBAC ni D-Bus reales en ese smoke. Las unidades HTTP/D-Bus sí cubren los handlers productivos por separado.

## Archivos del lote

- `src/hermes/tasks/triggers/application/{delegation_approval_service.py,delegation_authority.py,trigger_gate.py}`.
- `src/hermes/tasks/triggers/domain/authorized_trigger_ports.py` y `infrastructure/sqlite_authorized_trigger_repository.py`.
- `src/hermes/tasks/infrastructure/{sqlite_pending_delegations.py,sqlite_task_dashboard.py,sqlite_work_queue.py}`; `src/hermes/tasks/testing/in_memory_work_queue.py` (adaptador de pruebas).
- `src/hermes/config_sync/{delegation_inbox.py,delegation_status.py}`.
- `src/hermes/agents_os/infrastructure/dbus_runtime_service.py` (sólo delegación); `src/hermes/shell_server/cowork/inbound_delegations_api.py`.
- `frontend/src/api/types.ts`, `frontend/src/components/InboundDelegationCard{.tsx,.test.tsx}`, `frontend/src/views/TasksView.tsx`.
- `tests/security/{test_delegation_provenance.py,test_delegation_admission_authority.py}`, `tests/tasks/test_pending_delegations_repo.py`, `tests/integration/delegation_native_roundtrip.py`.
- `tests/unit/config_sync/{test_delegation_inbox.py,test_delegation_status.py}`, `tests/unit/agents_os/test_dbus_delegation_verbs.py`, `tests/unit/shell_server/test_inbound_delegations_api.py`, `tests/unit/test_task_dashboard_projection.py` y este informe.

## Límites de cierre/release

No se publica imagen ni se valida todo el binario Tauri. Falta recorrido desplegado entre instancias autorizadas con UI/D-Bus/red reales y efecto de herramienta aprobado dentro del mismo encargo; este arnés es narrativo, y los efectos/HITL se comprueban en tests separados. Una reclamación sin recibo confirmado exige revisión local de la evidencia; no se añade un botón peligroso de replay ni recuperación automática. La revocación posterior a una admisión ya confirmada no cancela retroactivamente la tarea: la cancelación y los permisos de cada efecto siguen por sus rutas existentes. No se afirma que este lote cierre todo Safent, la autonomía Ads ni el empaquetado final.
