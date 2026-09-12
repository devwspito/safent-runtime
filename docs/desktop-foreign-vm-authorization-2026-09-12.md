# Desktop: bloqueo de arranque y autorización de una VM ajena

## Corte implementado y alcance

Base scratch `fd0f4fa` (árbol equivalente al Runtime canónico `76ff5c7`).
Rama aislada `fix/desktop-vm-conflict-20260912`. No cambia CLI, Rust,
capabilities, política de arranque ni ninguna VM real. No publica artefactos.

| Before | After | Why |
| --- | --- | --- |
| `machine_start_failed`, `retryable=false`, decía «Vuelve a intentarlo» | Explica el bloqueo, ofrece el diagnóstico existente y aclara que no detiene otras máquinas por su cuenta | No sugerir una acción que el estado no permite ni afirmar una causa no detectada |
| Reintentar oculto pero habilitado | Oculto **y** deshabilitado; handler comprueba el estado vigente | Una pulsación pendiente/sintética no debe reactivar el arranque |
| Reducer aceptaba reintentar cualquier fallo | Sólo acepta un fallo retryable sin reintento en curso | Estado coherente, sin duplicar peticiones |

Se conserva la pantalla compacta existente, el foco en su encabezado, la exportación
de diagnóstico y los estados accesibles. No se añade movimiento ornamental. Esta
corrección **no implementa** el gesto «detener la VM ajena y continuar», ni muestra
un nombre deducido de texto libre.

## Por qué la acción requiere una frontera nativa adicional

- `safent::cmd_ensure_machine` devuelve un fallo genérico; no emite una identidad
  estructurada del recurso ajeno que bloquea. Su corrección CLI pertenece a otro
  corte y no está incluida aquí.
- `desktop/src-tauri/src/diagnostics.rs` publica `BootstrapSnapshot` sin el detalle
  libre del error. `desktop/src/bootstrap-state.ts` recibe `code` y `retryable`,
  no una VM autenticada. Es correcto conservar esa minimización de datos.
- Los IPC existentes son cancel/retry/diagnostics/snapshot. `cancel_bootstrap`
  cancela un intento activo: no es un permiso para detener máquinas ajenas.
- Parsear stderr, pedir un nombre al usuario o reutilizar `retry_bootstrap` con
  un flag de stop convertiría datos de presentación en autoridad. No hacerlo.

## Contrato propuesto (NO implementado)

1. **Observación nativa.** El adaptador obtiene un inventario estructurado del
   Podman verificado y usado por ese intento. Sólo propone una VM que figure
   activa, distinta de la propia y responsable del conflicto. Si el inventario
   falla, es ambiguo o contiene campos inválidos, conserva el fallo genérico.
   No infiere identidad de stderr ni del éxito de `podman info` de otra VM.
2. **Challenge local y de una sola utilización.** El backend conserva durante
   como máximo 60 segundos un identificador opaco aleatorio ligado a intento,
   ejecutable/provider, identidad de configuración y ejecución de la VM detectada.
   Debe investigar los identificadores realmente disponibles en Podman/macOS,
   incluyendo recreación bajo el mismo nombre. No asumir que el nombre es ID.
   El snapshot sólo añade un objeto tipado y cerrado para `foreign_vm_active`:
   `{challenge_id, machine_label, expires_at}`. Label acotado, sin controles ni
   bidi, mostrado con `textContent`; nunca interpolado en un comando.
3. **Dos gestos humanos.** En la misma pantalla compacta: título «Otra máquina
   está usando Podman», nombre leído de backend, explicación «Detenerla
   interrumpirá los contenedores y tareas que tenga en ejecución. Safent no la
   eliminará». Botones **Cancelar** (opción segura) y **Detener [nombre] y
   continuar**. Enter no debe autorizar automáticamente al aparecer. Escape
   cancela la revisión. El foco inicial permanece en encabezado/cancelar.
