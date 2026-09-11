# Community — Integraciones y diálogos compartidos

Bloque UI parcial sobre `6515a4c`. Community es app nativa; las pruebas React y
capturas de navegador de este documento **no certifican el binario Tauri**.
Skill Emil leída completa: decisiones centradas en densidad, estabilidad, foco y
ausencia de animaciones repetitivas de navegación, no efectos decorativos.

| Before | After | Why |
| --- | --- | --- |
| Fallo de conexiones o catálogo convertido a `[]`, incluso “Todo conectado” | Error localizado con reintento; conexión desconocida bloquea nuevos botones Conectar | Ausencia de evidencia no es estado vacío ni autorización |
| Refresh de foco antiguo podía pisar respuesta nueva | Generaciones por fuente y descarte al desmontar | La UI refleja la petición vigente |
| Entrada escalonada de cada sección/lista al volver a configuración | Contenido inmediato, sin movimiento ornamental | Navegación frecuente y teclado no deben esperar |
| Catálogo sin búsqueda, instrucciones de alta siempre expandidas | Búsqueda local y ayuda en disclosure nativo; tarjetas compactas y responsive | Reducir ruido manteniendo funcionalidad accesible |
| Búsqueda fallback “activa” cuando sólo constaba ausencia de Brave | Sólo afirma DuckDuckGo con `ddgs_fallback` verdadero | Mostrar capacidad verificada, no inventarla |
| Confirmación reemplazada/desmontada dejaba promesa pendiente | Resuelve false; el trigger original se conserva al reemplazar | Nunca convertir navegación/desmontaje en aprobación |
| Drawer con `document.querySelector` global y resets del overflow del body | Base UI existente maneja foco, nesting, Escape y scroll lock; CSS propio compacto | Menos código manual y aislamiento de cada panel |

## Archivos

- `frontend/src/views/IntegrationsView.tsx`, `.module.css`, `.test.tsx`.
- `frontend/src/components/ConfirmDialog.tsx`, `.test.tsx`.
- `frontend/src/components/ui/Drawer.tsx`, `.module.css`, `.test.tsx`.
- `frontend/src/lib/i18n.ts` (claves de este bloque parcialmente incluidas por root
  en 6515a4c; copy restante ES/EN en diff actual).
- `inventario-ui-community.md` actualizado al router Tareas real; no marca toda UI
  terminada por este grupo.

No se cambió API/backend, autoridad de permisos, MFA, router/Layout, Tareas,
desktop nativo, ni las otras vistas de configuración. El Drawer compartido sí
mejora los paneles que lo consumen (incluidos Memoria/Archivos).

## Pruebas y visual

Regresión primero: tres tests de Integraciones rojos (error conectado, catálogo,
respuesta obsoleta); dos de ConfirmDialog rojos (reemplazo y unmount). Corregidos.
Diez tests del bloque cubren además respuesta inválida, fallback no declarado,
Escape/foco, retorno al trigger y preservación de scroll preexistente. Ejecución
final focal: **10 passed, 3 archivos, exit 0**; `npm run build` incluye tsc y Vite,
**exit 0** tras los cambios paralelos de carga lazy de root. `git diff --check`
sin errores.

Suite frontend antes del modal de seguridad paralelo: **39 archivos, 198 tests,
exit 0**. Una ejecución posterior recogió ese nuevo test paralelo y terminó con
202/203: falló exclusivamente la espera inicial de foco en SecurityModal.test.tsx,
comunicado a root; no contar ese run como verde. Build/tsc del bloque completados;
permanece aviso Vite de bundle grande, no error de compilación.

QA visual real Chrome headless aislado, viewport 1280×800 y 390×844:

- Integraciones con catálogo real renderizado desde fixture, lookup connected HTTP
  503 a través del wrapper de producción: alerta visible y acciones deshabilitadas.
- Drawer real abierto, foco de cierre, Escape y regreso al trigger; área scroll y
  footer separados.
- Ancho estrecho: document.scrollWidth=390, sin overflow horizontal.
- Capturas inspeccionadas: `/tmp/community-integrations-wide.png`,
  `/tmp/community-integrations-narrow.png`, `/tmp/community-drawer-wide.png`.
  Capturas actualizadas tras la última revisión de copy/disclosure: cero claims
  falsos de DuckDuckGo y ayuda plegada por defecto.
- Fixture `.ui-polish-review.html/.tsx`: datos claramente marcados, mutaciones
  responden 503, sin cuentas externas. **No incluir en commit ni distribución.**

No se hizo OAuth real, no se activaron claves, no se instaló un servicio y no se
alteró una política desde la revisión visual.

## Pendiente real (no ocultarlo detrás de un build)

1. Memoria: detalle tardío puede llenar el editor de otro item; fallo detalle deja
   texto truncado editable; búsqueda/save necesitan generaciones por selección.
2. Archivos: preview llama `r.text()` sin comprobar `r.ok`; cancelación/finally
   antiguos pueden pisar estado nuevo. Navegación y selector/subida nativos deben
   recorrerse completos.
3. Providers: OAuth en ventana nativa, cancelación/retorno y errores de conexión
   reales; configuración gestionada sigue sujetándose al gate backend LLM.
4. Skills/MCP/Seguridad: instalación, inspección, owner confirmation, fallos y
   aprobación en flujo runtime real; root modifica modales de revisión en paralelo.
5. Usage/En vivo: métricas reales, fechas, desconexión VNC, fullscreen y teclado.
6. Anuncios: el producto companion tiene su propia UI y aceptación; no se valida
   porque el iframe/puente de Community cargue.
7. Nativo: Tauri real, preparación/arranque/reconexión, selector archivos, llavero,
   cámara/mic/captura, notificaciones, diálogo OAuth y permisos aceptar/rechazar.
   Root cubre desktop/src con sus propios tests; aquí no se certificó esa ejecución.

No se inició el siguiente bloque Memoria/Archivos mientras root prepara la tabla
de estado global solicitada por el usuario.
