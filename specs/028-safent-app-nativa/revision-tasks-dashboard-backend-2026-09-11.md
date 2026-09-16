# Tareas Community: proyección durable local

## Alcance

Implementado GET `/api/v1/tasks/dashboard?limit=100` mediante un nuevo verbo
D-Bus `GetTasksDashboard(uint32, operator_token)`. El shell conserva su sesión
HTTP autenticada; la lectura de resultados exige además autoría del operador
en el daemon, con token firmado y acotado a la operación para el canal proxy.
No se añade ejecutor, chat ni permiso de actuación.

El daemon lee una transacción SQLite read-only sobre su propio `shell-state.db`:

- `agent_tasks`: identidad, estado, instrucción resumida a120 caracteres y
  conversación persistidos; no payload, instrucciones completas ni credenciales.
- `pending_delegations`: único vínculo de procedencia Enterprise. Encargos aún
  sin ejecución usan `delegation:{message_id}`, pendientes de admisión o rechazados.
  Tras aprobar se muestra la tarea real una sola vez. Aprobado no es completado.
- `messages`: último mensaje assistant de esa tarea y sólo si está completada;
  texto acotado a8000 caracteres con aviso de truncado. No devuelve chat completo.
- `pending_approvals`: IDs de propuestas de la tarea; no parámetros ni secretos.

Almacenes opcionales ausentes omiten campos desconocidos. Un almacén presente
pero corrupto, DB inexistente, procedencia ambigua o daemon no disponible no
devuelven un falso listado vacío. La API responde503 si el daemon no puede
entregar el contrato. Límite1–200, limit+1 y orden estable fecha/ID.

## Pruebas

Suite completa `tests/unit`: **5489 PASS, 19 SKIP, 38 deselected**, 4 warnings,
202.77s. Después se añadió el test explícito de firma D-Bus `us`→`s`: foco final
**93 PASS** entre proyección, contrato D-Bus, dashboard previo y wiring de
producción. No hubo cambios productivos después de la suite completa.
Ruff sobre archivos nuevos y `git diff --check`: PASS.
Pruebas en scratch DGX `/tmp/safent-dashboard.ZQla4M`, nunca DB real.

```sh
PYTHONPATH=src:. python3 -m pytest tests/unit/test_task_dashboard_projection.py \
  tests/unit/agents_os/test_dbus_verb_export_completeness.py \
  tests/unit/test_tasks_dashboard.py \
  tests/unit/agents_os/test_list_recent_tasks_production_wiring.py \
  tests/security/test_dbus_runtime1_contract.py -q
```

## Revisión de rama previa

Se inspeccionó `lane/rt-tasks-remove` (`bbd914d`) recuperada de Claude. Contiene
otro read-model WIP: acceso directo desde shell y cambios adicionales de schema.
No se mezclan dos rutas iguales ni se importa sin pruebas. Este corte mantiene
el origen de datos en el daemon y usa los vínculos ya persistidos; no requiere
migrar identidad de tareas o recibos. La retirada de empaquetados de esa rama
sigue siendo un bloque separado.

## Pendiente que este corte NO cierra

- Telemetría durable de ejecución hacia Enterprise, secuencia/outbox y rechazos.
- QA en binario Tauri y bus real con sesión/credenciales de instalación.
- Paginación navegable de historial anterior al límite (se informa `has_more`).
- Retirada backend de agentes empaquetados y resto del plan completo.

La prueba de repositorio y transporte no equivale a validar el despliegue nativo.
