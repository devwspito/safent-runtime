# Una configuración de Composio para Integraciones y Anuncios

## Decisión de producto

Community guarda la clave de Composio una vez, en Integraciones. El módulo
Anuncios consume esa misma configuración. No requiere una segunda copia manual
en `broker.env`, en el panel Ads, ni dentro del DMG. Esto no convierte una conexión
Gmail en autorización Google Ads: cada cuenta publicitaria necesita consentimiento.

En la comprobación real anterior al cambio, Integraciones tenía una conexión
ACTIVE de Gmail y ninguna de Google Ads o Meta Ads. No se ha importado ni concedido
acceso a otras cuentas del proyecto Composio.

## Implementación

- Se corrige el contrato REST de conexiones: `toolkit_slug` no es el `slug` de
  las fichas del catálogo. UI y ContextPanel conservan ID de conexión, estado y
  múltiples conexiones de una plataforma; sólo ACTIVE aparece como conectado.
- Google/Meta se operan desde Anuncios, conservando negocio, jaula y aprobación.
  Sus herramientas Composio genéricas siguen bloqueadas fuera de ese módulo.
- Un método D-Bus de propósito fijo publica la configuración del vault al
  companion local validado. No acepta destinos, claves receptoras ni claims del
  llamante y devuelve únicamente una aceptación booleana.
- El transporte reutiliza TLS con CA fijada y sesión SSO. La API Ads sólo
  retransmite un sobre cifrado por su socket Unix; únicamente el broker lo abre.
- El instalador deriva la identidad pública del broker usando su clave privada
  únicamente por stdin de un proceso aislado, sin red. Proyecta el resultado en
  `/etc/hermes/companions/ads-composio-channel.pub`, propiedad de root y modo 0444.
  El publicador verifica esta identidad local antes de leer el vault: la API Ads
  no puede sustituir la clave receptora. Una proyección vacía, alterada o ausente
  bloquea la publicación sin exponer credenciales.
- X25519 + HKDF-SHA256 + AES-GCM protegen el sobre. Una firma Ed25519 con audiencia
  y propósito propios acredita al runtime. Sólo se proyecta al broker la clave
  PÚBLICA del emisor; nunca su clave privada.
- La configuración descifrada vive sólo en memoria durante un máximo de 90 s.
  Se renueva cada 20 s y se solicita renovación al guardar/cambiar la configuración
  o iniciar OAuth. Cambios durante llamadas ya iniciadas no prometen revocación
  retroactiva; un fallo del canal no extiende una concesión caducada.
- Revisión monotónica e identidad de instalación impiden replay y rollback.
  El broker sólo persiste esos metadatos, no otra copia de la clave Composio.
- En modo companion no se recupera una antigua clave de entorno si la concesión
  falta, caduca o se desactiva. Los adaptadores mantienen su identidad: renovar
  la clave no reinicia contadores, límites ni presupuestos de la jaula.
- Las instancias administradas por Enterprise no exportan su configuración local
  a un destino empresarial. Se conserva la política existente de Ads administrado.
- Los endpoints internos nunca se publican por el puente `/ads` del navegador;
  se rechazan también las variantes con codificación o traversal.

## Preparación de Google y Meta

El publicador puede resolver y guardar la configuración OAuth gestionada de
Google usando la clave ya guardada. Antes de crearla consulta los metadatos del
proveedor. Una actualización concurrente de clave impide asociar el resultado
del proyecto anterior. También existe la acción owner-only
`POST /api/v1/integrations/composio/ads/prepare`, sin parámetro de clave.

Meta no tiene app OAuth gestionada por Composio en esta fecha. Debe prepararse
una app propia y seleccionar explícitamente su auth config en Integraciones.
No se elige automáticamente una app ajena de todo el proyecto.

La página de Composio para Google pide `Customer ID`, el número de 10 dígitos de
la cuenta publicitaria o MCC. No es un secreto de desarrollador ni un correo.
El panel explica este campo para los enlaces Composio; no lo inventa como
prerrequisito del OAuth directo de Google.

## Verificación y aceptación

- UI Community: 406 pruebas y compilación TypeScript/Vite correctas.
- Preparación compartida, SDK, propietario y provisionado: 190 pruebas iniciales;
  42 pruebas focales posteriores incluyen actualización inmediata del canal.
- Publicador: 226 pruebas de TLS real, cifrado, rotación, desactivación, política,
  SSO, DBus y puente. Interoperabilidad `_seal` runtime → consumidor Ads real.
- Panel Ads: 15 pruebas focales de inicio, errores, expiración y explicación del ID.
- Fijación de destinatario: 170 pruebas focales del runtime, 34 del broker/CLI
  y 98 de provisionado y base de datos fría. El CLI devuelve sólo la clave pública;
  los fallos de identidad impiden descifrar el vault y enviar la concesión.

Versiones candidatas preparadas: runtime 0.9.17 y Ads 0.2.12. La publicación y
la validación del binario instalado siguen pendientes al escribir esta nota.

Estos resultados son pruebas de código, no aceptación de cuentas publicitarias.
Antes de declarar el producto operativo debe publicarse e instalarse el binario,
comprobar la concesión activa sin dependencia de la antigua clave de entorno,
completar OAuth y verificar inventario real. No se han activado campañas ni gasto.

Fuentes del proveedor:
[Google Ads](https://docs.composio.dev/toolkits/googleads) y
[Meta Ads](https://docs.composio.dev/toolkits/metaads).
