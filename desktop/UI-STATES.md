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
`retryable`; «Reintentando…» sin doble envío). «Exportar diagnóstico» aparece
deshabilitado con explicación hasta que exista el comando nativo.
`<details>` con Código, Etapa, Mensaje técnico. Cancel también resuelve
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

**Sigue sin cablear**: `export_diagnostics` — el botón «Exportar diagnóstico»
está deshabilitado, no invoca un comando inexistente (la CLI ya expone
`diagnostics --out <path>`, contrato §4; falta el comando + la superficie de
guardado/revelado del fichero — no es wiring mecánico, es una decisión de UX
pendiente). Ver el informe de integración.

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
