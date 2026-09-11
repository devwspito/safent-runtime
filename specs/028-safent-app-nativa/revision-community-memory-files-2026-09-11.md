# Community: Memoria y Archivos — estados y concurrencia

Fecha: 2026-09-11. Bloque acotado sobre `ae3d47c`, sin cambios de backend ni contratos. No implica que toda la UI o la aplicación nativa estén terminadas.

## Decisiones de diseño

Aplicada la skill `emil-design-eng` leída completa. Navegar y buscar son acciones frecuentes: respuesta inmediata, sin entradas escalonadas ni desplazamientos ornamentales. Se mantienen controles y política existentes; el contenido incompleto no se presenta como editable.

| Before | After | Why |
| --- | --- | --- |
| Una respuesta de detalle antigua podía sustituir la entrada seleccionada. | Generaciones para lista/detalle y comprobación de montaje. | Nunca mezclar contenido entre entradas. |
| Si fallaba el detalle se podía editar el resumen truncado. | Error recuperable, sin editor y sin guardar hasta obtener contenido completo. | Evitar pérdida de contenido. |
| Guardar/eliminar podía alterar o cerrar un panel abierto después. | Escritura ligada al identificador y generación originales; bloqueo de escrituras paralelas. | Una respuesta antigua no actúa sobre otra selección. |
| Reabrir durante el guardado podía cargar contenido anterior a la escritura. | Espera a la escritura enviada antes de volver a obtener el detalle. | El servidor sigue siendo la fuente de contenido guardado. |
| Un identificador ausente se reconstruía usando índice cero. | No se inventa índice: detalle/guardar/eliminar quedan bloqueados. | No operar sobre la primera entrada por error. |
| Respuestas de lista inválidas parecían una colección vacía. | Estado de error y reintento; se conserva consulta/ruta. | Diferenciar ausencia de datos de fallo. |
| Un HTTP 403 podía aparecer como texto del archivo. | Se verifica `response.ok`; mensaje genérico y reintento sin mostrar el cuerpo de error. | No presentar respuestas del servidor como documentos. |
| La vista previa antigua podía sustituir el archivo actual. | AbortController más comprobación de abort en éxito/error/finally. | Soporta incluso transporte que termina después de abortar. |
| Finalizar una subida devolvía al usuario a raíz aunque hubiera navegado. | Sólo vuelve a raíz si el usuario no ha cambiado de carpeta durante la subida. | Respetar el contexto actual. |
| Alias global `.memory-item` imponía columna y tarjetas altas. | Filas compactas con CSS local, superficies neutras y foco visible. | Densidad consistente, sin conflicto con estilos antiguos. |

## Archivos del bloque

- `frontend/src/views/MemoriaView.tsx` y `MemoriaView.module.css`.
- `frontend/src/views/ArchivosView.tsx` y `ArchivosView.module.css`.
- `frontend/src/views/MemoriaView.test.tsx` (11 casos).
- `frontend/src/views/ArchivosView.test.tsx` (7 casos).
- `frontend/src/lib/i18n.ts`: tres claves nuevas ES/EN para error de detalle, error de vista previa y archivo vacío.

No modificados `api/client.ts`, tipos API, Drawer compartido, backend, Tareas ni componentes nativos.

## Evidencia ejecutada

Primero se reprodujeron **8 regresiones rojas** en las dos vistas (exit 1). Tras implementación y ampliación: **18/18 focales** verdes. Casos: detalle tardío, fallo completo, lista inválida, identificador ausente, guardado no confirmado, guardado/eliminación sobre selección antigua, reapertura pendiente, desmontaje, búsqueda más reciente; HTTP403, archivo previo, listado inválido, reintento/vacío, navegación obsoleta, subida con cambio de carpeta y aborto al desmontar.

Comprobación final con código quieto, 18:29 Europe/Madrid:

```sh
cd /tmp/safent-takeover.Ys0aPw/runtime/frontend
NODE_OPTIONS=--no-experimental-webstorage npm test
NODE_OPTIONS=--no-experimental-webstorage npm run build
git diff --check
```

- **42 archivos de pruebas, 221 tests PASS**, exit 0.
- TypeScript y build Vite PASS, exit 0.
- `git diff --check` limpio.
- Advertencias no ocultadas: jsdom `Window.scrollTo` no implementado; bundle principal 792.12 kB minificado (>500 kB). No bloquean el build, pero el tamaño continúa pendiente de optimización global.

## Comprobación visual y teclado

Chrome headless aislado, vistas reales y wrapper API real con respuestas HTTP ficticias, sin credenciales ni datos reales. Fixture local **no publicable/no commiteable** `.ui-memory-files-review.html/.tsx`, puerto5196 `/app/`. Verificadas capturas a1280×800 y390×844, también reduced-motion:

- `/tmp/community-memory-list-wide.png`: filas compactas tras eliminar conflicto legacy.
- `/tmp/community-memory-error-wide.png`: mensaje y reintento; guardar deshabilitado.
- `/tmp/community-memory-editor-mobile.png`: contenido completo, acciones visibles, sin overflow horizontal.
- `/tmp/community-files-error-wide.png` y `/tmp/community-files-error-mobile.png`: HTTP403 recuperable, cuerpo privado no visible.
- `/tmp/community-files-preview-mobile.png`: documento tras reintento.

Medido: `scrollWidth > innerWidth` falso en390px; guardado sin detalle deshabilitado; `PRIVATE ERROR BODY` cero coincidencias; Enter abre la entrada, Escape cierra y restaura foco al activador. Vista previa con `tabIndex=0` y foco verificado en sesión reduced-motion desde el inicio. Las capturas se inspeccionaron visualmente. Una primera medición de foco inmediatamente después de cambiar el tamaño/motion durante la apertura fue inestable; no se contó como aprobación, se repitió desde sesión estable.

## Límites / pendiente real

- Esta es QA del contenido web de Community, **no** prueba del binario Tauri, selector de archivos del SO, puente nativo ni instalación final.
- Una escritura/subida ya enviada puede finalizar tras cerrar la vista. No se afirma cancelación de servidor: se suprimen efectos UI obsoletos y no se envía el resto de un lote tras desmontar.
- La API actual sube a raíz; no se inventa subida a la carpeta seleccionada.
- No hay revisión optimista/ETag para edición simultánea desde otros clientes. Identificadores compuestos por índice siguen siendo contrato backend; garantizar identidad estable ante cambios concurrentes externos requiere trabajo de API, fuera de este bloque.
- No se habilitó nueva autoridad ni se alteraron controles de jaula, OAuth, políticas o permisos.
- Faltan QA con backend/nativo reales y el resto de rutas del plan general; cerrar estas dos vistas no cierra el rediseño global.
