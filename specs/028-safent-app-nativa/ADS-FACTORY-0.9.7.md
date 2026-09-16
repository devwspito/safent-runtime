# Safent 0.9.7 — cierre de arranque y conexión OAuth

Estado tras prueba real del 13 de septiembre: artefactos publicados y firmas verificadas, pero **0.9.7 no supera la aceptación funcional**. Se ha marcado como prerelease para no ofrecerla como actualización estable. No constituye certificación de conexión a cuentas reales.

## Prueba del DMG en el Mac: resultados y defectos encontrados

- Workflow de motor `34733246480`, Ads `34733247231` y desktop `34733993399` (segundo intento de publicación) completados.
- DMG `Safent_0.9.7_aarch64.dmg`: SHA-256 `ba1632969f88bd861e1a07ad8e8ef9308532747fd7b81419646d1c69b962c53d`, 1.016.789.189 bytes. `codesign --verify --deep --strict`, `spctl` (Notarized Developer ID) y `stapler validate` pasan. Compose empaquetado tiene modo 0755.
- Instalado desde ese DMG en `/Applications/Safent.app`, conservando copia recuperable de 0.9.5 y todos los datos de `.safent`. El arranque GUI, sin preprovisión manual, descarga el motor y Ads, entra en Chat y muestra 0.9.7. El motor real queda en el digest arm64 fijado `033caf770f49d23a3f0eee2ae3d3285ff849f9b0060868f7ed8298cefe9cd23f`, con volumen `safent-data` y proyección Ads de sólo lectura. En esta actualización el puerto cambia de 35335 a 40843; no se certifica conservación del puerto entre versiones.
- **Fallo de convergencia**: el motor nuevo y la imagen Ads nueva descargada no bastan. Los servicios Ads existentes siguen ejecutando 0.2.2; el handshake MCP devuelve 64 herramientas pero versión 0.2.2. La comprobación de disponibilidad no comparaba las imágenes de los contenedores en ejecución con el pin del paquete. Además el inventario podía sobrescribir el pin deseado con el persistido antiguo. Corrección en curso para la siguiente versión.
- **Fallo de panel embebido**: el proxy responde 200 a `/ads/` y `/ads/api/v1/auth/me`, pero el navegador termina en `/login`. El HTML inyecta script inline y `<base>`, ambos prohibidos por la CSP de producción. La corrección debe usar metadata no ejecutable y rutas de assets prefijadas, sin relajar `script-src` ni `base-uri`.
- **Carrera de publicación corregida en pipeline**: la matriz creó dos borradores v0.9.7 y el gate final rechazó el inventario incompleto. Se consolidaron los cuatro archivos Linux x64 verificando sus SHA-256 antes y después, conservando respaldo, y se eliminó únicamente el duplicado. El segundo intento del gate final pasó: release única 387774699, 15 assets, 14 hashes coincidentes, manifiesto minisign válido y URLs del actualizador correctas. Commit preventivo `ff5e6d1f`: resolver un solo draft antes de la matriz, pasar `releaseId` explícito, rechazar identidades ambiguas/publicadas y serializar publicaciones del mismo tag. 82 pruebas pasan y 3 se omiten por herramientas Apple no disponibles en Linux; revisión independiente aprobada.

Pendiente para cerrar: publicar la corrección siguiente, repetir arranque GUI y reapertura idempotente, comprobar Ads en la versión fijada y panel embebido sin segundo login. No repetir la limpieza del Mac ni sustituir validación GUI por una reparación manual oculta.

## Por qué no entregar 0.9.6

La imagen 0.9.6 se construyó, pero el workflow desktop 34731924110 se canceló intencionalmente antes de completar el DMG. El borrador parcial no se debe publicar. La última auditoría encontró que un motor creado sin el volumen de proyección de Ads no podía recuperarse: verificaba Ads antes de volver a montar su registro seguro.

El commit `93b936e` corrige ese caso usando la ruta normal de arranque endurecida. Antes de recrear exige el digest del motor, el volumen de datos exacto y el puerto loopback observado; no permite sustituirlos por valores por defecto. Descarta el ticket y el evento Ready internos. Una segunda reparación no recrea el contenedor.

Validación: 27 pruebas de peticiones/CLI y 7 de inventario pasan; `sh -n` y `git diff --check` pasan. La base 0.9.6 pasó 297 pruebas Python de integración seleccionadas y 242 Rust. Estos resultados no sustituyen la prueba GUI con el artefacto final.

## OAuth: alcance de la corrección

- Corregir la URL pública de callback que muestra el panel y mantenerla idéntica a la usada al iniciar OAuth.
- Permitir únicamente el callback externo exacto validado por un `state` ligado al proveedor, expirable y de un solo uso. No relajar la autenticación del proxy general.
- Abrir Google/Meta en navegador del sistema desde la app, sin permitir navegación arbitraria en la ventana principal.
- Google Desktop usa loopback, PKCE y client ID; no exigirle el secreto de un cliente web confidencial.
- Meta no está certificado end-to-end. No distribuir un secreto maestro de Safent en instalaciones Community. El caso de una app propia de Friendog es distinto de un servicio compartido para clientes.

Validación OAuth integrada: 41 pruebas TLS del puente, 9 Rust de ventana, 24 HTTP/PostgreSQL real efímero y 187 backend focales de Google Desktop aprobadas; estos conjuntos se solapan y no deben sumarse. El panel pasó 5 pruebas, typecheck y build. Clippy/fmt pasan. Google Desktop incluye prueba de renovación por los SDK y ADC con transporte simulado; no sólo el primer intercambio. Los parámetros OAuth desconocidos se descartan conforme a RFC 6749 §4.1.2, manteniendo validación de state, proveedor, caducidad, consumo único y duplicados de campos relevantes.

La ventana nativa sólo permite el origen interno exacto de Tauri o el origen autorizado por el arranque. Se deniegan file/data/javascript y protocolos arbitrarios. Los fallos al abrir OAuth muestran un diálogo nativo, no un evento sin consumidor. El actualizador describe correctamente la preparación posterior de componentes (17 pruebas renderer y 6 Rust aprobadas).

Límite de recuperación verificado por revisión independiente: si una reparación reclamada reinicia el core y no puede renovar su lease mediante exec a los 20 segundos, aborta de forma cerrada y exige una nueva petición; no declara éxito ni omite el lease. El arranque GUI converge el montaje antes de habilitar el consumidor. No se ha certificado una reparación reclamada con caída larga del core.

## Verificación real pendiente de usuario

En el Mac, el bróker no tiene aplicaciones Google/Meta configuradas. Google exige reautenticación de `arturo.soria@friendog.market`; la sesión personal accesible no muestra el proyecto Friendog `sylvan-plane-508309-q0`. Meta también pide iniciar sesión. No se han obtenido secretos del navegador ni modificado campañas o presupuestos.

Antes de declarar entrega: pruebas OAuth y de seguridad verdes, imágenes 0.9.7/0.2.3 publicadas por digest, DMG firmado y notarizado, actualización preservando `.safent`, arranque GUI con Ads disponible y reapertura idempotente. Si faltan login/consentimientos, reportarlos por separado; no afirmar que Google/Meta están conectados.
