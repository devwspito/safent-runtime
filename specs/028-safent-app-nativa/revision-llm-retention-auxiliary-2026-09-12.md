# Herencia LLM: retención y limpieza de auxiliares — 2026-09-12

## Resultado y alcance

Delta acotado sobre el código actual de Runtime. **No habilita la herencia LLM**:
`managed_llm.resolve_managed_config` y el gate de la fábrica en `nous_engine`
siguen rechazando ejecución gestionada. No cambia Hermes upstream, proveedor,
OAuth, credenciales, política firmada, jaula ni contrato de Enterprise.

| Antes | Después | Motivo |
| --- | --- | --- |
| Cada arranque generaba un perfil sin política de retención. | Asignaciones nuevas `g{generation}-p{pid}-{random}`; limpieza conservadora en el siguiente arranque. | Identificar el proceso propietario sin almacenar secretos ni inventar otra autoridad. |
| `mkdtemp` usaba una ruta después de comprobarla. | Creación relativa a un descriptor de directorio privado abierto con `O_NOFOLLOW`, bajo `configuration_lock`. | Compartir exclusión con mantenimiento y reducir carreras de rutas. |
| Un handshake MCP fallido/cancelado dejaba el cliente sin registrar y sin cerrar. | Cierre antes de abandonar admisión, también al fallar decodificación de herramientas; cancelación original conservada. | Ningún cliente provisional debe perder su propietario. |
| Error MCP podía reproducir la excepción de transporte, incluidos valores sensibles. | Error estable y registros sin contenido de la excepción. | No copiar claves o respuestas remotas al diagnóstico visible. |
| Eager-start del navegador y reconexión MCP no pertenecían a `tasks`. | Entran una vez al `gather` existente y a la cancelación tras la gracia. | El apagado también espera esas tareas. |
| Una segunda solicitud de apagado podía cancelar otra vez un `finally` que libera recursos. | El helper no vuelve a cancelar tareas con `cancelling() > 0`. | Permitir que termine el cierre existente, sin nuevo ejecutor. |

## Retención: garantías y límites explícitos

- Edad mínima de siete días. Como máximo 128 entradas inspeccionadas y ocho
  perfiles eliminados por pasada; no existe worker de eliminación periódica.
- Sólo nombres nuevos reconocidos, directorios propios modo `0700`, PID
  confirmado muerto y contenido vacío o únicamente `config.yaml` regular propio
  modo `0600`, sin hardlinks. No hay borrado recursivo ni traversal de symlinks.
- Comprobaciones de identidad inode/dispositivo antes de borrar y operaciones
  relativas a descriptores abiertos. Creadores y limpiadores cooperantes usan
  el mismo bloqueo de configuración entre hilos/procesos.
- PID vivo/reutilizado, error de permisos al consultar liveness, perfil actual
  de `HERMES_HOME`, propietario/modo dudosos, enlaces, perfiles legacy y contenido
  adicional se conservan. El bootstrap sellado exige también el PID del nombre.
- Esto **no garantiza un límite total de disco**: perfiles con caches/archivos
  desconocidos o PID reutilizado se retienen deliberadamente. No se intenta
  migrar o adoptar un perfil antiguo. Tampoco es una defensa contra un atacante
  con el mismo UID y control de la base de autoridad; coincide con el alcance
  explícito de `configuration_lock`.
- Las pruebas sólo borran directorios efímeros propios. No se ejecutó la
  limpieza sobre perfiles de ninguna instalación del usuario.

## Contrato de inferencia comprobado, sin implementación duplicada

El estado actual ya cerraba la brecha de `extra_body`:

1. La fábrica de `nous_engine` excluye `request_overrides` para modelos
   gestionados y rechaza `ModelConfig.extra` gestionado no vacío. El modo libre
   conserva las opciones locales de Qwen.
2. `Enterprise/domain/inference_request.py` admite un sobre cerrado y rechaza
   tanto `extra_body` como campos aplanados (`chat_template_kwargs`, `base_url`,
   `api_key`, `provider`, `fallback_providers`).
3. `InferenceService._prepare` vuelve a validar modelo exacto y límite de tokens
   antes de reservar consumo; no acepta ambos campos de límite simultáneamente.

Se añade una regresión pura cruzada en Runtime, ejecutada contra el código
Enterprise actual. Conserva texto, herramientas de función y configuración SSE;
los argumentos JSON de herramientas permanecen datos inertes. No es una prueba
de transporte contra proveedor ni una nueva certificación de Hermes.

## Auxiliares: qué no cierra este lote

