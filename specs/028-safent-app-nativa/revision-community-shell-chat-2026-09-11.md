# Community: revisión del shell y chat — 2026-09-11

Estado: bloque implementado en el árbol de revisión. No representa el rediseño completo de Community, ni una publicación o validación de la aplicación Tauri instalada.

## Alcance y criterio

Se aplicó la skill `emil-design-eng`, leída completa, y la captura de Codex desktop proporcionada por Luis. Se priorizan conversación, densidad, navegación inmediata y estados honestos. No se han añadido dependencias de animación ni maquetas como código de producción.

| Before | After | Why |
| --- | --- | --- |
| Sidebar fija, marcador azul «L», navegación debajo de solo tres recientes | Wordmark Safent, navegación compacta arriba, ocho recientes, búsqueda local y sidebar plegable | Jerarquía estable y más espacio para conversar sin perder funciones |
| Navegación activa con línea azul luminosa | Selección neutra por superficie | El color semántico se reserva para información y estados relevantes |
| Recientes implementados como `listbox` sin interacción de listbox | Lista de botones con `aria-current` | La semántica coincide con la navegación real |
| Fallo al cargar historial convertido en lista vacía; respuestas anteriores podían sobrescribir posteriores | Error recuperable, conserva lista conocida y descarta respuestas obsoletas | Un fallo de conexión no equivale a perder el historial |
| Menú del compositor sin foco inicial ni flechas/Escape | Foco inicial, flechas/Home/End, Escape y retorno al disparador | Teclado completo sin animación ni espera |
| Enter enviaba durante composición IME y saltaba el bloqueo visual de adjuntos | La misma guarda rige teclado y botón; IME no envía; adjuntos pendientes/fallidos bloquean envío | Evita mensajes accidentales o incompletos |
| Posibles dos envíos antes del render del padre | Guarda síncrona de envío | Evita duplicados entre eventos rápidos |
| Campo bloqueado durante streaming | Se puede preparar el siguiente borrador; envío sigue bloqueado y permanece el botón detener | El trabajo del agente no bloquea la escritura del usuario |
| Error al cargar habilidades mostrado como «Ninguna» | Error explícito y reintento | Distingue indisponibilidad de ausencia de habilidades |
| Sugerencia enviada inmediatamente | Sugerencia rellena y enfoca el compositor | El usuario puede revisar/editar antes de enviar |
| Botones textuales de enviar/detener y título genérico | Controles circulares con nombre accesible, título tomado del primer mensaje, panel opcional | Reduce ruido y mantiene orientación |
| Entrada animada de delegaciones y adjuntos en una acción repetitiva | Entrada inmediata; feedback de pulsación limitado y sin animación de teclado | La frecuencia justifica velocidad, no ornamentación |
| Texto/adjuntos/carpeta/habilidades pertenecían al componente visible | Borrador en memoria de Layout por hilo o agente de conversación nueva | Navegar o terminar una subida no traslada contexto a otro hilo |
| Respuesta tardía de `postChat`, carga histórica o stream antiguo alteraba el estado actual | Generación invalidada al detener/navegar/desmontar; callbacks y resultados asíncronos comprueban pertenencia | Evita respuestas mezcladas y que `done/error` de A limpien la tarea B |

## Archivos de este bloque

- `frontend/src/components/Layout.tsx`
- `frontend/src/components/Layout.module.css`
- `frontend/src/components/Layout.test.tsx`
- `frontend/src/views/ChatView.tsx`
- `frontend/src/views/ChatView.module.css`
- `frontend/src/views/ChatComposer.test.tsx`
- `frontend/src/api/conversation-errors.test.ts`
- `frontend/src/lib/chatDrafts.ts` y `chatDrafts.test.ts`
- `frontend/src/views/ChatDraftIsolation.test.tsx`
- `frontend/src/hooks/useChat.ts` y `useChatIsolation.test.tsx` (ampliación autorizada para aislamiento de streams)
- `frontend/src/lib/i18n.ts` (textos ES/EN, además de dos textos solicitados por el agente principal para políticas)

El agente principal eliminó, coordinadamente, el `catch(() => [])` de `listConversations` en `api/client.ts`. El test de cliente usa la implementación real para que el estado de error no sea solo una simulación de componente.

## Verificación

