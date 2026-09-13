# Configuración guiada de Meta y Google con Composio

## Decisión de producto

La conexión de Anuncios en Community nativo utiliza la integración Composio de
Safent. El usuario prepara la conexión desde la UI; no se le pide ejecutar
comandos, compartir secretos por chat ni entender puertos locales.

Composio ofrece herramientas Meta Ads y OAuth2 con aplicación propia. La guía
pide App ID y App Secret juntos, explica dónde conseguirlos y proporciona
botones a Meta. No afirma que Composio aporte una aplicación Meta gestionada.

## Implementado

- Runtime `8e97801`: comando nativo `open_ads_setup`, destinos cerrados, validación
  de ventana y origen, apertura del navegador del sistema. OAuth no se amplía.
- Runtime `972e018`: navegación del iframe de Anuncios a la guía de Integraciones,
  comprobando origen, ventana del iframe, proveedor y permiso actual.
- Runtime `894433d`: guía Google/Meta en
  `/capacidades?tab=integraciones&ads_setup=meta` (o `google`).
- Runtime `2d8c57a`: actualiza la capacidad del iframe cuando los permisos llegan
  después de cargar el panel o se revocan.
- Ads `a9c13d6`: CTA «Configurar Meta paso a paso» en el bloqueo real; el host guiado
  no monta los formularios de conexión directa, el retorno local ni la vía de
  token manual. Google configurado conserva su conexión y su botón de autorización.

La guía reutiliza la clave Composio ya guardada. Si falta, primero explica cómo
obtenerla desde Composio. Si la configuración de una plataforma ya existe, no
vuelve a pedir el secreto ni la reemplaza.

Para Meta, tras App ID/Secret, se muestra el retorno exacto que usa el backend:
`https://backend.composio.dev/api/v1/auth-apps/add`. La casilla confirma que el
usuario lo guardó en Meta; NO constituye validación externa. Cambiar App ID
desmarca la casilla. Guardar envía los datos a Composio mediante el backend.

Los enlaces a información básica e inicio de sesión se construyen sólo con el
App ID numérico introducido. Se mantiene copiar dirección como salida accesible
si el navegador o el portapapeles no se abren. No se embeben IDs de Friendog.

Los secretos no van a React state, URL, navegador storage ni mensajes de error;
los inputs se vacían antes del envío y al cancelar. Los envíos de configuración
tienen 60 s de plazo para admitir los límites del backend. Ante fallo hay una
comprobación de lectura que permite reconciliar un guardado sin repetirlo.

«Configuración preparada» NO significa «cuenta conectada». Después se vuelve a
`/anuncios?connect=meta` (o `google`), se enfoca el proveedor y el usuario inicia
OAuth explícitamente. No se publican campañas ni se cambian presupuestos.

## Verificación

- Runtime: 597 pruebas frontend completas y build TypeScript/Vite aprobados en
  `894433d`; tras `2d8c57a`, 56 pruebas focalizadas (34 AdsView + 22 enlaces).
- Nativo: 166 unitarias del binario, incluyendo 21 pruebas de window_policy;
  formato Rust aprobado.
- Ads: 419 pruebas en 48 archivos; ESLint, TypeScript y build Vite aprobados.
- Revisión independiente: encontró plazo insuficiente de requests y confirmación
  del retorno no ligada al App ID. Ambos corregidos con regresiones.
- Inspección de la guía en navegador usando el componente real y respuestas
  sintéticas, sin red de API real: campos simultáneos, enlaces derivados y cambio
  App A → App B desmarca confirmación. No se introdujeron secretos reales.

## Entrega y límites pendientes

- La app instalada 0.9.20 no contiene esta guía. 0.9.21 + Ads 0.2.14 es un candidato
  anterior; NO anunciarlo como solución de esta petición.
- Generar versiones nuevas 0.9.22 + Ads 0.2.15; nunca mover las etiquetas anteriores.
  Comprobar imágenes, firmas, notarización, artefactos y canal antes de promover.
- El consentimiento real y la prueba de lectura de cuentas Meta siguen pendientes
  de que el usuario complete la guía en la app distribuida. No afirmar que se
  pueden publicar campañas por el mero hecho de guardar la configuración.
- La UI web standalone conserva su configuración directa existente; esta entrega
  sustituye la configuración confusa en el panel incrustado Community autorizado.
- No reinstalar ni limpiar el Mac para validar este cambio sin indicación del
  usuario. No se ha cambiado ninguna credencial o campaña en vivo en este trabajo.

Referencia de capacidades y requisitos: https://docs.composio.dev/toolkits/metaads
