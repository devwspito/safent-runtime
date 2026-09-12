# Runtime + Ads: actualización cerrada y readiness

Auditoría sobre Runtime `5addd59`, rama `fix/safent-review-20260911`.
Este corte corrige readiness; **no implementa una actualización transaccional conjunta**.

## Decisión de publicación

Un único tren de versiones compatibles: publicar safent-ads no debe cambiar una
instalación existente. El digest exacto de Ads lo autoriza el VersionSet firmado
de Safent; `latest`, etiquetas mutables y overrides arbitrarios no son una
selección de actualización válida. La actualización remota independiente y
transaccional permanece fuera de la superficie habilitada hasta conectar los
puertos reales, journal y recuperación. El endurecimiento CLI correspondiente
lo realiza otro corte: no se atribuye a estos archivos ni se certifica aquí.

## Corrección demostrada

`CompanionHealthChecker` aceptaba HTTP 200 con contrato desconocido, DB degradada
o valores de cuenta no booleanos como señal positiva. Ahora sólo acepta el wire
contract revisado `safent-ads` `1.0.0`, sus cuatro campos y booleanos estrictos,
`status=ok` y `db=ok`. Es el contrato emitido actualmente por
`safent_ads/mcp/application/health.py`, no la versión del paquete Ads.

El cuerpo se lee con límite de 8 KiB, sin descompresión ni redirecciones. Se
mantiene TLS con CA propia, SNI y resolución al IP validado; no se exponen datos
crudos inesperados. Un cuerpo incompatible/malformado/degradado retorna el estado
existente `unreachable` con `reachable=true` cuando hubo respuesta HTTP. No se
añaden estados UI ni mensajes de éxito. Sin cuentas vinculadas sigue devolviendo
`no_accounts`. El siguiente sondeo vuelve a leer los datos: no hay caché de éxito.

**Límite importante:** este lector no es la puerta de autorización MCP ni una
comprobación de digest de la imagen en ejecución. En
`dbus_runtime_service.reload_companion_presence`, la conexión MCP se intenta
antes del sondeo. No se certifica la promesa pendiente de impedir el registro de
herramientas de un companion incompatible. El pin de imagen y la autoridad de
herramientas son controles separados que no sustituye este cambio.

## Lo existente y lo pendiente

| Superficie | Evidencia actual | Límite pendiente |
| --- | --- | --- |
| Updater nativo | `desktop/src-tauri/src/update/native.rs` es real, app-only, con descarga verificada y gesto local. | No actualiza atómicamente engine + Ads. |
| Coordinador conjunto | `update/orchestrator.rs` contiene `UpdatePorts` y secuencia con rollback; sus únicos implementadores/callers localizados son tests. | Faltan puertos de producción, journal durable, recuperación tras muerte/relaunch y comprobación del par activo. Un rollback también puede fallar. |
| VersionSet/plan | `update/types.rs`, `plan.rs` y `tauri_updater.rs` modelan digests y firma; `min_app_version` exige wrapper mínimo. | Firma no demuestra compatibilidad de APIs/esquemas; faltan precondiciones de migración y aceptación conjunta. |
| CLI companion | En la base auditada, `cmd_companion_update` hacía pull, guard de Alembic, compose up y escritura de marcador. | No hay prepare/health/switch/rollback conjunto durable. Revisar por separado el endurecimiento del pin realizado en paralelo. |
| Migraciones y copias | El guard existente comprueba que la imagen candidata conoce la revisión DB; hay backup/restore separados. | No demuestra reversibilidad ni integra snapshots coherentes de ambas bases en una transacción de actualización. Nunca bajar esquema por mera etiqueta. |
| OTA bootc | `bootc_updater.py` es un flujo OS distinto. | No conectarlo implícitamente al updater desktop ni presentarlo como recuperación conjunta. |

Antes de habilitar el flujo conjunto: persistir intención y digests antes de
mutar; verificar firma/compatibilidad; preparar y validar ambos artefactos;
quiesce y snapshots verificables; aplicar y aceptar salud + identidad exactas;
publicar un único estado activo; recuperar/revertir tras cada frontera y tras
relaunch. Probar fallos, caída del proceso, migración incompatible, rollback
fallido y el caso Ads nuevo/runtime antiguo. Reutilizar `UpdatePorts`, no crear
un segundo actualizador.

## Pruebas de este corte

- RED sobre implementación anterior: **27 FAIL** en los casos nuevos iniciales.
- Fuente final: **89 PASS**, sin skips. Incluye dos pruebas TLS reales en loopback
  con CA/certificado efímero: respuesta válida y redirect no seguido; el resto
  cubre contrato, tamaños, compresión, JSON y regresiones reload/shell.
- Ruff de los tres archivos: **PASS**. No full suite ni servicios reales.
- DGX Python 3.12 / pytest 9.0.2. Scratch aislado:
  `/tmp/safent-update-health.Uq4Spo`, archive de `5addd59` + tres archivos propios.

```sh
PYTHONPATH=src python3 -m pytest \
 tests/unit/agents_os/test_companion_health_check.py \
 tests/unit/agents_os/test_companion_health_contract.py \
 tests/unit/agents_os/test_companion_reload.py \
 tests/unit/shell_server/test_companions.py \
 tests/unit/shell_server/test_companion_reload_cli.py -q -rs --tb=short
python3 -m ruff check \
 src/hermes/agents_os/infrastructure/companion_health_check.py \
 tests/unit/agents_os/test_companion_health_check.py \
 tests/unit/agents_os/test_companion_health_contract.py
```

Sin commit, bump, publicación, cambio de esquema ni actualización de servicios
en este corte. Las pruebas TLS usan exclusivamente credenciales ficticias.