- `NODE_OPTIONS=--no-experimental-webstorage npm test`: **175 pruebas correctas en 34 archivos**, exit 0, sobre el árbol compartido tras la ampliación de aislamiento y la corrección del banner por el agente principal. Veinticuatro pruebas nuevas de este bloque: seis compositor, tres recientes, una cliente real, cuatro integración de borradores, dos almacén y ocho hook de chat.
- `NODE_OPTIONS=--no-experimental-webstorage npm run build`: TypeScript y Vite correctos. Persiste aviso de chunks grandes: principal ~982 kB y Swarm ~1.415 kB sin comprimir; no se ha ocultado el aviso.
- `git diff --check`: correcto.
- CUA: inspección de la UI real montada en fixture aislada a 1280 × 720: bienvenida, conversación guardada, sidebar plegada/reabierta con restitución del foco, panel de contexto abierto/cerrado, menú anclado con foco inicial y búsqueda local que filtra por «marca».
- La fixture intercepta todas sus peticiones y deniega mutaciones; sus datos son ejemplos identificados por una banda visible. No demuestra acceso real a Google, Meta ni funcionamiento de Ads.
- Fixtures locales **no publicables**: `.ui-community-review.html` y `.ui-community-review.tsx`; no incluir en commits ni imágenes. Las fixtures anteriores `.ui-review.*` no fueron modificadas.

## Pendientes y hallazgos fuera del bloque

1. Verificar ventana nativa Tauri, variantes estrechas, zoom y lector de pantalla sobre la compilación integrada; no se han probado aquí con hardware real.
2. Completar onboarding, Agentes, hubs, archivos, integraciones y todas las superficies restantes. Este cambio no equivale a «UI entera terminada».
3. El panel de contexto todavía añade «Búsqueda web» como conector incorporado sin consultar disponibilidad; los errores de fuentes del panel siguen ocultándose. Necesita una revisión propia de estados reales y carga independiente.
4. La resolución del modelo del compositor y el banner de modelo usan consultas separadas; una indisponibilidad transitoria puede presentarse como ausencia de modelo. Consolidar el estado compartido en un bloque dedicado.
5. Borradores aislados durante la sesión: persisten al cambiar de hilo/agente/vista, no al cerrar/reabrir la aplicación o recargar la página. Persistencia entre sesiones requeriría almacenamiento privado apropiado; no se han enviado textos ni archivos a localStorage/sessionStorage.
6. Auditar y reducir el bundle principal/Swarm con límites de ruta antes de la imagen final.
7. No se modificaron permisos, aprobaciones, jaula ni backend en este bloque. No hubo commit, push, despliegue o publicación de imagen desde este agente.

## Ampliación: aislamiento real de hilos y borradores

Regresión roja → verde comprobada antes y después:

- La prueba integrada de Layout + ChatView + Composer falló porque el borrador del hilo A seguía visible tras pasar a B. Con el almacén por hilo, B queda independiente y la subida tardía se conserva exclusivamente en A.
- Cuatro pruebas de `useChat` fallaron inicialmente: respuesta de envío tras Nuevo chat, error antiguo que marcaba B como fallido, callbacks `done/error` de A que borraban el task ID de B y carga histórica fuera de orden. Las cuatro pasan después de la generación de sesión.
- Cobertura adicional: editar mientras hay streaming, salir y volver a la vista Chat, separar borradores nuevos por agente, migrar borrador nuevo a su conversation ID, detener con enqueue pendiente, fallo tardío de restauración, restauración bajo React StrictMode y agente seleccionado con conversation ID todavía null.
- El envío sigue siendo explícito; no hay reenvíos automáticos. Detener invalida la respuesta tardía y cierra el transporte existente. Esto no inventa una API de cancelación remota: `stopStream` conserva su contrato existente, no prueba que el servidor haya cancelado una tarea ya encolada.

Contrato pendiente: `ConversationDetail`/`ConversationSummary` no proporcionan `agent_id`, y el hook conserva el agente anterior al abrir un histórico. Para borradores históricos se usa conversation ID con agente desconocido explícito, nunca el agente anterior como supuesto propietario. El agente de una conversación nueva sí queda asociado al promover el borrador a su ID; no se altera el agente que el backend ya tiene vinculado a una conversación existente.

Los archivos subidos siguen dentro del workspace autorizado compartido: esto aísla el **contexto que se envía**, no establece una ACL de archivos por conversación. La API de uploads deduplica los nombres de adjuntos; folder bridge mantiene su contrato de actualización de la carpeta elegida.
