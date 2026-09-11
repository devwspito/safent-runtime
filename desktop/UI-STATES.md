# Estados de la ventana (`desktop/src`)

**Preparando** (`kind: 'preparing'`) — pantalla por defecto: «Preparando
tu espacio», texto en vivo (`aria-live="polite"`) con la etapa activa
(`stage.label`, ya en español del CLI). Lista de etapas «Hecho»/«En curso»
con progreso real («43 de 86 MB»), nunca un porcentaje inventado. «Cancelar»
activo hasta el punto de no retorno (hoy, `container`); después se
deshabilita con nota: «Esta fase ya no se puede cancelar; espera a que
termine.»

**Fallo** (`kind: 'failed'`) — la ÚNICA pantalla de error (FR-033). Titular
en lenguaje del dueño según `FailureCode` (`failure-copy.ts`; nunca
«podman»/«contenedor»/«VM» fuera de «Detalles»). «Reintentar» (oculto si no
`retryable`; «Reintentando…» sin doble envío). «Exportar diagnóstico de arranque»
abre el selector nativo sólo dentro de la app; en preview sin Tauri queda
deshabilitado con explicación.
`<details>` con Código, última Etapa y explicación de la información omitida
(no se transportan mensajes privados en el snapshot). Cancel también resuelve
aquí: el contrato lo trata como un `failed` más.

**Reconectando** (`kind: 'reconnecting'`) — red de seguridad de
FR-012/SC-012, no el recorrido normal. Dos motivos: `token_missing` («Safent
necesita volver a autorizar esta ventana») y `engine_restarted` («Safent se
reinició. Un momento mientras vuelve a conectar»). No llama a ningún
endpoint: espera la señal del envoltorio, sin ráfagas de reintentos.

**Listo** (`kind: 'ready'`) — transición breve antes de que el envoltorio
navegue al producto.

Foco: al ENTRAR en Fallo o Reconectando, el foco salta al título (NFR-005);
un re-render del mismo estado (p. ej. un `progress`) no lo roba de vuelta.

## Huecos de contrato — RESUELTOS en la integración (app-desk-integration)

`app-engine.md` no definía el canal Tauri wrapper→webview; ahora lo hace en
§8, documentando lo que `boot.rs` (`EngineEventPayload`/`ReconnectingPayload`)
realmente emite, verificado contra el código real de la línea del núcleo:

1. **Canal y forma** — `safent://engine-event`, discriminante `kind` (NO `t`),
   campo de etapa `stage` (NO `id`); `ready` lleva `{app_version,
   engine_digest, companion_digest}`, no `{endpoint_ref}` — el vale nunca
   sale de Rust, así que la ventana no necesita saberlo. `lifecycle.ts`/
   `ipc.ts` ya consumen esta forma real, no la adivinada.
2. **Reconexión** — confirmado `safent://reconnecting`, `{reason}`, sin tag.
3. **`FailureCode` de cancelación** — el núcleo añadió `cancelled_by_owner` Y
   `cli_porcelain_unsupported` como extensiones propias del envoltorio (no
   forman parte del vocabulario de 21 códigos del CLI, §3) que SÍ viajan por
   este canal. Añadidos a `lifecycle.ts`/`failure-copy.ts`.
4. **Punto de no retorno** — el núcleo SÍ lo manda ahora: `stage` events
   llevan `point_of_no_return: boolean` (`boot.rs::bootstrap_point_of_no_return`).
   El cliente ya no lo adivina (se borró el `POINT_OF_NO_RETURN` hardcodeado).
5. **`Failed` sin etapa** — a diferencia de lo asumido, `EngineDegraded`/
   `Failed` NO llevan `stage` (una violación de preflight no tiene etapa
   activa todavía — decisión deliberada del núcleo, ver `ports.rs`). El
   reductor deriva `stageId` de la última etapa activa vista (`undefined` si
   ninguna); la pantalla muestra «—» en ese caso.

`window_policy.rs` emite `safent://restart-engine-requested` y
`safent://quit-requested`; `boot.rs::start` ahora escucha ambos y ejecuta el
apagado real (`stop_engine_best_effort` + reconectar / salir).

## Diagnóstico de arranque y replay (2026-09-11)

