# UI-CONFIG — Community: proveedores, uso, Skills y MCP

Estado: corte frontend integrado en el árbol de revisión, sin commit por el
subagente. No modifica el daemon, Hermes, la jaula, el binario Tauri ni Enterprise.
El código backend concurrente pertenece a root y no se cuenta como verificación
de este informe. No significa «toda la aplicación terminada».

## Inventario real y criterio

| Ruta real | Componente | Correcto y conservado | Defectos corregidos / límite |
| --- | --- | --- | --- |
| `/sistema?tab=proveedores` (`/proveedores` redirige) | ProvidersView | Catálogo Hermes, unión del proveedor nativo activo, deduplicación del mirror, bloqueo de edición cloud, formulario de modelo requerido. | OAuth tardío, lectura fallida ocultada, estado de conexión efímero, activación antes de probar en tarjeta Codex API key, disposición móvil. |
| `/sistema?tab=coste` (`/coste` redirige) | UsageView | Resumen, serie y desglose existentes; carga diferida del hub. | Error vs vacío, fuentes parciales, periodo perdido al fallar, coste cero ≠ propio, tokens ≠ ciclos, enlace a `/agentes` retirado. |
| `/capacidades?tab=skills` (`/skills` redirige) | SkillsView | Instalación nativa/Hub, aprobación exacta con grant, ficha de detalles, uso de habilidades en chat. | Fallo de scan no puede iniciar instalación, estado instalado desconocido, búsquedas fallidas y tardías, disposición vacía móvil. |
| `/capacidades?tab=mcp` | McpView | Servidores reales, parámetros de instalación, aprobación exacta, interfaz existente del companion Ads. | Fallo de scan no puede iniciar instalación, catálogo malformado/tardío, ancho móvil, movimientos de hover innecesarios. |

La skill `emil-design-eng` se leyó completa y se aplicó sólo al bloque acordado.
Se conservaron tokens compartidos, botones y diálogos ya consistentes. No se
introdujeron librerías, proveedores ni contratos nuevos para producir una demo.

| Before | After | Why |
| --- | --- | --- |
| Error HTTP de proveedor activo → `null`; OAuth → `unknown`. | Error propagado y estado recuperable; ausencia nativa sólo para respuesta vacía confirmada. | No afirmar «sin configurar» ante una fuente inaccesible. |
| Código OAuth sólo en toasts, popup asíncrono único camino. | Código y enlace persistentes, URL HTTPS sin credenciales embebidas, botón para comprobar conexiones. | Permitir continuar si se bloquea el popup y evitar perder el código. |
| Start/poll OAuth podían terminar después de salir, iniciar dos solicitudes o dejar rechazos sin capturar. | Singleflight, generación por hook, timer cancelado y respuestas obsoletas ignoradas. | No abrir ventanas ni refrescar otra vista por un resultado tardío. |
| Codex API key se guardaba con `set_active:true` antes de comprobar. | Guardar inactivo, probar, activar sólo si se confirmó éxito y la vista sigue presente. | No desplazar la selección por una credencial no verificada. Native/custom ya tenían default backend false; ahora es explícito. |
| Selector de modelo parecía gobernar también OAuth aunque no se enviaba al endpoint. | Nota explícita: la selección de esta tarjeta aplica a API key; OAuth usa flujo/modelo Hermes nativo. | No prometer una selección que el contrato no aplica. |
| Fallos de Uso se transformaban en objetos con cero y `available:false`. | Resumen inaccesible = error; desglose inaccesible = error local con resto de cifras confirmadas. | No mezclar falta de datos con gasto cero ni borrar cifras válidas por una fuente parcial. |
| Vista «tokens» dibujaba ciclos y el coste cero se llamaba propio. | Serie `tokens`, etiquetas concordantes y coste registrado `$0.00` sin inferir alojamiento. | Mostrar lo que el contrato realmente informa. |
| Cuatro métricas comprimidas a 390 px y botones que estrechaban demasiado los vacíos. | Métricas 2×2, controles debajo del título, vacíos en dos columnas con acción propia y filas MCP adaptables. | Densidad legible sin overflow ni grandes áreas vacías. |
| Cascadas animadas en cada carga de configuración; elevación de filas al pasar el puntero. | Secciones inmediatas; sin stagger de entrada ni elevación decorativa en Skills/MCP. | Son vistas de uso repetido; el movimiento no explicaba ningún cambio. |
| Scan inaccesible → instalar directamente en Skills/MCP. | No instalación ni override cuando el scan falla o su veredicto es desconocido. | Conservar la revisión obligatoria, sin transformar una ausencia de evidencia en PASS. |
| Helpers MFA de Community muertos y mocks de enrolamiento sin consumidor. | Helpers/DTO eliminados y tests de confirmación sin mocks MFA. | Reflejar la decisión de producto sin quitar aprobación del propietario. |

## Contratos y límites de seguridad

- `api/client.ts` sólo cambia getters de configuración/uso y elimina helpers MFA
  muertos por petición de root. Se revisaron todos los consumidores de los getters:
  los de Hub están en SkillsView; búsqueda MCP en McpView; proveedor/OAuth en
  ProvidersView; Usage en UsageView. No se necesitó editar ContextPanel.
- Búsquedas Hub ya no convierten fallos HTTP en `[]`. La vista captura el error,
  no lo presenta como ausencia de resultados y descarta búsquedas anteriores.
  Los polls de instalación capturan errores y dejan de afirmar progreso.
- La unión del listado de Skills exige tanto el origen nativo como Hub. Si uno
  falla no se da por verificada la lista instalada ni se inicia una instalación.