4. **IPC de confirmación específico.** Propuesta:
   `resolve_machine_conflict({attempt_id, challenge_id, decision})`, con
   `decision: cancel | stop_and_continue`, campos adicionales rechazados.
   No recibe nombre, comando, rutas, PID ni flags. Sólo loader local, no origen
   remoto/producto; capability dedicada y comprobación explícita del llamante.
   Community no añade MFA: el gesto explícito del propietario es la autorización.
5. **Consumo y revalidación.** Bajo el control del mismo intento, consumir el
   challenge una sola vez; rechazar caducidad, replay, intento sustituido, segundo
   clic, origen no autorizado y VM recreada/reiniciada/cambiada. Reobservar antes
   del efecto. Si cambió el recurso, emitir una revisión nueva, no reutilizar el
   consentimiento. `cancel` invalida el challenge sin parar ni arrancar nada.
6. **Efecto limitado y continuidad.** Worker nativo sin bloquear la UI. Parada
   ordenada únicamente de la VM almacenada, sin `--all`, `--force`, eliminación,
   adopción ni cambio de conexión por defecto. Timeout acotado, resultado incierto
   sin reintento automático. Sólo tras verificar esa VM detenida se inicia una
   nueva observación normal de Safent; recibir el IPC no equivale a estar listo.
   Cancelar/cerrar la revisión una vez iniciada la parada no promete deshacerla.

**Riesgo de concurrencia que debe cerrarse antes de activar:** comprobar una
identidad y luego ejecutar `podman machine stop <nombre>` son dos operaciones.
El mutex de Safent no bloquea otra aplicación Podman. Debe verificarse si la
versión soportada ofrece un bloqueo/identidad estable que permita enlazar la
parada con la VM revisada. Si no existe esa garantía, no certificar protección
contra sustitución concurrente ni exponer una acción supuestamente atómica:
mantener instrucciones de resolución manual hasta disponer de una frontera segura.

## Superficie exacta del siguiente corte

- `safent`: detección estructurada y primitive privada de conflicto si procede,
  sin ampliar la CLI a nombres libres provenientes de webview.
- `desktop/src-tauri/src/domain.rs`, `ports.rs`, `engine_adapter.rs`: código y
  resultado tipado; pruebas del proceso real/fake Podman determinista.
- `desktop/src-tauri/src/bootstrap_control.rs`, `diagnostics.rs`, `boot.rs`:
  challenge, replay snapshot y worker bajo identidad de intento.
- `desktop/src-tauri/src/main.rs`, `build.rs` y `capabilities/default.json`:
  registrar sólo el IPC local dedicado.
- `desktop/src/{bootstrap-state,lifecycle,ipc,main,render,failure-copy}.ts`,
  `index.html`, `styles.css` y tests; regenerar `desktop/ui` versionado.

Pruebas mínimas antes de habilitar: ninguna parada sin gesto; cancel/Escape;
nombre inválido/ambiguo y stderr malicioso; replay/expiry/reload/doble clic;
intento nuevo; origen remoto; otra VM toma el bloqueo; recreación con el mismo
nombre entre observación y parada; timeout/resultado incierto; parada exacta,
sin `force/all/rm`, y readiness sólo tras comprobar Safent. QA 1280/390,
teclado y reduced-motion; una prueba real macOS únicamente con VM de fixture
creada para el test, nunca una VM del propietario.

## Evidencia de este corte

Ejecutado en `/tmp/safent-vm-conflict.M0VGVY/desktop`, Node 26.0.0:

```sh
npm test -- src/main-actions.test.ts src/lifecycle.test.ts src/failure-copy.test.ts
# Antes del fix: 3 FAIL / 69 PASS; reproduce los tres defectos.
npm test
# Después: 118 PASS, 10 archivos.
npm run typecheck
npm run build
```

Typecheck y build correctos. No suite Rust (sin cambios Rust), QA visual nueva,
VM real ni autorización de parada implementada. La corrección es renderer-only;
no convierte el guard de UI en autorización de seguridad del backend.
