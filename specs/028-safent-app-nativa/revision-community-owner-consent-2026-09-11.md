# Community — contratos residuales de confirmación local

11 de septiembre de 2026. Complementa e572eeb; no cambia la autenticación MFA de
Enterprise, no activa telemetría ni suspende ningún equipo.

## Cambio y frontera

Los servicios internos AlwaysOnSupervisor y TelemetryOptInService todavía pedían
`totp_validated` tras retirar el MFA de Community. Se sustituye por
`owner_confirmation_validated`: un resultado interno del adaptador autenticado,
no una credencial ni un campo que pueda autorizar un agente por HTTP/MCP.

Se exige el booleano exacto True y UUID de propietario tipado. Una cadena, un
entero truthy o un actor ausente no habilitan la operación. `force` sólo omite el
drain, nunca la confirmación. El opt-in permanece apagado por defecto, sólo admite
exportadores enumerados y mantiene la auditoría firmada. Se retiran imports muertos.

La búsqueda de consumidores sólo encuentra llamadas de pruebas a estos métodos
de habilitación/suspensión. No existe una ruta CLI/UI de producto que los invoque:
no se presenta este corte como una elevación ya operativa. El shell crea el
servicio de telemetría en memoria y deshabilitado en cada arranque; su comentario
anterior afirmaba una carga desde DB que no estaba implementada. Se ha corregido
esa documentación. Estado y cadena durable, endpoint autorizado y equivalente UI
siguen pendientes si se habilita esta capacidad en producto.

No se conserva el alias antiguo TOTP en estos contratos internos sin consumidor
productivo. No se cambia el router de aprobaciones Enterprise ni se eliminan sus
controles. No se transforman confirmaciones locales en aprobaciones automáticas.

## Verificación

Snapshot DGX aislado: `/tmp/safent-owner-consent.cQkrtK`, base Git0eab3fe y sólo
este delta. Python3.12, sin modificar daemon, entorno global ni datos reales.

- Cuatro archivos de prueba consumidores: **50 PASS**, incluidos20 casos nuevos
  de confirmación/actor/exportador inválidos y force sin autorización.
- Suite agents_os +test_prometheus_exporter_wired: **1011 PASS,8 SKIP**,7.14s.
  Siete skips requieren parámetros de plantillas Landlock y uno contratos spec003
  no presentes en Git. No se cuentan como cobertura ejecutada.
- Ruff de seis archivos modificados y `git diff --check`: PASS.
- Primera pasada amplia de scratch falló por no copiar ops/launchers/migraciones;
  reejecución con recursos versionados presentes pasó. No fue un fallo de producto
  ni se contabiliza aquella ejecución como verde.
- Log final DGX: `/tmp/safent-owner-consent-final.log`.

La prueba usa adaptadores de sistema falsos: verifica autorización y secuencia,
no suspende hardware real. La integración nativa y actualización de imagen siguen
siendo gates independientes.