- Los scans con `error: scan_unavailable` sin veredicto, `UNKNOWN`, excepción o
  null, PASS sin scan_id o flag de aprobación inválido quedan cerrados. No se
  solicita un grant para sobreescribir ese error.
  Root ha coordinado el cierre equivalente en backend; no se atribuye aquí su QA.
- La generación de OAuth controla seguimiento **local**, no cancela ni revoca
  la autorización del proveedor. Salir durante una autenticación no asegura que
  ésta no termine en servidor. La UI lo dice cuando no puede verificar el estado.
- `window.open` y el enlace de autorización conservan el mecanismo frontend
  existente. Falta confirmar el comportamiento del opener Tauri y callbacks en
  el binario real; no se modificó su capability ni se elevó ningún permiso.
- OAuth usa un dominio devuelto por el backend y exige HTTPS; no se inventó una
  lista global de dominios ni un transporte de tokens. Códigos ficticios sólo QA.
- Los errores de lectura/scan/OAuth de este corte no imprimen cuerpos upstream.
  No se afirma haber saneado todos los errores libres de mutaciones preexistentes.

## Pruebas

`NODE_OPTIONS=--no-experimental-webstorage npm test`: **257 PASS, 44 archivos**.
`NODE_OPTIONS=--no-experimental-webstorage npm run build`: TypeScript + Vite PASS.
Se mantiene el warning de chunk principal >500 kB; no se cambió bundling global.
JSDOM emite avisos preexistentes de scrollTo; no son tests fallidos. El proyecto
no define script lint, por lo que no se declara una pasada de lint inexistente.

Nuevas pruebas: `api/config-read-errors.test.ts`, `views/ProvidersOAuth.test.tsx`,
`views/UsageView.test.tsx`. Ampliadas ProviderRow, SkillsView y McpView. Limpieza
de mocks MFA en GovernanceSection/KillSwitchSection. Cobertura del corte:

- HTTP 503 real a través del wrapper con sesión ficticia, respuesta nativa
  vacía vs malformada; sin convertir errores en ceros/listas vacías.
- OAuth singleflight, popup bloqueado, enlace/código persistido, URL insegura,
  resultado start tardío, aprobación poll tardía, fallo de poll y aprobación real
  del contrato (simulada, sin autenticar cuentas).
- Prueba de conexión fallida nunca activa; prueba tardía tras salir nunca activa.
- Usage disponible/incompleto/fallido, desglose parcial, cero sin inferir propio,
  tokens reales de la serie, periodo conservado al reintentar, respuesta antigua
  tras StrictMode, vacío sólo con fuentes confirmadas.
- Skills/MCP no llaman instalación al fallar el scan, con veredicto desconocido
  ni con scan tardío después de salir. Aprobación válida con grant sigue pasando.
- Skills búsqueda fallida e inventario Hub fallido se muestran como errores.

### QA visual y teclado (no confundir con QA del binario)

Chrome headless propio mediante Playwright instalado, aislado del navegador del
usuario. Fixture `.ui-config-review.html/.tsx` **no commiteable**, datos ficticios
y fetch controlado. URL local `http://127.0.0.1:5201/app/.ui-config-review.html`.
No se hicieron llamadas a proveedores, instalaciones ni cambios de credenciales.

Revisión de 1280×900 y 390×844: proveedores, OAuth con popup bloqueado, uso con
gráfica inaccesible, Skills con búsqueda fallida, MCP y proveedor inaccesible.
La primera captura reveló métricas truncadas y texto estrecho de los vacíos;
se corrigió y se repitió. Resultado final: cero errores JavaScript, sin overflow
horizontal en proveedores/uso, disposición móvil de Skills/MCP revisada en imagen.
Prueba separada de teclado: Enter selecciona periodo, Tab pasa al periodo siguiente,
con preferencia reduced-motion activada. Sin prueba de dispositivo táctil físico.

Artefactos locales: `/tmp/community-config-visual.mjs`,
`/tmp/community-config-keyboard.mjs` y capturas `/tmp/community-config-*.png`.
En particular `usage-390`, `providers-390`, `oauth-1280`, `skills-390`, `mcp-390`
y `providers-error-390`. No se publican estas fixtures en la imagen final.

## Pendientes que este corte no resuelve

1. OAuth end-to-end en Community **nativa** con cuentas reales, abrir navegador
   externo, completar callback/device flow, comprobar proveedor/modelo efectivo
   y reiniciar la app. La UI no convierte prueba simulada en autenticación real.
2. Seleccionar un modelo específico durante OAuth exige contrato backend/nativo
   explícito; el selector API-key no se reutiliza fingiendo soporte.
3. Cancelación/retomar autorización en servidor, invalidación entre varias ventanas
   y exclusión global entre conexiones de proveedores distintos no se añadieron.
4. Catálogo/modelos predefinidos: no se auditó disponibilidad comercial actual ni
   se cambiaron versiones de paquetes sugeridos por intuición.
5. Integración real de Skills/MCP con scanner, permisos OS e instalación en el
   contenedor. Las pruebas DOM confirman solicitudes y cierres, no ejecutan npx.
6. No se garantiza que toda mutación remota devuelva resultado idempotente tras
   un timeout. No se añadieron reintentos automáticos de escrituras.
7. No hubo pruebas de precisión/facturación contra proveedores ni nuevas métricas
   del backend; Uso sólo representa los campos que recibe y valida.

El informe cierra defectos demostrados de estas cuatro rutas. No cierra todo el
rediseño de Safent, la herencia LLM, Ads ni la publicación de Community.
