# TASK-STATUS — outbox de estados observados de Community

Implementado sobre32fa8a6/d2370de. No es otro ejecutor ni concede aprobación.

## Contrato

- El repositorio de delegaciones conserva `to_instance_id` del sobre ya verificado
  por el daemon. Migración aditiva; filas legacy quedan NULL, sin adivinar destino
  ni reasignarlas tras nuevo pairing. La estructura firmada de12 campos no cambia.
- Una instantánea transaccional del buzón y `agent_tasks` proyecta pending→
  awaiting_approval, approved/pending→queued, in_progress→running,
  pending_approval→blocked y terminales persistidos. Aprobar no es completar.
- Cada cambio crea UUID+secuencia monotónica y payload canónico durable ANTES de
  HTTP. Reinicio/reintento reutiliza exactamente el mismo evento. Dos recolectores
  se serializan en SQLite; ningún cambio en permisos/HITL/leases del ejecutor.
- POST `/v1/delegations/{request_id}/status`, únicamente event_id/sequence/status/
  task_id. Sin instrucciones, conversación, memoria, mensajes de error ni secretos.
- Confirmación exacta accepted/ignored/event_id/sequence requerida antes de marcar
  entrega. Timeout/401/403/404/409/429/500/mal recibo conservan el evento original.
  No redirects. Lote máximo8 envíos, timeout2s por envío,200 observaciones por ciclo.
- Trabajo SQLite/HTTP fuera del event loop. Antes de cada envío se revalida estado
  activo, instancia, tenant, paired_at, endpoint y clave pública de asociación.
  Enterprise debe revalidar revocación en su propia transacción al recibir.
- Error de telemetría no impide el poll/ACK anterior; log estático sin error privado.

## Pruebas ejecutadas

DGX scratch `/tmp/safent-dashboard.ZQla4M`, Python3.12:

- Suite unit completa: **5518 PASS,19 SKIP,38 deselected,4 warnings**,194.21s.
  Log `delegation-status-full.log`. Skips de entorno/release ya documentados;
  no se confunden con verificaciones de la imagen publicada.
- Foco inicial integrado:96 PASS (outbox/inbox/repositorio/dashboard).
- Tras añadir sólo tests de reasociación/error: **35 tests outbox PASS**.
  Código de producto sin cambios posteriores a la suite completa.
- Ruff módulo/tests nuevos y diffcheck PASS.
- Fixture inicial falló al intentar insertar in_progress sin lease/worker y
  cancelled no admitido por el CHECK. Lease/worker corregidos en fixtures; no se
  desactivó ningún CHECK. Cancelled es un fallo preexistente separado reproducido
  con la cola real y registrado para migración posterior.

## Límites / siguiente corte

- Endpoint Enterprise desarrollado en paralelo; registrar su prueba conjunta
  runtime→TestClient antes de afirmar cierre end-to-end.
- Sólo observa instantáneas: no inventa etapas intermedias que no haya visto.
- Cancelled requiere reparar esquema real de cola. Expirados antes de admisión
  local todavía no producen telemetría; entrega y ejecución siguen separadas.
- Corrupción/rebinding/transición terminal inválida detiene recolección sin
  sobrescribir evidencia. Conflicto remoto conserva outbox y detiene el lote;
  falta cuarentena por solicitud y UX de reconciliación para no bloquear vecinas.
- Historial outbox no se poda silenciosamente: definir retención de entregados
  preservando watermark/idempotencia. No borrar UNKNOWN ni filas por reconectar.
- No prueba OAuth real, instancia Friendog real ni despliegue de producción.
