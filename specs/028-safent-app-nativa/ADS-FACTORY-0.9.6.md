# Safent 0.9.6 — Ads preparado automáticamente

Decisión del usuario: Ads forma parte de Community desde el primer arranque,
como las herramientas incluidas. No exige una instalación manual adicional.
La app administra su máquina privada, binarios e imágenes fijadas por digest.
El primer arranque puede descargar imágenes; esto no es una instalación offline.
Conectar cuentas Google/Meta por OAuth sigue siendo una acción del propietario.

## Fallos reproducidos en macOS 0.9.5

- El bootstrap devolvía Ready al obtener el ticket del motor, antes de reconciliar Ads.
- La normalización del bundle convertía docker-compose en 0644. La instalación
  fallaba con permission denied, aunque los archivos y su hash existían.
- La caché de runtime aceptaba cualquier marcador .verified, incluso de otra
  versión, sin verificar el manifiesto ni los permisos del bundle actual.
- La petición de instalar Ads no tenía consumidor nativo activo tras Ready.
- Los bind mounts de archivos retenían el inode del placeholder SSO vacío después
  de su reemplazo atómico. Además, virtiofs reportaba al caller como propietario:
  el registro se rechazaba para hermes y los permisos de secretos no ofrecían
  el aislamiento esperado entre UID.
- HTTP 401 se trataba como salud y el fallo de recarga del companion no hacía
  fallar la instalación. El vocabulario healthy tampoco coincidía con reachable
  en el adaptador nativo.

## Corrección integrada

- Bootstrap nativo mantiene la preparación hasta convergencia de Ads y emite un
  ticket nuevo al finalizar. El companion debe tener un digest válido.
- Bundle, distribución firmada y caché verifican ejecución/permisos de Compose.
- Proyección mínima en volumen Linux `safent-companion-runtime`, montado de solo
  lectura; nunca un bind de los secretos desde macOS. Solo contiene registro,
  CA pública, bearer y clave SSO. No contiene clave de CA, credenciales OAuth ni
  base de datos. Los secretos son 0400 root:root con propiedad Linux real.
- Root valida y copia registro y secretos a tmpfs protegido para hermes. Registro
  inválido o fallo de staging retira el estado anterior. nft usa la fuente
  validada antes del arranque del daemon.
- Consumo nativo de solicitudes acotadas, exclusión con bootstrap/actualización,
  claim con identidad y renovación, fallo terminal y reintento humano explícito.
- La salud debe ser HTTP 200, contrato 1.0.0, DB ok; `no_accounts` es un estado
  instalado válido, no un error ni una cuenta ficticia. La recarga no se ignora.

## Evidencia de esta corrección

- Instalación real en el Mac: Postgres sano, migraciones exit 0, API/worker/broker
  arrancados; health autenticado 200, contrato 1.0.0, Google/Meta sin conectar.
- Canary aislado sobre la imagen real, con los módulos corregidos y proyección
  real: root stages 1 bearer + 1 SSO; hermes carga la clave Ed25519 y recibe
  `no_accounts`/200. UID sandbox 886 no puede leer ninguno de los cuatro paths
  de secretos, ni originales ni staged. No se ha debilitado la validación.
- Suite provisioning/scaffold: 72 PASS. Otras evidencias focales están en los
  informes de Compose, registry staging y la salida de integración de CI.

## Gate de entrega

No confundir el canary ni los tests con una app final instalada. Antes de anunciar
resuelto: publicar imagen 0.9.6, construir y notarizar DMG Apple Silicon, validar
bundle final y ejecutar el arranque nativo contra los artefactos publicados.
No borrar datos del usuario para hacer pasar la prueba. Probar también reapertura
idempotente y que Ads aparezca listo para OAuth sin pulsar Instalar.
