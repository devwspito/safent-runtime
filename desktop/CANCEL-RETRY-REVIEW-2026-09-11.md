# Cancelación y reintento del bootstrap nativo

Base: `d2370de`. Corte acotado de `desktop/`, sin cambios en updater, CLI/runtime
Python, jaula, permisos del producto o instalaciones del usuario.

| Before | After | Why |
| --- | --- | --- |
| Reintentar creaba una señal que el comando Cancelar no conocía. | `BootstrapControl` conserva la señal del único intento activo. | El botón actúa sobre el trabajador real, también tras reintentar. |
| El loop devolvía Cancelled sin notificar la ventana. | Ambos retornos Cancelled notifican `cancelled_by_owner`; sólo el bootstrap local honrado permite retry manual. | Evita quedar esperando indefinidamente sin obligar a reiniciar la aplicación. |
| Retry/restart podían abrir trabajadores concurrentes. | Adquisición atómica y guard RAII hasta finalizar el trabajador. | No solapar reparación ni borrar una cancelación en curso. |
| Un gesto IPC retrasado podía afectar el intento siguiente. | `attempt_id` monotónico en snapshot y argumentos de cancelar/reintentar. | Rechazar el intento equivocado; no deducirlo del último estado disponible al recibir el comando. |
| La primera notificación se emitía dentro del handler IPC de retry durante este corte. | Se emite al principio del trabajador adquirido, antes de observar el host. | El smoke nativo detectó bloqueo de despacho que las pruebas sin ventana no reproducían. |

## Implementación

- `src-tauri/src/bootstrap_control.rs`: slot de intento, señal compartida,
  rechazo de identificadores obsoletos y liberación por Drop, incluso unwind.
- `src-tauri/src/boot.rs`: comandos usan el coordinador; inicio, retry manual y
  restart de bandeja pasan por la misma adquisición. Retry ocupado se rechaza;
  restart de bandeja ocupado no abre otro trabajador. Notificación explícita de
  cancelación. El gate irreversible `apply_gated` no cambia.
  Si falla la creación del trabajador, el snapshot de fallo se registra antes
  de devolver el error y se emite desde el runtime async existente: tampoco ese
  borde raro despacha eventos de webview desde el handler IPC síncrono. Startup
  no duplica la notificación del mismo fallo.
- `src-tauri/src/diagnostics.rs`: identidad de intento en el replay seguro;
  secuencia global de eventos permanece monotónica. Se reinicia sólo el contexto
  de última etapa/irreversibilidad, no el historial de diagnóstico.
- `src-tauri/src/main.rs`: registro del módulo. `src/main.ts`, `src/ipc.ts` y
  `src/bootstrap-state.ts`: contrato tipado y acciones dirigidas al intento.
  `ui/` se regenera con el build, sin edición manual.

No se cambió `CancelSignal` ni su semántica global. No hay reset compartido,
reintento automático ni nuevo servicio/lifecycle paralelo. Los comandos siguen
restringidos al loader local por las capacidades existentes.

## Evidencia de pruebas

Regresión roja real: se añadió a
`cancelling_before_the_point_of_no_return_stops_the_loop` la expectativa de
notificación Cancelled. Falló con exit 101 antes de implementar el retorno
notificado; después pasó con la suite completa.

Rust: **213 PASS** (126 unitarias, 48 adapters, 39 contratos CLI reales),
`cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` y `cargo build`.
Renderer: **97 PASS**, `npm run typecheck`, `npm run build`.
Las pruebas nuevas incluyen cancel→retry→cancel, señal anterior sin reset,
duplicados de cancel y retry, 8 adquisiciones simultáneas con exactamente un
ganador, gestos obsoletos después de terminar, cancel antes del primer intento,
liberación tras unwind y cancelación ignorada tras el punto irreversible. La
prueba renderer comprueba argumentos IPC reales del adaptador e identidad entre
dos intentos; no sustituye la prueba de la ventana nativa.

### Smoke de aplicación nativa, no preview HTML

Linux aarch64, Tauri/WebKitGTK real, Xvfb aislado. Código y recursos normales del
producto, sin instrumentación en la pasada final. La dependencia CLI fue una
**fixture de protocolo** ejecutable externa al repo: `facts` válido y
`stage-runtime` con progreso que permanece activo hasta la cancelación. No se
ejecutó Podman ni se alteró motor/instalación del usuario. Perfil XDG aislado en
`/tmp/safent-native-cancel.jUn2gu`; build en
`/tmp/safent-native-diagnostics.RlX4xP/desktop/src-tauri`.

Secuencia final observada: preparación real del wrapper → Cancelar →
«Cancelaste la preparación» con Reintentar → Reintentar → preparación → Cancelar
→ mismo estado final. Un solo proceso CLI `stage-runtime` durante el nuevo
intento; ninguno después de la segunda cancelación. Dos clics rápidos de retry
no abrieron un segundo proceso. Capturas inspeccionadas:

- `/tmp/community-native-cancel-cancelled-1.png`
- `/tmp/community-native-cancel-restarted.png`
- `/tmp/community-native-cancel-cancelled-2.png`

Durante la preparación del smoke se diagnosticaron también fallos del arnés
(permisos de ejecución perdidos al copiar la fixture). No se cuentan como
resultado del producto. El bloqueo IPC inicial sí se reprodujo con entrada al
comando Rust; la pasada normal final funciona al emitir desde el worker. No
quedaron logs de depuración en los archivos entregados.

## Comparación con el WIP `1cfb397`

Se inspeccionaron `boot.rs` y `ports.rs` con `git show`, sin cherry-pick. Coincide
en la necesidad de notificar Cancelled, pero aquel WIP reutiliza una señal
global con `reset()` y no excluye retries concurrentes: un nuevo intento puede
borrar la cancelación que aún está leyendo el anterior. Aquí se conserva la
idea de notificación y se reemplaza esa propuesta por propiedad por intento.
No se importó su código de diagnóstico, updater ni `CancelSignal::reset`.

## Límites pendientes

- Smoke completo con motor empaquetado y cancelación durante descarga real;
  aquí se probó el child-process driver con protocolo controlado.
- Paquetes nativos macOS y Windows, firma/notarización y distribución final.
- Cancelar durante la observación read-only espera la observación acotada:
  no se amplió el contrato de `EngineProbe`. El click no promete parada instantánea.
- No se cambió la salida desde bandeja, el actualizador ni el comportamiento de
  cancelación de otras operaciones. No declarar toda Community terminada por
  este corte.
- No se forzó agotamiento de hilos del SO en la app real. La ruta de error de
  creación del trabajador se revisó para conservar snapshot y dispatch diferido;
  la liberación del slot está cubierta por las pruebas RAII/unwind.