`JailedBrowserManager` no ofrece un método público de parada ni un identificador
de proceso propio: delega en el launcher/unidad externa. Cancelar su tarea de
arranque **no demuestra que la unidad externa haya terminado**. `CerebroBrowserManager.stop`
sólo puede terminar su ruta Popen directa; en la ruta por emisor únicamente
limpia estado local. No se añadió un `stop` ficticio, un kill por puerto ni un
cierre global de conexiones inferido de `snapshot()` (que sólo lista saludables).

MCP: el cliente real ya tiene cierre idempotente y dueño de sesión; el manager
ahora lo llama al abortar admisión, sin monkeypatch ni cambios de SDK. Las
conexiones registradas y sus unidades externas siguen requiriendo verificación
real del ciclo de servicio durante transición/revocación antes de abrir el gate.
El plazo duro y `KillMode=control-group` existentes no se modifican.

## Evidencia

- **117 PASS, 0 SKIP**, Linux DGX, snapshot aislado
  `/tmp/safent-llm-retention.uMCvX1/verified`, `PYTHONPATH=src` y ruta de módulo
  verificada antes de ejecutar. Incluye bootstrap Linux con descriptores
  sellados, retención, proceso competidor real, proceso propietario vivo,
  symlinks/cambios de directorio, fábrica gestionada/local, OAuth, lifecycle,
  manager MCP y apagado.
- **9 PASS**, contrato cruzado contra `Enterprise/src`, sin modificar ese repo.
- MCP: siete regresiones reproducidas rojas antes del fix → verdes; prueba con
  hijo Python y tuberías reales demuestra EOF, salida y una sola liberación al
  cancelar. La implementación de cliente en esa prueba es explícitamente un
  puerto de prueba, no una certificación del SDK MCP real.
- Helper de apagado probado con cancelaciones reales mientras cleanup está
  suspendido. Prueba de composición verifica una sola inclusión de cada tarea
  y que monitor/READY conservan su orden; no equivale a un boot completo de VM.
- Ruff PASS en los archivos del lote excepto `__main__.py`, cuyo baseline ya
  contiene **62** avisos: mismo total y distribución por código antes/después.
  `git diff --check` PASS.
- Incidencias del arnés no se contabilizan como verde: una copia inicial dejó
  un paquete viejo en la raíz que sombreaba `src`; se descartó y rehízo el
  snapshot limpio. Una pasada siguiente carecía del fixture systemd `ops/`;
  se copió el archivo y se repitió el conjunto final de 117 pruebas.
  La suite adicional `test_mcp_sdk2_launcher_bridge` iniciada en la copia
  inválida fue interrumpida; **no se declara certificada en este lote**.

Comando Linux final (desde la raíz del snapshot con `src`, `tests` y el archivo
`ops/agents-os-edition/systemd/hermes-runtime.service`):

```sh
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_managed_profile_retention.py \
  tests/unit/test_managed_llm_profile.py \
  tests/unit/test_managed_llm_bootstrap.py \
  tests/unit/test_managed_factory_payload.py \
  tests/unit/test_managed_llm_lifecycle.py \
  tests/unit/mcp/test_mcp_connect_cleanup.py \
  tests/unit/mcp/test_mcp_server_manager.py \
  tests/unit/runtime/test_startup_task_shutdown.py \
  tests/unit/runtime/test_sigterm_graceful_shutdown.py \
  tests/unit/test_runtime_shutdown_deadline.py -q
```

Contrato cruzado: añadir `Enterprise/src` al mismo `PYTHONPATH` y ejecutar
`pytest tests/integration/test_managed_llm_request_contract.py -m integration -q`.

## Archivos de este lote

- `src/hermes/runtime/managed_profile_retention.py`
- `src/hermes/runtime/managed_llm_profile.py`
- `src/hermes/runtime/managed_llm_bootstrap.py`
- `src/hermes/runtime/__main__.py` — sólo propiedad de tareas de arranque/cancelación
- `src/hermes/mcp/application/mcp_server_manager.py`
- `tests/unit/test_managed_profile_retention.py`
- `tests/unit/mcp/test_mcp_connect_cleanup.py`
- `tests/unit/runtime/test_startup_task_shutdown.py`
- `tests/unit/runtime/test_sigterm_graceful_shutdown.py`
- `tests/integration/test_managed_llm_request_contract.py`
- Este informe.

No UI, no fixture `.ui-*`, no imagen, publicación, configuración del host ni
credenciales reales. La certificación macOS/Mach-O pendiente no cambia.
