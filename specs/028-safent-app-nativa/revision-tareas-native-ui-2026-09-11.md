# Community — Tareas, navegación estrecha y arranque nativo

2026-09-11. Checkpoint UI, **no cierre integral de UI ni del producto**.

## Implementación

| Antes | Después | Por qué (skill Emil) |
| --- | --- | --- |
| Office/Swarm, departamentos ficticios y motor pixel/3D | Tareas, lista compacta y detalle, origen/remitente/resultado/conversación | Jerarquía de trabajo real; eliminar decoración sin función |
| Programadas dentro de Agentes | Pestaña Programadas en Tareas, reutiliza CalendarView | Una ubicación estable, sin duplicar motor |
| Sidebar ocupa media ventana estrecha después de redimensionar | Colapso al entrar en ≤700px; navegación completa al abrir, cierre al navegar/Escape | Contenido legible, navegación inmediata sin animar anchura |
| Arranque nativo con logo flotante, gradientes y sombras grandes | Marca discreta, tipografía y controles neutrales compactos, progreso de 3px | Movimiento sólo para progreso/feedback; no decoración continua |
| Decisión de encargo acepta cualquier respuesta HTTP exitosa | Requiere `ok:true`, guard síncrono contra doble envío | El feedback debe reflejar un resultado confirmado |

Retirados OfficeView, SwarmView, agentRoster UI y motor office-live, junto a 47
paquetes transitivos/directos 3D ya sin consumidores. Todo recuperable mediante
Git. **Backend de roster/seed y migración de perfiles antiguos aún pendientes**.
No se borraron conversaciones, datos o agentes personalizados de usuario.

Tareas valida DTO antes de mostrarlo; servicio ausente/error no se convierte en
lista vacía. Conserva último snapshot, bloquea acciones cuando falla la lectura,
no inventa autoría ni resultados ni aprobaciones vinculadas. El chat se abre con
el contexto existente. No hay nuevo motor de conversación ni ejecución automática.

## Contrato UI pendiente de implementación backend

`GET /api/v1/tasks/dashboard?limit=100` NO existe todavía. DTO definido en
`frontend/src/api/types.ts`, consumo en `TasksView.tsx`. Campos:
`available:true`, `tasks`, `has_more`; cada tarea lleva `task_id`, `label`,
`status`, `source`; opcionales string/null: remitente, fechas, conversación,
resultado; `approval_ids` opcional array de strings. Ausencia de `approval_ids`
significa vínculo desconocido, no cero aprobaciones. `has_more` no es un total.

Inbox usa ruta existente `/inbound-delegations` sin fallback local a `[]`.
La ruta backend todavía puede esconder fallos internamente: debe devolver 503
cuando no se puede consultar. La admisión local no debe interpretarse como
aprobación automática de las acciones sensibles de la tarea.

Pendiente de lógica: telemetría durable a Enterprise, rechazo y cancelación,
distinción ACK/ejecución, reconciliación después de reinicio, vínculos exactos
de conversación/propuesta, paginación backend y permisos. Véase plan único EE.
Pendiente UI: localización de nuevos textos Tareas al inglés, búsqueda histórica
cuando exista paginación y revisión del flujo con datos reales backend. No
presentar fixtures como prueba end-to-end ni el estado «no disponible» como
funcionalidad entregada.

## Evidencia

- Frontend: 190 PASS / 36 archivos y build TypeScript/Vite PASS en snapshot del
  primer corte (antes de los cambios posteriores de Integrations/diálogos).
- Native desktop: 81 PASS / 4 archivos; typecheck y build PASS. Los assets
  `desktop/ui` se regeneraron desde `desktop/src`, como exige build.mjs.
- Corte posterior nativo: **83 PASS / 5 archivos**, añade guard single-flight
  y feedback visible de fallos IPC; suscripciones capturan rechazo, retry fallido
  no queda girando ni pisa eventos recientes. Exportar diagnóstico deshabilitado
  explícitamente porque no existe comando Rust: pendiente implementar selector
  nativo/CLI/exportación protegida antes de habilitarlo, no simular éxito.
- Playwright Chrome, fixture aislada: 1440×960 y 390×844, detalle poblado,
  resize, abrir sidebar, main oculto mientras navegación abierta, Escape cierra.
  Sin pageerrors ni overflow horizontal en la ronda final de Tareas.
- Arranque nativo HTML/CSS claro/oscuro a 860×640; reduced-motion devuelve
  animation-name:none. Bootstrap Tauri omitido en fixture; NO prueba instalación
  del binario, elevación OS ni arranque real de daemon. Tests renderer separados.
- Capturas de trabajo `/tmp/safent-community-tasks-{desktop,mobile}.png`,
  `/tmp/safent-native-start-{dark,light}.png` son efímeras y usan datos ficticios.
- Avisos pendientes: frontend chunk principal ~986kB, npm audit 12 avisos en
  frontend y 2 moderados desktop. No ejecutar audit fix --force sin revisar
  actualizaciones/contratos. Estos avisos impiden declarar auditoría cerrada.

```sh
cd frontend
NODE_OPTIONS=--no-experimental-webstorage npm test
NODE_OPTIONS=--no-experimental-webstorage npm run build
cd ../desktop
npm ci --ignore-scripts
NODE_OPTIONS=--no-experimental-webstorage npm test
npm run typecheck
npm run build
```

La nueva prioridad del usuario sigue siendo **TODA UI** Community/Enterprise/Ads,
no sólo este bloque. Los otros informes detallan sus propios alcances y faltantes.
