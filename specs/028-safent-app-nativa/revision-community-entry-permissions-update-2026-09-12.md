# Community: entrada, permisos y recuperación de actualizaciones

Fecha: 2026-09-12. Lote focal de UI; no cambia autoridad, backend, protocolo de instalación ni imagen publicada. La aplicación Community sigue siendo nativa; la QA Chrome descrita abajo no equivale a validación del binario Tauri.

## Cambios justificados (Emil)

| Before | After | Why |
| --- | --- | --- |
| Cancelar no distinguía solicitud IPC pendiente de cancelación consumada. | Estados solicitando/solicitada/error, bloqueo single-flight y espera del evento terminal real. | Un gesto debe producir feedback inmediato sin afirmar un efecto que todavía no ocurrió. |
| Una respuesta de cancelación de un intento anterior podía confundir el siguiente. | Generación por intento y descarte de respuestas antiguas; el punto irreversible sigue bloqueando cancelar. | Recuperación predecible sin alterar el coordinador nativo. |
| Al fallar Rechazar, el foco podía acabar en Permitir; una respuesta tardía podía resolver otra propuesta. | Retorno al botón de la decisión original y componente ligado a proposal_id con guarda de desmontaje. | Evitar inducir una decisión contraria o atribuir la respuesta a otra solicitud. |
| Confirmación pequeña podía desbordar con texto largo o viewport bajo. | Altura limitada al viewport, scroll interno, texto y acciones adaptables. | Conservar revisión completa y controles accesibles por teclado. |
| Reconexión tenía composición distinta del arranque. | Jerarquía compacta, marca discreta y foco inicial en el encabezado. | Una sola acción de recuperación clara, sin nuevas animaciones. |
| Fallos de getters se convertían en versión vacía/sin actualización o lista vacía de instalaciones. | Rechazo HTTP propagado, estado desconocido visible y comprobación manual recuperable; último estado conocido preservado. | Un fallo de lectura no demuestra disponibilidad ni ausencia de trabajo. |
| Un resultado ambiguo podía facilitar repetir una solicitud de actualización. | Confirmación explícita, single-flight, snapshot de revisión, acuse validado y lectura obligatoria tras incertidumbre. | No duplicar efectos ni equiparar solicitud aceptada con instalación terminada. |
| El otro consumidor de solicitudes (Anuncios) podía ofrecer instalar/reparar tras fallar la consulta. | Comprobando/no verificado/reintentar consulta; cero POST desde estado desconocido. | Mantener coherencia del contrato compartido sin inventar disponibilidad. |

Se mantienen las rutas de aprobación existentes, la jaula, la separación updater del daemon/aplicación desktop y los bloqueos nativos. No se añade movimiento de entrada ni animación al gesto repetido de teclado; el feedback se comunica mediante estado, foco y texto.

## Evidencia ejecutada

- Frontend: **58 PASS / 8 archivos** (`SystemUpdateFooter`, `ApprovalCard`, `CompanionInstallAction`, `useCompanionInstall`, `api/update-errors`, `ConfirmDialog`, `ReconnectScreen`, `App`). Incluye HTTP 503 real del wrapper, error de rechazo/foco, propuesta reemplazada, consulta desconocida sin POST, aprobación de update obsoleta, doble clic, acuse no confirmado y preservación de solicitud conocida tras fallo de polling.
- Una regresión nueva de polling detectó un bloqueo real causado por ubicar `mutating=true` en cleanup. Se corrigió antes de la pasada final conjunta; no se cuenta el resultado anterior con ese fallo como verde.
- Desktop: **40 PASS / 6 archivos** (`main-actions`, `render`, `native-action`, `diagnostics-action`, `bootstrap-state`, `native-updater`). Pruebas DOM de main real con IPC simulado: cancelar→acuse→terminal→reintentar, foco, cancelación vieja descartada y fase irreversible.
- `desktop npm run build && npm run typecheck`: PASS. Assets generados sincronizados en `desktop/ui`.
- `frontend npm run build`: PASS (incluye tsc). El nuevo reparto de chunks App/Vite pertenece al lote paralelo root; no se atribuye a este corte.
- `git diff --check`: PASS.
- Chrome propio, 1280×900 y 390×900, `prefers-reduced-motion: reduce`: rechazo por teclado conserva foco; actualización abre con Cancelar enfocado; Escape restaura el disparador; confirmar produce exactamente un POST; sin overflow horizontal. Arranque con renderer generado y fixture IPC: acuse visible, retry enfoca encabezado, cancelar deshabilitado tras punto irreversible. No se ejecutan instalaciones ni proveedores.
- Capturas inspeccionadas: `/tmp/community-update-review-390.png`, `/tmp/community-permissions-1280.png`, `/tmp/community-loader-cancel-390.png`, `/tmp/community-reconnect-390.png`; existen también las variantes 390/1280 de cada superficie.
- El arnés Chrome `/tmp/community-entry-flow-qa.mjs` y `.ui-entry-flow-review.*` son QA local explícita, **no se incluyen en producto ni commit**.

## Límite de prueba nativa

En scratch DGX, el binario Tauri existente se recompiló con estos assets (`cargo build --locked`, PASS). Se intentó arrancar con XDG/perfil temporales y CLI de fixture que sólo emite etapas, sin Podman ni datos personales. Bajo Xvfb la ventana quedó `IsUnMapped`; no hubo captura visual nativa válida. Por tanto este lote **no acredita smoke gráfico nativo completo**, instalación real, actualización firmada de punta a punta ni publicación de imagen. No se cambió Rust ni se alteró el protocolo para forzar un resultado.

El servidor continúa siendo autoridad para resolver permisos, aceptar marcadores y ejecutar actualizaciones. La revisión UI congela lo observado, pero no añade un CAS/version-pin nuevo al backend. Una aprobación no se presenta como acción ejecutada; un marcador aceptado tampoco como instalación completada.

## Archivos exactos del lote

- `desktop/src/{index.html,main.ts,native-action.ts,native-action.test.ts,main-actions.test.ts,render.ts,render.test.ts,styles.css}`.
- Generados: `desktop/ui/{index.html,main.js,main.js.map,native-action.js,native-action.js.map,render.js,render.js.map,styles.css}`.
- `frontend/src/api/{client.ts,update-errors.test.ts}` (client: sólo dos getters de lectura).
- `frontend/src/components/{ApprovalCard.tsx,ApprovalCard.module.css,ApprovalCard.test.tsx,CompanionInstallAction.tsx,CompanionInstallAction.test.tsx,ReconnectScreen.tsx,ReconnectScreen.module.css,ReconnectScreen.test.tsx,SystemUpdateFooter.tsx,SystemUpdateFooter.module.css,SystemUpdateFooter.test.tsx}`.
- `frontend/src/hooks/useCompanionInstall.ts`.
- `frontend/src/lib/i18n.ts` (claves ES/EN del lote) y `frontend/src/styles.css` (overflow ConfirmDialog).
- Este informe. Sin cambios a App, backend, MFA, rutas de autoridad o actualización firmada.

## Pendiente real

QA final en binario empaquetado de Community con motor real y su ciclo de vida/selector del sistema; protocolo de actualización desktop firmado y release cuando esté disponible; recorridos integrales con cuentas/permisos reales autorizados. Este lote no declara terminada toda la UI ni reemplaza esas pruebas.
