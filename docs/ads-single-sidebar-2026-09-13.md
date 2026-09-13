# Anuncios local: un único sidebar

Base: Runtime `23c44ef`, rama `fix/safent-review-20260911`. Cambio sólo React; sin versiones, backend, repo Ads, artefactos firmados ni publicación.

| Before | After | Why |
| --- | --- | --- |
| Sidebar global junto al sidebar propio del panel local | El panel local ocupa todo el ancho; el sidebar global queda oculto, no desmontado | Una sola navegación de producto y conservación del estado de Safent |
| Salir exige usar la navegación global que resta espacio | «Volver a Safent» en el toolbar exterior, siempre fuera del iframe | Retorno visible y accesible incluso mientras carga o falla el documento |
| Ningún traspaso de foco al entrar/salir | Foco al retorno al entrar; al enlace Anuncios o al control visible de abrir sidebar al salir | Teclado usable sin animación ni abrir un drawer por sorpresa |

## Alcance

- Sólo `AdsPanel` local reclama el espacio mediante un contexto de presentación. No se decide con el polling independiente del Layout: el iframe realmente montado es la señal.
- `ready` y `no_accounts` muestran el panel. Carga, managed, no instalado y pérdida de autorización conservan/restauran el shell habitual. La política y las acciones de esos estados no cambian.
- Retorno a la última ruta principal con query/hash; entrada directa a `/anuncios` o `/anuncios/` vuelve a `/chat`. No se usa `history.back()` para el botón, porque puede recorrer historial del iframe o salir de Safent.
- Sidebar, recientes y borradores siguen montados. No se resetea conversación; el estado abierto/cerrado de escritorio se conserva. En móvil se mantiene el cierre habitual del drawer al navegar.
- El freno de emergencia global sigue visible. Se elimina también el botón flotante de abrir el sidebar global dentro de Anuncios.
- El iframe sigue en `/ads/`, sin cambios de cookies, mensajes, sandbox, navegación ni lectura/modificación del DOM del panel. La navegación interna móvil pertenece a Ads, no se duplica aquí.
- Guía `emil-design-eng` aplicada: navegación inmediata, jerarquía compacta, componente Button existente, foco visible y sin movimiento añadido. Copy ES/EN local a la vista para no ampliar el cambio de catálogos globales.

## Evidencia

Snapshot de QA: `/tmp/safent-sidebar-23c44ef.NNKIYs/frontend` en DGX, creado con `git archive 23c44ef frontend` más los seis archivos UI/test de este corte; `npm ci --no-audit --no-fund` con lock real, Node 24.13.1. Canonical node_modules intacto.

- Rojo: ocho casos nuevos fallan contra Layout/AdsView originales de `23c44ef` (`/tmp/safent-sidebar-red.log`).
- Focal inicial: **25 PASS**, Layout/AdsView/ChatDraftIsolation, antes del caso adicional de slash final (`/tmp/safent-sidebar-focal.log`).
- Final: **373 PASS, 56 archivos**, sin fallos ni exclusiones, **4.14 s** (`/tmp/safent-sidebar-full-final.log`). Incluye nueve casos nuevos: ocultación sin remontaje, retorno ruta/query y foco, collapsed, móvil directo con/sin slash, router Back, loading/managed/unavailable, no_accounts.
- `npm run build`: **PASS** (`tsc --noEmit` + Vite, **2.74 s** Vite; `/tmp/safent-sidebar-build-final.log`).
- `git diff --check`: PASS. La suite conserva avisos históricos de `act` en otros tests; no se presenta como una suite sin warnings.

Comandos desde el snapshot: `PATH=/home/luiscorrea-dev/.nvm/versions/node/v24.13.1/bin:$PATH NODE_OPTIONS=--no-experimental-webstorage npm test` y `npm run build` con el mismo entorno.

## Límites de aceptación

Los tests de navegación usan Router/DOM reales y disponibilidad simulada; no simulan cuentas conectadas ni certifican OAuth. No se ha modificado ni ejecutado una app firmada para este cambio. La revisión visual en preview aislado y aceptación del futuro binario pertenecen al cierre del principal; no deben confundirse con estos tests de DOM.

Para preview, reutilizar el snapshot y una fixture de API/iframe explícita. El Vite dev config del repo tiene proxy `/api` a `127.0.0.1:17517`: no apuntarlo a un servicio real ni interpretar el preview sin fixture como una sesión Ads operativa. No se incluye ningún `.ui-*` en el commit.
