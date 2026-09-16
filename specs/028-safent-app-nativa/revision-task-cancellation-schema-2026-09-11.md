# TASK-CANCEL — cancelar realmente una tarea persistida

Auditoría encontró `SqliteWorkQueue.mark_cancelled()` escribiendo un estado que
el CHECK de `agent_tasks` rechazaba. No bastaba el botón ni el enum de dominio.
Regresión con cola SQLite real: **2 FAIL,1 PASS antes de corregir**.

## Corrección

- Migración P5 (user_version6) añade únicamente cancelled a la forma P4 y a los
  terminales sin claim/lease. Deriva el DDL anterior, sin duplicar otras32 columnas
  ni relajar evidencia para completed, ownership de claims, triggers/autoridad,
  worker o retry_count. No habilita nuevas tareas ni efectos.
- Copia/reemplazo transaccional bajo BEGIN IMMEDIATE, recheck de versión traslock;
  conserva filas/IDs/payload_signature/evidencia. Preserva índices y triggers
  existentes e inboundFK; foreign_key_check antes de commit. Fallo revierte todo.
- Columnas extra desconocidas detienen migración en vez de perder datos. Nunca
  borra una tabla temporal desconocida para forzar que pase la migración.
- Dedup de tareas vivas excluye cancelled. El fake in-memory se alinea con la cola
  real; se retira una constante terminal sin consumidores. Telemetría ahora
  admite cancelled porque ya es un estado persistible, no una simulación.

## Evidencia

DGX scratch `/tmp/safent-dashboard.ZQla4M`:

- Foco SQLite/migraciones/in-memory/telemetría **141 PASS**, forzando `-m ""`
  para incluir las integraciones que el comando por defecto deselecciona.
- Se detectaron5 expectativas obsoletas de migración007 ya FALLANDO en baseline
  canónica sin este cambio (5FAIL/33PASS): aún esperaban versión3 y3tipos aunque
  P4 existente ya dejaba versión5 y external_delegation. Corregidas para verificar
  cadena aplicada>=P2 y catálogo4defaultdeny. Tests P5 verifican versión6 exacta.
- Suite ampliada final `tests/unit tests/tasks`: **5711 PASS,19 SKIP,64 deselected,
  6 warnings**,249.04s. Incluye test adicional de paridad del fake de cancelación.
  Log `tasks-cancel-full.log`. Skips de entorno/release no se declaran verificados.
- Reinicio tras cancelar, nuevo dedup, claim incorrecto, doble cancelación,
  completed sin evidencia, inboundFK, triggercustom, columnasdesconocidas,
  rollbackFK y8 migradores concurrentes comprobados. Diffcheck PASS.

No se aplica migración a una instancia del usuario en este corte, ni se publica
imagen. Upgrade de motor empaquetado real/backuprestore completo sigue enrelease.
