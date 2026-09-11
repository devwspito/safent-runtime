# Inventario real UI Community — UI-01

Base inspeccionada: runtime `b699483`, 2026-09-11. Inventario de código, no declaración de validación funcional. UI-01 sigue **parcial**; UI-01A (panel de contexto) implementado y probado en frontend, sin validación de instalación nativa.

Fuentes: `frontend/src/App.tsx`, `views/SectionHubs.tsx`, `views/OfficeView.tsx`, `views/SeguridadView.tsx`, `components/Layout.tsx`, `hooks/useFeatures.ts`, `src/hermes/shell_server/instance/api.py`, `desktop/src/lifecycle.ts`, `desktop/src-tauri/src/window_policy.rs` y árboles QML de `src/hermes/lumen`. No aparece un tipo literal `ViewId` en frontend/desktop: el vocabulario efectivo es `_ALL_VIEWS` del servidor y las claves de los hubs.

## Rutas del producto React (basename `/app`)

| Ruta / superficie | Implementación real | Estado de revisión | Siguiente aceptación pendiente |
| --- | --- | --- | --- |
| `/chat`, navegación y compositor | Layout, ChatView, useChat, ChatDrafts | Parcial integrado; visual 1280×720, teclado/IME/adjuntos/carreras probados en bloque anterior | Tauri real, estrecho/zoom, modelo activo/errores y cierre del resto de estados |
| `/chat`: Contexto | ContextPanel, useContextSource | UI-01A implementado: fuentes independientes, error ≠ vacío, sin conectores inventados, latest-wins, reintento y teclado; CUA aislada | Recorrido con backend real y Tauri; móvil/zoom integral. Ver revision-context-panel-2026-09-11.md |
| `/chat`: permisos, notificaciones, freno | PendingApprovalsInChat, ApprovalCard, NotificationsPanel, KillSwitchBanner | Permisos/freno parcialmente integrados con pruebas; notificaciones no auditadas integralmente | Recorrido real de solicitud y resolución; todas las variantes de error/foco |
| `/agentes?tab=enjambre` | OfficeView → SwarmView | Pendiente de recorrido integral | Estados reales, navegación, accesibilidad y coste del canvas/bundle |
| `/agentes?tab=tarjetas` | OfficeView → TarjetasView | Pendiente | Altas/ediciones, densidad, errores y controles |
| `/agentes?tab=live` | OfficeView → OfficeCanvas | Pendiente | Observación real, pausa/desconexión y rendimiento |
| `/agentes?tab=tareas` | OfficeView → CalendarView | Pendiente | Calendario, formularios, fechas, errores y permisos |
| `/capacidades?tab=skills` | SkillsView | Cambios previos de aprobación integrados; UI integral pendiente | Instalación/actualización/eliminación, selección y estados |
| `/capacidades?tab=integraciones` | IntegrationsView | Pendiente | Conectores, OAuth, fallo/reintento y permisos reales |
| `/capacidades?tab=mcp` | McpView | Aprobación exacta integrada; UI integral pendiente | Conectar/desconectar y diagnósticos desde UI real |
| `/capacidades?tab=en-vivo` | EnVivoView | Pendiente | VNC real, desconexiones, fullscreen y teclado |
| `/sistema?tab=seguridad` | SeguridadView | Seguridad/backend parcialmente probado; no revisión visual integral | Freno, aprobaciones, delegaciones entrantes, gobierno, egress, tailnet, hosts SSH y SecurityCenter |
| `/sistema?tab=coste` | UsageView | Pendiente | Datos reales, vacíos/errores, filtros, tablas/gráficos y legibilidad |
| `/sistema?tab=proveedores` | ProvidersView | Pendiente | Conexión/activación, errores, credenciales y modelo gestionado |
| `/sistema?tab=memoria` | MemoriaView | Pendiente | Lista/detalle, búsquedas, cambios y errores |
| `/sistema?tab=archivos` | ArchivosView | Pendiente | Navegación, previsualización, subidas/descargas y fallos reales |
| `/anuncios` | AdsView, useAdsAvailability/useAdsPanel, puente del companion | Parcial; Ads es producto conectado y su UI tiene inventario separado | Todas las disponibilidades, aislamiento de cuenta y acciones del panel Ads |
| Antes del router: reconexión | App auth gate → ReconnectScreen | Pruebas de cero peticiones sin sesión y refresh único integradas | Recorrido Tauri tras caducidad y recuperación real |

