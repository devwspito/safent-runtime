# UI-CHAT — interrupción y estado real

Fecha: 2026-09-12. Lote acotado del renderer Community; no backend, permisos nuevos, publicación ni cambio de motor. No equivale a certificar toda la aplicación nativa.

## Inventario y criterio Emil

Se conservan shell, diseño del chat, aprobaciones y aislamiento de borradores existentes. Skill `emil-design-eng` releída completa: feedback inmediato y foco estable, sin animaciones nuevas para acciones de teclado.

| Before | After | Why |
| --- | --- | --- |
| Detener cerraba sólo SSE y presentaba la respuesta parcial como finalizada | `cancelTask(task_id)` real, single-flight; solicitud/acuse/error separados del final | Desconectar una vista no cancela el trabajo del daemon |
| Detener durante enqueue borraba la futura respuesta, sin conocer su tarea | Control accesible pero no activable hasta recibir `task_id` | No afirmar una cancelación imposible de verificar |
| Error SSE cerrado se ocultaba con «Trabajando» | Aviso explícito de recepción interrumpida; mantiene handle, espejo y posibilidad de cancelar | El puente también produce error por fallo de conexión, no sólo por tarea fallida |
| Actividad global de otra tarea eliminaba «Reconectando» | Sólo actividad del task actual cambia su texto; únicamente un frame recibido confirma reconexión | No atribuir señales de otro chat |
| Cualquier HTTP 409 mostraba «Conecta un modelo» | Se preserva `ApiError.code`; sólo código real conocido recibe copia específica; un conflicto desconocido sigue siendo error | HTTP 409 no implica falta de proveedor |
| Un estado ausente/desconocido del espejo se trataba como final | Sólo `status=complete` o frame `done` finalizan; estado desconocido permanece pendiente también al restaurar | No confundir falta de datos con éxito |
| Un batch de streaming encolado podía revivir el spinner tras adoptar el final | El final del espejo descarta el batch previo | Mantener orden terminal de eventos |
| Foco del botón podía perderse al finalizar | Se conserva durante cancelación pendiente y vuelve al borrador sólo si seguía allí | No robar foco ni enviar el borrador siguiente |
| Un fallo de transporte decía «No se envió. Inténtalo de nuevo» | «Envío sin confirmar» y revisar Tareas antes de repetir | Una respuesta perdida no prueba que el servidor no aceptara el envío |

## Contrato comprobado

- `shell_server/main.py`: POST `/api/v1/tasks/{task_id}/cancel` delega cancelación cooperativa. `{ok:true, requested:true}` no afirma terminalidad ni revierte herramientas ejecutadas.
- `shell_server/cowork/chat_stream.py`: el puente puede emitir `kind:error` por conexión fallida o stream no disponible. Se conserva la tarea; no se reenvía el POST original.
- `api/client.ts`: `EventSource.onerror` reconecta automáticamente; un frame `error` cierra esa recepción. El renderer distingue esos caminos sin añadir transporte paralelo.
- `sqlite_conversation_repo.py`: `complete` es el estado de la narrativa final, incluidas finalizaciones con error/cancelación. No implica éxito de negocio.
- Navegar/desmontar sólo separa la vista; **no** cancela trabajo implícitamente. Cancelar manualmente nunca envía automáticamente el siguiente borrador.

## Evidencia del snapshot final

```
NODE_OPTIONS=--no-experimental-webstorage npm test -- \
  src/hooks/useChatLifecycle.test.tsx src/hooks/useChatIsolation.test.tsx \
  src/views/ChatLifecycle.test.tsx src/views/ChatComposer.test.tsx \
  src/views/ChatDraftIsolation.test.tsx src/api/chat-lifecycle.test.ts src/App.test.tsx
```

39 PASS, 7 archivos, 1.68 s. Incluye contrato HTTP del cliente real con fetch controlado, EventSource controlado con deduplicación, cancelación duplicada/error/tardía, navegación, espejo de otra tarea, restore desconocido, batch obsoleto, DOM/IME/Shift+Enter, foco y 409. Las primeras pasadas detectaron referencias de estado eliminadas y errores del arnés; corregidos, no contados como verde.

`NODE_OPTIONS=--no-experimental-webstorage npm run build`: PASS (TypeScript + Vite), build final 2.04 s. Aviso existente: chunk principal 889.46 kB. `git diff --check`: PASS. No se repitió la suite completa de producto.

Chrome propio headless con reduced-motion, 1280×900 y 390×900: PASS. Renderer real `Layout/useChat/ChatView`, fixture declarada con HTTP/SSE ficticios; no proveedor ni gasto. Verificado botón visible en viewport, ausencia de overflow horizontal, un único POST cancel y un único POST chat, borrador preservado, reconexión/error visible, foco al finalizar y cero page errors. El primer intento del arnés entregaba un shape incorrecto al footer de actualizaciones; corregido al DTO de requests, sin cambio de producto fuera del lote.

Capturas locales inspeccionadas:

- `/tmp/community-chat-reconnect-1280.png`
- `/tmp/community-chat-reconnect-390.png`
- `/tmp/community-chat-cancel-1280.png`
- `/tmp/community-chat-cancel-390.png`

Arnés local `/tmp/community-chat-lifecycle-qa.mjs`; fixtures `.ui-chat-lifecycle-review.html/.tsx` **no versionables**. Browser QA no es validación de binario Tauri ni cancelación end-to-end de un modelo real.

## Pendiente real

- Smoke nativo Tauri + daemon real: cancelación durante herramientas/permiso pendiente, suspensión/reanudación del equipo, credencial expirada y reconexión real. Este corte conserva la jaula; no la certifica por mocks.
- Un stream definitivamente cerrado sin fila terminal deja aviso/poll explícitos; no inventa éxito ni reenvía. Recuperar la página permite reenganchar el handle guardado. No se añadió un segundo protocolo de reintento.
- No hay cancelación antes de que enqueue entregue su ID. Navegar durante enqueue no cancela la tarea que pueda haber aceptado el servidor; revisar Tareas.
- El espejo sólo permite saber finalización narrativa, no distinguir con un enum estructurado éxito/cancelación/fallo del task. No se afirma «cancelada» a partir de `done` o `complete`.
- Quedan QA general de todas las rutas, rendimiento de bundle y release nativa; no se declara UI completa por este lote.

## Archivos de entrega

`frontend/src/hooks/useChat.ts`, `useChatIsolation.test.tsx`, `useChatLifecycle.test.tsx`; `frontend/src/components/Layout.tsx`; `frontend/src/views/ChatView.tsx`, `ChatView.module.css`, `ChatLifecycle.test.tsx`; `frontend/src/api/chat-lifecycle.test.ts`; `frontend/src/lib/i18n.ts`; este informe. Sin stage, commit ni push por el subagente.
