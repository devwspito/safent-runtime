# TASK-REMOVE — retirada de agentes empaquetados

Base: `19d86ee`. Fecha: 2026-09-11. No despliega ni modifica una base de usuario.

## Antes / después / motivo

| Antes | Después | Motivo |
| --- | --- | --- |
| 27 personas predefinidas, seed dependiente de edición y toggle reactivable. | Catálogo y seed eliminados en cualquier edición; API y verbos D-Bus del toggle retirados. | Decisión explícita: Hermes forma equipos dinámicos; Enterprise representa trabajadores reales. |
| Una delegación se atribuía a un especialista por palabras del prompt. | Actividad atribuida a la identidad observada; `delegate_task` nativo sin modificar. | No fabricar actividad ni un organigrama ficticio. |
| Un identificador antiguo podía terminar usando la persona por defecto. | Identidades retiradas fallan explícitamente antes del ciclo del LLM; no fallback silencioso. | No ejecutar con una identidad distinta de la solicitada. |
| Quedaban traducciones Office/Swarm y cliente SSE sin consumidores. | Retirados esos residuos y sus pruebas exclusivas; acción fullscreen compartida conservada. | Quitar código muerto, no funciones activas. |

Se conserva solamente la lista exacta de 27 IDs históricos para bloquear su
resurrección. No se bloquea cualquier prefijo `roster-`: los perfiles personalizados
y los perfiles empresariales ya gestionados por cloud se conservan. Un perfil
nuevo no puede reutilizar un ID reservado. Las filas históricas y la antigua tabla
de settings, si existen, no se borran ni reescriben. La apertura del registro asegura
el agente nativo por defecto incluso en una base antigua que sólo contenga paquetes.

La regla que exigía negar cualquier limitación del entorno se reemplaza por
comprobar herramientas, solicitar permisos por el sistema y explicar acceso
faltante con precisión. La delegación no amplía permisos ni sustituye aprobaciones.
Se mantienen custom CRUD, conversaciones, calendario, perfiles cloud y Hermes.

## Verificación

- Scratch aislado DGX `/tmp/safent-dashboard.ZQla4M`, Python 3.12 del host.
- `PYTHONPATH=src python3 -m pytest tests/unit tests/tasks -q -rs --tb=short`:
  **5712 PASS, 19 SKIP, 64 deselected, 6 warnings**, 240.18 s;
  `factory-retirement-full.log`. Los skips de SDK/entorno, gitleaks y gate de
  clave de release siguen abiertos; esta suite no certifica la imagen final.
- Selección inicial de registro, permisos, config-sync, proveedores y motor:
  **270 PASS**. Selección final `test_factory_retirement.py`, incluida regresión
  del emisor de actividad añadida tras la suite completa: **11 PASS**.
- Frontend: **215 PASS**, TypeScript y build PASS. Se eliminan seis pruebas del
  stream Office retirado, no regresiones activas. Node del Mac requiere
  `NODE_OPTIONS=--no-experimental-webstorage npm test` para usar localStorage de
  jsdom; sin esa opción hay 44 fallos de entorno previos a las aserciones.
- Ruff de los módulos nuevos y `git diff --check`: PASS.

No hay borrado de historial que recuperar. No se certifica todavía la UI nativa
completa, instalación multiplataforma, lifecycle LLM gestionado ni despliegue real.
