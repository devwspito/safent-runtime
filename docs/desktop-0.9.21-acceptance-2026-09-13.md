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

## Recuperación comprobada en el Mac (0.9.20, 21:10–21:12 UTC)

- El diagnóstico exportado se detuvo en `companion_reload`, tras los latidos
  de 5, 10 y 15 segundos, con `companion_unreachable` no reintentable. El core,
  PostgreSQL y la API de Ads estaban activos; `health-ads` terminó con exit 0.
- Reabrir únicamente la aplicación permitió entrar al chat y al onboarding de
  Anuncios, sin reinstalar, borrar, restaurar cuentas ni recrear contenedores.
- La causa funcional del MCP era `net.ipv4.ip_forward=0` en el namespace de red
  del core. `mcp-remote` agotaba la conexión TCP desde su namespace aislado;
  la salud del core seguía pasando porque no atraviesa ese encaminamiento.
  La escritura desde dentro no funcionaba porque `/proc/sys` es de solo lectura.
- Se verificaron identidad, PID y namespace del contenedor y se activó el
  encaminamiento exclusivamente allí, desde el administrador de la VM. El
  valor de la VM antes/después permaneció en 1; no se cambiaron capacidades,
  montajes, permisos de credenciales ni reglas de aprobación.
- Después, `verify-ads` terminó con exit 0 en 0,47 segundos. `ListMcpServers`
  confirmó `safent-ads` sano con **64 herramientas**; el diario del runtime
  confirmó el registro de las mismas 64 herramientas.
- La corrección aplicada a ese namespace es temporal hasta recrear el core;
  la versión 0.9.21 debe configurar el sysctl durante su creación y verificar
  el valor efectivo. El proceso de empaquetado/publicación aún está pendiente.

Pruebas ejecutadas antes de integrar el cambio persistente de encaminamiento:
33 de recarga/consumidor y 63 del CLI de instalación/reparación aprobadas. La
regresión de recarga usa un reloj virtual: reprodujo el fallo con el límite
anterior de 15 segundos sin esperar minutos reales. El nuevo límite total
incluye conexión e introspección D-Bus y conserva la verificación autenticada.

No se conectaron cuentas externas ni se publicaron anuncios o cambiaron
presupuestos en esta reparación. Al tratarse de una instalación limpia, las
cuentas y el modelo aún requieren su configuración desde la UI. El respaldo
privado de la desinstalación anterior permanece intacto.

## Corrección persistente y controles de aislamiento

El commit `ddc5260` declara `net.ipv4.ip_forward=1` al crear el core en ambos
launchers, antes del montaje de `/proc/sys` como solo lectura. La convergencia
comprueba el valor efectivo del kernel; no da por válida una declaración o un
`sysctl -w` que no haya surtido efecto. La adopción de contenedores antiguos
conserva las comprobaciones de identidad, volumen, puerto y red. Los errores
de recarga siguen siendo errores, pero permiten reintentar sin borrar datos.

El commit `5813fa4` fija también la interfaz de entrada `veth-hmcp-host` en
las excepciones FORWARD/NAT de Ads. Esto evita que una IP de origen MCP
suplantada desde otra interfaz aproveche el encaminamiento. OUTPUT, los
destinos/puertos autorizados y las políticas por defecto DROP no se amplían.

La misma restricción de interfaz se aplicó al core en ejecución con una
transacción `nft` comprobada antes con `--check`. Después de aplicarla:

- `verify-ads` volvió a terminar con exit 0.
- Desde el namespace MCP: TCP a Ads `10.201.0.10:8443` conectó.
- Desde el mismo namespace: PostgreSQL `10.201.0.11:5432`, WAN directa y DNS
  TCP directo agotaron el tiempo de conexión, como exige la jaula.
- Desde el namespace del navegador: Ads `10.201.0.10:8443` permaneció bloqueado.

Se probó además la creación con el Podman 6.1.1 realmente empaquetado: un
contenedor efímero de la misma imagen, sin red, raíz de solo lectura y sin
capacidades, recibió el sysctl en creación y leyó el valor efectivo 1. Se
retiró automáticamente al terminar; el sysctl de la VM no cambió.

Estos resultados comprueban la reparación y el aislamiento en el Mac, pero
no sustituyen una aceptación de instalación limpia del paquete firmado 0.9.21.

## Candidato integrado y regresiones (13 de septiembre, después de las 21:20 UTC)

El tag `v0.9.21` fija el código integrado en
`60c4c8468ad6ed15a77ce98c9b16c2543c6ea55d`. No se debe mover ese tag por
actualizaciones posteriores de este documento. El workflow de imagen es
`34783576034`; al registrar esta sección sigue en construcción. La versión
estable continúa siendo 0.9.20. El paquete nuevo debe permanecer como
candidato hasta completar su aceptación; no instalarlo automáticamente en
el Mac del propietario, que quiere comprobar él mismo el actualizador.

Regresiones completadas sobre los cambios integrados:

- 160 pruebas del CLI de instalación, convergencia y encaminamiento aprobadas.
- 17 negativos adicionales de adopción/convergencia aprobados, incluido el
  rechazo de configuración de red del host.
- 33 pruebas de recarga/consumidor y 43 de reglas de red relacionadas aprobadas.
- 514 pruebas del frontend, TypeScript y compilación Vite aprobados.

La UI de Community añade un acceso directo «Configurar conexiones» desde
Anuncios a Integraciones, sólo con los permisos correspondientes. No modifica
las autorizaciones Enterprise ni convierte una plataforma sin configurar en
una plataforma disponible.

## Conexiones: recuperación limitada y estado verificable

La instalación limpia no contenía la clave de Composio ni conexiones OAuth.
Se recuperó exclusivamente la clave previamente autorizada desde el respaldo
privado, se descifró en memoria y se guardó mediante la API de propietario de
Safent. No se restauraron la base de datos, la clave maestra ni cuentas antiguas.
La clave se cifra con la instalación actual. No incluirla en código, artefactos,
capturas ni registros.

- La API de Integraciones confirma Composio habilitado y Google Ads preparado.
- Desde la UI de Safent se inició Google OAuth para la cuenta `167-779-1325`.
  La aplicación muestra «Esperando a la plataforma» y el enlace de continuación.
  Esto **no** demuestra conexión terminada: falta el consentimiento, el retorno
  y la aparición de la cuenta en Safent.
- Meta sigue sin configuración OAuth personalizada. El App ID conocido es
  `1063816289878236`; todavía falta obtener de forma segura el App Secret y
  guardarlo mediante el formulario de Integraciones. El identificador de la
  cuenta publicitaria no sustituye esa configuración.
- El modelo de lenguaje tampoco está validado en esta instalación nueva.

No se han publicado campañas ni cambiado presupuestos. Antes de afirmar que
se puede operar, comprobar: conexión real, inventario de cuentas, permisos,
estado del anunciante, modelo y flujo propuesta/aprobación/ejecución. La cuenta
Google había mostrado una pausa por verificación del anunciante en una captura
anterior; revisar su estado actual después de conectar, sin darlo por resuelto.
