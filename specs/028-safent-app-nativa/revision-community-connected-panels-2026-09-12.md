# Community: paneles conectados y repaso de superficies

Lote acotado sobre runtime `b741996`. No rediseña otra vez chat, tareas, configuración ni permisos ya trabajados. Actualiza el inventario real de rutas con los informes acumulados y cierra dos brechas visibles: notificaciones de la shell y contenedor de Anuncios. Community continúa siendo app nativa; la evidencia Chrome siguiente sólo verifica su frontend.

## Diseño — skill Emil leída completa

| Before | After | Why |
| --- | --- | --- |
| Notificaciones posicionadas manualmente a 340 px, sin ajustar al borde del viewport ni gestionar foco al abrir. | Popover Base UI existente, anclado con colisiones, ancho limitado, scroll interno y botón de cierre enfocado. | Lectura y teclado consistentes tanto en escritorio como en ventana estrecha; sin otra implementación manual de foco. |
| HTTP fallido se convertía en bandeja vacía o contador cero. | Error recuperable y contador desconocido; vacío sólo tras respuesta válida. | No afirmar ausencia de avisos sin haber podido consultarlos. |
| Aviso se marcaba leído antes de confirmar el POST, con rechazo silenciado. | Cambio visual tras confirmación; fallo conserva último estado y pide comprobarlo. | No confundir una intención con un cambio confirmado. Una respuesta perdida puede exigir refrescar. |
| Cerrar/reabrir aceptaba respuestas del panel anterior; un POST tardío podía abrir un hilo después de cerrar. | Generaciones de lectura y ámbito de apertura, guardia sincrónica de escritura y descarte al desmontar/cerrar. | El resultado anterior no toma el control de la navegación nueva. |
| Anuncios ocupaba 78vh con mínimo 420 px dentro de otro scroll y una cabecera grande duplicada. | Barra compacta y documento ocupando el espacio restante de la app, sin mínimo fijo. | Más espacio para el producto conectado y sin doble contenedor de desplazamiento. |
| `unreachable` decía “está arrancando” con icono animado. | Conexión no disponible, motivo incierto explícito y reintento; icono estático. | Un fallo de conexión no demuestra que el servicio esté arrancando. |
| Cualquier poll podía solaparse y una respuesta vieja sobrescribir el estado actual. | Poll serial, reintento bloqueado mientras está en curso y efecto anterior invalidado. | No volver a mostrar un iframe por una disponibilidad obsoleta. |
| Estilos globales de la bandeja antigua quedaban sin consumidor. | Se retiraron 126 líneas legacy; CSS local del popover. | Evitar estilos muertos y colisiones sin tocar tokens globales. |

No se añadieron animaciones a navegación, apertura por teclado ni estados de error. La comprobación en curso mantiene el indicador que comunica trabajo real. Textos nuevos ES/EN.

## Evidencia

Pruebas focales del lote y consumidores de shell/companion: **54 PASS, 8 archivos**. Incluyen HTTP503 preservado por los getters, error≠vacío, lectura rechazada/duplicada, bulk fallido, hilo inaccesible, close/reopen con respuesta antigua, cierre durante POST, Escape, iframe sin query/secretos, recarga explícita, desmontaje por autorización, poll sin solapamiento y efecto previo descartado.

```sh
cd /tmp/safent-takeover.Ys0aPw/runtime/frontend
NODE_OPTIONS=--no-experimental-webstorage npm test -- src/components/NotificationsPanel.test.tsx src/views/AdsView.test.tsx src/hooks/useAdsAvailability.test.tsx src/api/notification-errors.test.ts src/components/Layout.test.tsx src/App.test.tsx src/hooks/useCompanionInstall.test.tsx src/components/CompanionInstallAction.test.tsx
NODE_OPTIONS=--no-experimental-webstorage npm run build
```

TypeScript y Vite PASS en la pasada final del lote. Se mantiene warning de bundle principal >500 kB (886.34 kB); no se cambió estrategia global de bundles. No se ejecutó una suite completa por cada modificación ni se atribuye aquí una validación global.

Chrome propio, headless, 1280×800 y 390×800 con reduced-motion: PASS. Panel de notificaciones poblado dentro del viewport, foco de cierre y Escape devuelven a la campana; POST de lectura503 conserva aviso/error visible; Anuncios usa altura restante (>650 px en el fixture), no crea iframe con servicio inaccesible. Se inspeccionaron las capturas finales de lista y error.

- `/tmp/community-connected-qa/result.json`.
- `/tmp/community-connected-qa/notifications-390.png`.
- `/tmp/community-connected-qa/notification-error-390.png`.
- `/tmp/community-connected-qa/ads-1280.png`.
- `/tmp/community-connected-qa/ads-unavailable-390.png`.

Fixture temporal `.ui-connected-review.html/.tsx`, runner `/tmp/community-connected-qa.mjs`, Vite propio loopback5241. Datos ficticios y HTTP controlado; documento iframe rotulado QA, sin Google/Meta ni credenciales reales. No incluir fixtures, capturas ni runner temporal en commit.

## Límites explícitos

- `iframe.onload` indica que cargó un documento, NO salud funcional del panel, HTTP200, cuenta conectada ni permiso de operar. El bridge sigue siendo la fuente de disponibilidad y no se inventó otro protocolo de mensajes.
- Los getters usan el transporte común actual, que controla su propio timeout. Este lote invalida respuestas/callbacks; no promete abortar físicamente un POST ya enviado ni revocar sus efectos. No se amplió ese transporte global.
- Abrir un aviso no aprueba una acción. No se modifican jaula, aprobación, MFA, política Enterprise ni selección de agente.
- Se revisaron rutas/alias de App y SectionHubs y se contrastaron informes existentes. En vivo/VNC, algunas subsecciones de Seguridad, recorridos OAuth/selector/permiso del SO y la app Tauri con imagen final siguen pendientes del recorrido nativo integral.
- El contenedor Anuncios no equivale a verificar toda la UI del companion. Falta recorrido completo con companion/bridge reales, cuentas y permisos; no se afirma haber habilitado Ads administrado ni ejecución automática.

## Archivos

Producción: `frontend/src/components/NotificationsPanel.tsx` y `.module.css`; `frontend/src/views/AdsView.tsx` y `.module.css`; `frontend/src/hooks/useAdsAvailability.ts`; `frontend/src/api/client.ts` exclusivamente dos getters de notificaciones; claves pertinentes `frontend/src/lib/i18n.ts`; retiro de CSS antiguo en `frontend/src/styles.css`.

Pruebas: `NotificationsPanel.test.tsx`, `AdsView.test.tsx`, `useAdsAvailability.test.tsx`, `api/notification-errors.test.ts`. Documentación: este informe e `inventario-ui-community.md` actualizado. No cambios desktop/Rust/backend, rutas, chat, aprobaciones ni Tasks.