`export_diagnostics` ya está cableado al selector oficial de Tauri en Rust.
La afirmación anterior de que la CLI implementaba `diagnostics --out` era
incorrecta: ese verbo aparece en el contrato documental, no en el dispatcher
real de `safent`. La exportación entregada es **diagnóstico mínimo de arranque**,
no un bundle de soporte completo del runtime.

JSON schema1: versión pública de app, OS/arquitectura, últimos128 eventos
bootstrap proyectados por allowlist y contador de eventos omitidos. Nunca
recopila archivos, conversaciones, variables, keychain, stdout/stderr, etiquetas
libres, detalles de error, URL o credenciales. Ni siquiera los guarda en el
buffer. No ejecuta CLI ni scripts para exportar.

Selector nativo elige destino; el frontend no puede proporcionar ruta ni cuerpo.
Escritura atómica mediante temporal hermano (0600 en Unix), rechazando symlinks
y destinos no regulares. Reemplazo de un archivo existente sólo tras la selección
y confirmación del diálogo del SO. Doble invocación bloqueada en Rust y renderer.
Cancelación: estado normal, sin archivo; error: aviso recuperable sin mensaje
crudo de sistema; éxito sólo después de escritura confirmada. No abre ni sube el
archivo automáticamente.

El loader escucha `safent://bootstrap-state` y después invoca
`get_bootstrap_state`: snapshot `{sequence,attempt_id,event,last_stage,point_of_no_return}`
tipado, sin texto libre. Ignora secuencias antiguas/duplicadas para que un snapshot
tardío no sustituya eventos nuevos o un reintento. Reutiliza `reduceLifecycle`;
no hay otro motor de arranque. Corrige el fallo inicial emitido antes de montar
la ventana, reproducido en binario real. Ambos comandos nuevos se conceden
**sólo al loader local**, nunca a `remote-ui`; no se concede acceso JS general a
diálogos o filesystem. Los canales legacy de eventos siguen existiendo, pero
este loader usa el canal seguro con replay.

Verificación:95 renderer tests,207 Rust tests, typecheck/build/fmt; binario Linux
aarch64 real bajo Xvfb con runtime deliberadamente ausente, selector GTK real,
cancelación y guardado JSON0600 comprobados. Mac/Windows y distribución firmada
siguen pendientes. No se publicó imagen final.

## Feedback IPC (revisión 2026-09-11)

Errores de suscripción al lifecycle, cancelación y reintento tienen mensaje
visible; no rechazos de promesa sin capturar. Doble envío bloqueado mientras
invoke está pendiente. Error al solicitar retry restaura el fallo anterior sólo
si no llegó un evento más nuevo del motor. La UI nunca anuncia que cancelar o
reintentar completó el trabajo por el mero acuse IPC. Sin Tauri, invocar una
acción falla explícitamente (no éxito ficticio del preview browser).

Arranque claro/oscuro neutral sin animación decorativa del logo; progreso
indeterminado con transform, reduced-motion desactiva movimiento. Cambios
generados en `ui/` mediante `npm run build`, no editados a mano.

## Cancelar y reintentar un intento concreto (2026-09-11)

La cancelación honrada antes del punto irreversible emite `cancelled_by_owner`
y habilita **reintento manual**. No reintenta automáticamente ni modifica la
semántica global del error Cancelled fuera del bootstrap. El acuse del botón no
es confirmación de que el proceso ya se detuvo: se espera el evento final.

Rust mantiene un único intento activo mediante guard RAII. Cada intento recibe
una señal nueva; nunca se resetea la señal que conserva un trabajador anterior.
El snapshot identifica el intento con `attempt_id`; cancelar/reintentar envían
ese identificador y se rechazan gestos obsoletos, reintentos concurrentes y
cancelaciones sin trabajador. El botón permanece inactivo hasta recibir una
identidad real. El punto de no retorno sigue gobernado por el núcleo.

La etapa inicial del reintento se emite desde el trabajador, no desde el handler
IPC síncrono: evita bloquear el despacho de eventos de la ventana nativa. Véase
`CANCEL-RETRY-REVIEW-2026-09-11.md` para regresiones, evidencia del binario real y
límites de la CLI de prueba. Esta revisión no valida el actualizador ni publica
un instalador.
