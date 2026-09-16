# UI-01A — Contexto Community

Base `b699483`, 2026-09-11. Bloque acotado del plan UI-01; no cierre de toda la UI ni de Community. Skill aplicada: **emil-design-eng** (`/Users/luiscorrea/.codex/skills/emil-design-eng/SKILL.md`), leída completa. Su criterio de frecuencia elimina movimiento ornamental al plegar secciones; prioriza densidad, texto legible, foco y feedback verificable.

| Before | After | Why |
| --- | --- | --- |
| Catch del cliente convertía error de archivos en `[]` | El cliente propaga el error; cada fuente conserva su propio estado y reintento | No presentar una fuente inaccesible como vacía |
| Composio fallaba silenciosamente y siempre añadía Búsqueda web | Solo conectores devueltos por la API; error explícito, sin capacidad inventada | Disponibilidad verificable, sin prometer permisos o herramientas inexistentes |
| Refresh sin identidad de petición; podía pisar datos recientes | Hook latest-request-wins, limpieza al desmontar, polling sin solapamiento y refresh al finalizar trabajo | Evitar respuestas obsoletas y carreras |
| Panel sobrevivía al cambio de conversación sin cambiar identidad | Remontaje con `draft.key` de conversación + agente | Desechar solicitudes de la instancia anterior; no cruzar estado de contexto entre hilos |
| Descarga incluso de directorios, texto recortado en JS | Directorios sin enlace de descarga; nombre completo en DOM/título y elipsis CSS | Acciones correctas y nombres accesibles |
| Vacíos grandes, sin recuperación ni retorno de foco | Filas compactas, recuentos desconocidos `—`, última lista marcada sin verificar, Escape y retorno al activador | Mejor densidad sin ocultar incertidumbre |

## Contratos y alcance

- Archivos, habilidades y conexiones son inventario de **instancia**, no selección del mensaje. El panel lo aclara; no adjunta ni autoriza recursos automáticamente.
- No se ha inventado `conversation_id` para endpoints que no lo soportan. El aislamiento de borradores/adjuntos continúa en ChatDrafts; la única conexión aquí es `key={draft.key}` en ChatView.
- `listWorkspaceFiles` ya no oculta fallos. Consumidores revisados: ArchivosView tiene try/catch visible; folderBridge propaga a `ChatView.syncBridge`, que captura el error y no anuncia éxito. No se han reescrito esas vistas ni el backend.
- Listas inválidas o filas incompatibles se presentan como fuente no verificable, no como vacío.
- No hay mutaciones nuevas, OAuth nuevo, selección de cuenta, cambios MFA ni modificación de permisos.

## Evidencia

- Verificación final: `NODE_OPTIONS=--no-experimental-webstorage npm test` → **36 archivos / 183 tests PASS**, exit 0; `npm run build` con la misma opción → **tsc + Vite PASS**, exit 0. `git diff --check` limpio. JSDOM avisa de scrollTo no implementado en pruebas existentes; Vite mantiene warnings de tamaño (main 987,16 kB y SwarmView 1.415,23 kB).
- Pruebas DOM contra **cliente HTTP real** con fetch simulado (no mock de api/client): error 503 independiente, retry solo de la fuente fallida, éxito, lista previa no verificada, ruta de descarga codificada, directorio sin descarga, respuesta inválida, foco y Escape. Autenticación aislada con token de prueba.
- Primer recorrido de regresión antes del cambio: 3 tests fallaban (estado vacío/fuente inventada, descarga y foco); ajustado el fixture de autenticación para ejercer el wrapper HTTP real en las verificaciones finales.
- Hook: respuesta antigua fallida tras éxito reciente, polling no solapado, payload inválido, remount de hilo con respuesta tardía y StrictMode.
- CUA: fixture local `.ui-community-review.*`, montando **Layout y ChatView reales** con todas las peticiones interceptadas; banda visible de datos de ejemplo. Capturas revisadas de panel abierto, fuente fallida junto a archivos/habilidad disponibles, sección plegada con Enter, cierre con Escape devolviendo foco al activador. Revisión ancha y estrecha con sidebar plegada; viewport estrecho medido en DOM **777×888 CSS px**, sin overflow horizontal. Los overrides nominales difieren por zoom del navegador; no se presentan como medidas CSS verificadas. Override restaurado al terminar.
- La fixture es deliberadamente **no commiteable** y no demuestra una conexión real con cuentas ni backend de producción.

## Archivos

ContextPanel.tsx / ContextPanel.module.css / ContextPanel.test.tsx; useContextSource.ts / useContextSource.test.tsx; api/client.ts (solo propagación listWorkspaceFiles); lib/i18n.ts (ES/EN); ChatView.tsx (solo key); inventario-ui-community.md; este informe.

## Pendientes fuera del bloque

El inventario enumera las rutas y superficies reales sin darlas por validadas: shell/chat sigue parcial; notificaciones, modelo activo, hubs, gestión de agentes/tareas, integración de Anuncios, arranque Tauri y QML requieren sus propios recorridos. Falta probar este panel en la instalación nativa y contra servicios reales. No se afirma cobertura de móvil o zoom integral. Persisten warnings de chunks grandes de main y SwarmView; no se han ocultado ni resuelto en UI-01A.
