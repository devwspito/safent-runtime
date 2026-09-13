# Programadas: cierre y foco del diálogo de nueva tarea

Corrección acotada sobre Runtime `bfe8acd`, sin modificar backend, versiones ni la instalación del Mac. Se reutiliza Base UI Dialog, ya empleado por Drawer y otros diálogos del producto.

| Before | After | Why |
| --- | --- | --- |
| `motion.div` con `role=dialog`, sin gestión de Escape/foco | Dialog.Root/Portal/Popup con foco inicial y retorno explícitos | El teclado puede cerrar y seguir desde el botón Nueva tarea |
| Tab podía alcanzar el contenido de fondo | Modal y focus guards del componente compartido | El foco permanece en la decisión abierta |
| Cerrar/cancelar disponibles durante `createTask` | Guard síncrono + botones deshabilitados durante la promesa | No ocultar una escritura en curso ni duplicar el envío |
| Entrada/salida desplazada con spring | Apertura/cierre inmediatos, sin movimiento | Pauta Emil: interacción repetida de teclado y reduced-motion |

## Evidencia

- Tres regresiones sobre **CalendarView real**, API simulada: Escape y retorno al invocador; ciclo de foco; cierre bloqueado durante creación y recuperación tras error.
- Antes: 3 FAIL en snapshot previo con dependencias de lock. Después: 3 PASS. El test de focus guards espera el `requestAnimationFrame` que usa Base UI para devolver el foco, no un timeout arbitrario.
- Suite frontend completa final: **362 PASS / 55 archivos**, 3.51 s.
- `npm run build` (TypeScript + Vite): **PASS**, Vite 2.98 s.
- Entorno aislado DGX `/tmp/safent-calendar-ui.8trmU7/frontend`, Node24.13.1, `npm ci`, `NODE_OPTIONS=--no-experimental-webstorage`. No se modificaron node_modules canónicos.
- Logs: `/tmp/calendar-modal-red.log`, `/tmp/calendar-modal-green2.log`, `/tmp/calendar-ui-full-final.log`, `/tmp/calendar-ui-build-final.log`.

La primera ejecución contra node_modules canónicos no pudo importar Base UI (instalación antigua); no se contó como prueba ejecutada. Un chequeo de tipos detectó luego dos respuestas de fixture con forma incorrecta; se corrigieron a `{available:true,tasks:[]}` y se repitieron suite y build completos, con los resultados finales anteriores.

No se ha certificado aún este delta en el nuevo artefacto macOS: esa aceptación corresponde a la release integrada. No se publica ningún artefacto desde este lote.
