# Inventario real UI Community — UI-01

Base actualizada: runtime `b741996` y lote paneles conectados, 2026-09-12. Inventario de código y evidencias por corte, no declaración de validación integral. UI-01 sigue **parcial**. Community es una aplicación nativa: una revisión de su React en navegador NO valida el binario Tauri ni permisos del sistema operativo.

Fuentes: `frontend/src/App.tsx`, `views/SectionHubs.tsx`, `views/TasksView.tsx`, `views/SeguridadView.tsx`, `components/Layout.tsx`, `hooks/useFeatures.ts`, `src/hermes/shell_server/instance/api.py`, `desktop/src/lifecycle.ts`, `desktop/src-tauri/src/window_policy.rs` y árboles QML de `src/hermes/lumen`. No aparece un tipo literal `ViewId` en frontend/desktop: el vocabulario efectivo es `_ALL_VIEWS` del servidor y las claves de los hubs.

## Rutas del producto React (basename `/app`)

| Ruta / superficie | Implementación real | Estado de revisión | Siguiente aceptación pendiente |
| --- | --- | --- | --- |
| `/chat`, navegación y compositor | Layout, ChatView, useChat, ChatDrafts | Parcial integrado; visual 1280×720, teclado/IME/adjuntos/carreras probados en bloque anterior | Tauri real, estrecho/zoom, modelo activo/errores y cierre del resto de estados |
| `/chat`: Contexto | ContextPanel, useContextSource | UI-01A implementado: fuentes independientes, error ≠ vacío, sin conectores inventados, latest-wins, reintento y teclado; CUA aislada | Recorrido con backend real y Tauri; móvil/zoom integral. Ver revision-context-panel-2026-09-11.md |
| `/chat`: permisos, notificaciones, freno | PendingApprovalsInChat, ApprovalCard, NotificationsPanel, KillSwitchBanner | Permisos/freno integrados por cortes previos; notificaciones verificadas en lote conectado: error≠vacío, read confirmado, foco y respuestas tardías | Recorrido real solicitud→resolución→notificación en Tauri; no inferir ejecución desde aprobación |
| `/tareas`, `/tareas?tab=programadas` | TasksView, CalendarView | Rediseño y readmodel backend integrados; ver revision-tasks-dashboard-backend y revision-factory-retirement. Office/Swarm/pixel retirados | Recorrido delegado extremo a extremo en binario; calendario con programación real y recuperación |
| `/capacidades?tab=skills` | SkillsView | Corte config-ui integrado: búsqueda/fallo/scan failclosed, sin stagger decorativo y responsive | Instalación/actualización/eliminación real con imagen y aprobaciones; no basta fixture visual |
| `/capacidades?tab=integraciones` | IntegrationsView | Compactado, catálogo buscable, refresh latest-wins, errores de catálogo/conexión distintos, acciones bloqueadas con conexión desconocida; tests DOM | OAuth real en Tauri, popup bloqueado y flujos del proveedor; no probado con cuentas reales |
| `/capacidades?tab=mcp` | McpView | Aprobación exacta y config-ui: errores/búsqueda/scan cerrado, filas adaptables | Conexión/desconexión real y diagnóstico desde Tauri |
| `/capacidades?tab=en-vivo` | EnVivoView | Pendiente | VNC real, desconexiones, fullscreen y teclado |
| `/sistema?tab=seguridad` | SeguridadView | Seguridad/backend parcialmente probado; no revisión visual integral | Freno, aprobaciones, delegaciones entrantes, gobierno, egress, tailnet, hosts SSH y SecurityCenter |
| `/sistema?tab=coste` | UsageView | Config-ui integrado: error por fuente distinto de cero, serie tokens correcta, responsive 2×2 | Telemetría real de una sesión y conciliación de costes; no afirmar precios completos |
| `/sistema?tab=proveedores` | ProvidersView | Config-ui integrado: OAuth con singleflight/generaciones, errores visibles y activación tras prueba | Opener/callback OAuth en Tauri; herencia LLM gestionada depende de su gate, no queda habilitada por esta UI |
| `/sistema?tab=memoria` | MemoriaView | Memory-files integrado: detalle completo antes de editar, latest-wins, guardar/eliminar ligados a selección, filas compactas | Memoria real en binario y selector/gestos del SO |
| `/sistema?tab=archivos` | ArchivosView | Memory-files integrado: preview HTTP403 distinto de contenido, cancelación/selección tardía, upload respeta carpeta | Archivos reales y selector nativo; no confundir con Files Enterprise cloud |
| `/anuncios` | AdsView, useAdsAvailability/useAdsPanel, puente del companion | Lote conectado: espacio útil completo, carga de documento, retry explícito, unavailable sin arranque inventado, poll serial y revocación desmonta iframe | Companion y puente reales en Tauri; QA iframe de este corte sólo documento ficticio, no toda la UI Ads ni cuentas reales |
| Antes del router: reconexión | App auth gate → ReconnectScreen | Pruebas de cero peticiones sin sesión y refresh único integradas | Recorrido Tauri tras caducidad y recuperación real |

