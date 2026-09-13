# Safent 0.9.21 — preparación y aceptación pendiente

## Alcance de esta preparación

Se alinean las diez fuentes de versión del runtime, frontend y app nativa a
0.9.21. Los locks sólo cambian la versión del paquete raíz, sin modificar
dependencias. La rama de trabajo es `fix/safent-review-20260911`; la base de
esta preparación es `918ce983f4bba80f985be5ab2855e3291f1f24aa`.

Antes de editar se verificó que no existían el tag remoto `v0.9.21` ni una
release GitHub con ese nombre. Esta preparación no crea tags, no publica
imágenes ni paquetes, no inicia workflows y no modifica la app instalada.

## Estado real conocido al preparar la versión

Observaciones comunicadas por el coordinador durante el diagnóstico de la
instalación limpia de 0.9.20, no nuevas pruebas de aceptación de 0.9.21:

- El arranque inicial de 0.9.20 falló. El coordinador volvió a abrir la app y
  ahora funciona; esa reapertura no demuestra que una instalación limpia o un
  arranque en frío estén corregidos.
- La comprobación de salud de la API de Ads devolvió código de salida 0.
- Se observó un timeout al conectar MCP y `net.ipv4.ip_forward=0` en el
  espacio de red del core. La salud HTTP de Ads no prueba conectividad MCP.
- El reload CLI espera 15 segundos, mientras que la conexión MCP puede esperar
  120 segundos. Este desajuste de plazos causa un fallo visible del reload.

Los cambios de readiness, plazos y encaminamiento MCP se están implementando
y validando por separado. Este commit de metadatos no los incorpora ni afirma
que el defecto esté resuelto.

## Validación de esta preparación

- Consistencia de versiones e importación del runtime: **2 pruebas aprobadas**.
- UI del actualizador nativo: **56 pruebas aprobadas** con Node 24; no se cambió
  el comportamiento del botón en esta preparación.
- `cargo metadata --offline --locked --no-deps` confirma
  `safent-desktop 0.9.21` sin resolver dependencias nuevas.

Estas comprobaciones no sustituyen las regresiones de arranque/MCP ni la
aceptación del paquete instalado.

## Puertas antes de publicar y aceptar

1. Integrar y revisar los cambios de arranque/readiness, reload y red MCP.
2. Ejecutar sus regresiones y confirmar las once lecturas de metadatos contra
   `VERSION` antes de etiquetar el commit integrado.
3. Sólo tras autorización del coordinador, publicar la imagen y el paquete
   nativo mediante los workflows oficiales y verificar sus firmas/digests.
4. Probar el paquete firmado 0.9.21 en instalación limpia y arranque en frío:
   la app y Ads deben quedar listos sin reapertura o comandos manuales.
5. Verificar por separado salud API y conexión MCP, con los plazos reales del
   instalador/reload, conservando datos y sin habilitar rutas de red ajenas.

La aceptación del paquete instalado 0.9.21 queda **pendiente**.