Advertencia de disponibilidad: `_ALL_VIEWS` contiene chat/programadas/agentes/skills/integraciones/mcp/archivos/proveedores/seguridad/memoria/coste. Community excluye actualmente **agentes**, aunque la ruta existe para instancias asociadas; `/programadas` redirige a una ruta bajo ese guard. `en-vivo` se permite expresamente en Community en useFeatures. Anuncios siempre tiene entrada, aunque el companion no esté disponible. Inventariar la ruta no implica que esté habilitada para todos los usuarios.

## Alias y entradas sin vista propia

| Entrada | Destino |
| --- | --- |
| `/`, rutas desconocidas (incluido `/tablero`) | `/chat` |
| `/office` | `/agentes` |
| `/skills`, `/integraciones`, `/mcp`, `/en-vivo` | Pestaña homónima en `/capacidades` |
| `/ensenar` | `/capacidades?tab=en-vivo` |
| `/seguridad`, `/coste`, `/proveedores`, `/memoria`, `/archivos` | Pestaña homónima en `/sistema` |
| `/programadas` | `/agentes?tab=tareas` |
| `/ajustes` | `/sistema` |

Los hubs eligen la primera pestaña permitida si falta `tab` o no es válida; vuelven a chat si ninguna está permitida. No son rutas extra ocultas.

## Aplicación nativa y superficies adicionales

| Superficie | Fuente real | Estado |
| --- | --- | --- |
| Preparación/instalación, fallo con reintento/copia de diagnóstico, reconexión, listo | `desktop/src/index.html`, render.ts, lifecycle.ts; salida generada en desktop/ui | Pendiente de revisión premium y recorrido nativo. UiState usa preparing/failed/reconnecting/ready; no es otro router de negocio |
| Ventana única, tray, arranque/restauración hacia el producto | `desktop/src-tauri/src/main.rs`, window_policy.rs | Pendiente de build/instalación Tauri y validación en destino. El frontend del producto se carga desde el motor; no asumir que modificar desktop/ui rediseña las vistas React |
| QML compositor: FirstBootWizard, LoginScreen, Desktop | `src/hermes/lumen/compositor/qml/desktop/main.qml` y componentes asociados | Superficies existentes adicionales; determinar uso en imagen final antes de declarar legado eliminable |
| QML aplicaciones: Agents, Skills, Tasks, Integrations, FileManager, SecurityCenter, Terminal, Apps, Providers, Mcp, Settings | Archivos `*App.qml` del compositor | Pendiente: no están validados por los tests del frontend React |
| QML chat y shell previo | `src/hermes/lumen/apps/chat/ChatAppWindow.qml`, `src/hermes/lumen/qml/Main.qml`, ChatView.qml | Pendiente de determinar alcance/distribución y migración; no se han eliminado ni equiparado con Tauri |
| Lanzador de aplicaciones del escritorio | NativeAppsLauncher.qml | Pendiente; son aplicaciones del SO y no rutas del router React |

## Cierre de UI-01

Ninguna fila pendiente pasa a validada por un build global o por probar solamente shell/chat. Registrar por bloque: código, prueba de fallo/carga/éxito, teclado/foco, tamaños, evidencia visual y ejecución en el destino correspondiente. Informe del bloque ya integrado: `revision-community-shell-chat-2026-09-11.md`.
