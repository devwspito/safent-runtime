# Safent 0.9.7 — cierre de arranque y conexión OAuth

Prepublicación: código corregido y pruebas focales aprobadas; aún no constituye certificación del DMG ni de conexión a cuentas reales.

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