Advertencia: el contrato backend de features conserva vocabulario legacy; el router actual ya no presenta el catálogo de especialistas. Tareas tiene entrada propia. `en-vivo` se permite expresamente en Community en useFeatures. Anuncios siempre tiene entrada, aunque el companion no esté disponible. Inventariar la ruta no implica que esté habilitada para todos los usuarios.

## Alias y entradas sin vista propia

| Entrada | Destino |
| --- | --- |
| `/`, rutas desconocidas (incluido `/tablero`) | `/chat` |
| `/office`, `/agentes` | `/tareas` |
| `/skills`, `/integraciones`, `/mcp`, `/en-vivo` | Pestaña homónima en `/capacidades` |
| `/ensenar` | `/capacidades?tab=en-vivo` |
| `/seguridad`, `/coste`, `/proveedores`, `/memoria`, `/archivos` | Pestaña homónima en `/sistema` |
| `/programadas` | `/tareas?tab=programadas` |
| `/ajustes` | `/sistema` |

Los hubs eligen la primera pestaña permitida si falta `tab` o no es válida; vuelven a chat si ninguna está permitida. No son rutas extra ocultas.

## Aplicación nativa y superficies adicionales

| Superficie | Fuente real | Estado |
| --- | --- | --- |
| Preparación/instalación, fallo/reintento/cancelación, diagnóstico, reconexión, listo | `desktop/src/index.html`, render.ts, lifecycle.ts; salida generada en desktop/ui | UI-STATES recoge diagnóstico allowlist con selector nativo, replay bootstrap y cancel/retry; hubo smoke binario aislado. Sigue pendiente instalación end-to-end en destino con imagen final |
| Actualización desktop frente a motor | `desktop/UPDATER-REVIEW-2026-09-11.md`, SystemUpdateFooter | Estado desktop honesto: contrato de actualización no disponible; no se usa VERSION sin firma como éxito | Manifiesto/firma/instalador verificados e imagen final; no presentar updater daemon como desktop actualizado |
| Ventana única, tray, arranque/restauración hacia el producto | `desktop/src-tauri/src/main.rs`, window_policy.rs | Pendiente de build/instalación Tauri y validación en destino. El frontend del producto se carga desde el motor; no asumir que modificar desktop/ui rediseña las vistas React |
| QML compositor: FirstBootWizard, LoginScreen, Desktop | `src/hermes/lumen/compositor/qml/desktop/main.qml` y componentes asociados | Superficies existentes adicionales; determinar uso en imagen final antes de declarar legado eliminable |
| QML aplicaciones: Agents, Skills, Tasks, Integrations, FileManager, SecurityCenter, Terminal, Apps, Providers, Mcp, Settings | Archivos `*App.qml` del compositor | Pendiente: no están validados por los tests del frontend React |
| QML chat y shell previo | `src/hermes/lumen/apps/chat/ChatAppWindow.qml`, `src/hermes/lumen/qml/Main.qml`, ChatView.qml | Pendiente de determinar alcance/distribución y migración; no se han eliminado ni equiparado con Tauri |
| Lanzador de aplicaciones del escritorio | NativeAppsLauncher.qml | Pendiente; son aplicaciones del SO y no rutas del router React |
| Confirmación genérica y paneles laterales | ConfirmDialog, ui/Drawer | Tests de promesas descartadas, unmount, Escape, nombre/foco y retorno; Drawer usa Base UI existente sin trap global | Recorrido combinado de diálogos anidados y lector de pantalla en Tauri |
| Permisos y diálogos del sistema operativo | Tauri/Rust, selector de archivos, navegador OAuth, llavero, notificaciones y controles de captura | No cubiertos por fixtures HTML | Probar en instalación nativa con aceptar/rechazar, cancelación y recuperación; no fabricar pantallas para sustituirlos |

## Cierre de UI-01

Ninguna fila pendiente pasa a validada por un build global o por probar solamente shell/chat. Registrar por bloque: código, prueba de fallo/carga/éxito, teclado/foco, tamaños, evidencia visual y ejecución en el destino correspondiente. Informe del bloque ya integrado: `revision-community-shell-chat-2026-09-11.md`.
