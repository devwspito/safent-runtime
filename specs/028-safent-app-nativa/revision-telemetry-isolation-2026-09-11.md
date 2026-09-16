# TASK-CE/EE — aislar fallos de sincronización por encargo

Base `f479093`, 2026-09-11. Sin despliegue ni acciones publicitarias.

Un cambio de identidad/estado terminal local revertía la transacción de recogida
completa; un HTTP409/timeout detenía el envío completo. Un encargo defectuoso podía
impedir indefinidamente informar de todos los demás.

Ahora el conflicto local queda en una cuarentena durable por request. No se cambia
su estado, identidad, evento, secuencia, recibo ni ejecución. No hay expiración que
lo desbloquee. Los demás encargos continúan. El primer motivo se conserva sin
mensajes privados ni errores remotos arbitrarios. Los conflictos HTTP409/422 se
aislan igual; HTTP404 puede ser una versión antigua del servidor y se reintenta.

Cada pasada envía como máximo el evento pendiente más antiguo de ocho encargos.
Un registro durable del último intento permite rotación justa incluso después de
reiniciar: ocho errores transitorios no monopolizan la cola. No se adelanta una
secuencia de un mismo encargo. 401/403/429 detienen la pasada sin tratar una
credencial revocada o un rate limit como corrupción del evento. Cada envío
revalida el emparejamiento; sólo el recibo exacto marca `delivered=1`.

## Revisión UI — skill Emil

| Before | After | Why |
| --- | --- | --- |
| Tarea completada sin indicar que Enterprise no recibe su estado. | Aviso discreto dentro del detalle y marca «Sin sincronizar» en la fila; también aparece en «Necesitan atención». | Separar ejecución local de sincronización; no sugerir repetir efectos. |
| Fallo interno sin estado consultable por el propietario. | Dashboard autorizado y read-only expone sólo códigos permitidos; conserva resultado y aprobaciones. | Recuperación visible sin nuevo canal privilegiado. |
| Posible mensaje de error arbitrario. | Texto de UI cerrado; código desconocido se convierte en `unknown_conflict`. | No filtrar respuesta privada, identidad interna o texto remoto. |

No se añaden animaciones ni modales, ni «sincronizado» cuando falta evidencia. Un
estado pendiente se distingue de uno bloqueado. La vista no modifica la cuarentena.

## Pruebas

- Runtime DGX scratch `/tmp/safent-dashboard.ZQla4M`: **5729 PASS, 19 SKIP,
  64 deselected, 6 warnings**, 252.35 s, `telemetry-isolation-full.log`.
  Comando: `PYTHONPATH=src python3 -m pytest tests/unit tests/tasks -q -rs --tb=short`.
- Foco telemetría/dashboard: **118 PASS**. Incluye fallo permanente/transitorio
  + vecino, rotación tras reapertura, orden por encargo, cambio de ejecución,
  regresión terminal, evidencia intacta y proyección UI sin texto remoto.
- E2E cruzado real SQLite Community → API Enterprise: **2 PASS**, incluidas
  pérdida de recibo, replay y dos formas de revocación. Sólo efectos de fixtures.
- Frontend: **220 PASS**, TypeScript/build PASS; Node Mac requiere
  `NODE_OPTIONS=--no-experimental-webstorage npm test` para jsdom.
- Chrome headless con componente producto y datos ficticios: 1280×900 y390×900,
  abrir/cerrar detalle bloqueado y pendiente, cero errores JS ni overflow.
  Capturas `/tmp/safent-sync-1280.png` y `/tmp/safent-sync-390.png`, inspeccionadas.
- Ruff y diff-check PASS. Fixtures `.ui-sync-review.*` excluidas del commit.

## Pendiente operativo

La reconciliación humana auditada y su UI/acción de recuperación siguen pendientes;
NO borrar cuarentenas/eventos ni volver a ejecutar una tarea para «desatascar» el
estado. Se necesita comparar la evidencia de ambos extremos antes de autorizar una
corrección. También faltan retención histórica y expiración previa a admisión.
Estos controles no habilitan LLM gestionado, Ads real ni imagen final Community.
