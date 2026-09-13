# Safent Desktop 0.9.11 — primera configuración de Anuncios

Estado: en preparación, no aceptado hasta verificar el artefacto firmado en la app real. La candidata 0.9.10 corrigió y probó la recuperación de red, pero descubrió una carencia funcional de primera entrada: no había forma de crear el primer negocio desde UI/API.

## Cambio acotado

Ads añade una alta inicial humana (`POST /api/v1/onboarding/business`) con nombre, zona horaria y moneda de referencia. Conserva el modelo existente de propietario único: identidad desde sesión, CSRF, rechazo de propietario ambiguo y modo gestionado central. La transacción bloquea las tablas de metadatos durante la comprobación/alta, evitando duplicados y cambios concurrentes del propietario. Si ya hay negocio devuelve conflicto; la UI consulta de nuevo la sesión antes de repetir una petición incierta. No se añaden tablas de receipts, semillas de cuentas, credenciales ni permisos de gasto.

La UI bloquea las rutas dependientes de negocio hasta tener sesión/configuración válida y ofrece un formulario compacto de primera entrada. Después refresca la sesión y va a Conexiones. El filtro de negocio sólo acepta IDs presentes en `/auth/me`; respuestas HTTP realmente fallidas siguen siendo errores, no listas vacías falsas. `/auth/me` no se almacena en caché HTTP.

El cockpit vacío toma la moneda del negocio en vez de forzar EUR. En orígenes loopback, Google propone cliente OAuth de escritorio; los orígenes web remotos conservan cliente web. El usuario puede elegir otro tipo. No se cambia el transporte OAuth ni se afirma que Meta loopback esté certificado.

## Revisión UI con Emil Kowalski

| Antes | Después | Motivo |
|---|---|---|
| Selector vacío y «No se ha podido cargar» cuando aún no existe negocio. | Alta inicial con campos etiquetados, foco y estados de envío/recuperación. | Distinguir falta de configuración de un error de red y ofrecer el siguiente paso real. |
| Rutas montadas con `business_id` vacío o ajeno a la sesión. | Esperar sesión y seleccionar sólo un negocio autorizado por su respuesta. | Evitar consultas huérfanas y estados engañosos. |
| Cliente Google web por defecto también en loopback de puerto dinámico. | Escritorio por defecto en loopback, web en origen remoto; elección editable. | Ajustar la configuración propuesta al entorno sin ampliar permisos. |

## Pruebas y versiones

Paquete previsto: Desktop 0.9.11 + motor 0.9.9 + Ads 0.2.6 por digests inmutables. Son versiones independientes deliberadamente; conservar el motor evita cambiarlo para una mejora exclusiva de Ads. El artefacto y su aceptación real aún están pendientes.

- Ads: UI `d0af1ca45501b8fc7ade69370aeef832df7817ec`, backend `1c4d194619c22ef220bee3320e29aa0c9ee1a047`, versión/tag `v0.2.6` en `459b5293f6d420fb243f40d6a778b9cbd3073db2`.
- Backend Ads: 3430 pruebas unitarias PASS, sin skip; 47 pruebas focales/integración con PostgreSQL real PASS. Ruff y mypy PASS.
- Panel Ads: 318 pruebas en 46 archivos PASS; typecheck y build PASS. Revisión Chrome a 1280 y 390 px, movimiento reducido, foco y teclado PASS mediante fixture explícita. Esto no sustituye la aceptación nativa.
- Desktop 0.9.11: `cargo test --locked` PASS: 153 + 58 + 43 ejecuciones, cero fallos/ignoradas. Son ejecuciones de tres targets, no 254 pruebas únicas. Log DGX `/tmp/safent-0911-rust-full.log`.
- Consistencia de versiones: dos pruebas PASS antes de publicar.

Los cortes globales anteriores ya documentados: Runtime base 7493 PASS, 21 SKIP y 250 exclusiones opt-in; Enterprise backend 1681 PASS, 11 SKIP y frontend 332 PASS. No equivalen a certificación cloud, proveedores reales o todas las plataformas.

## Aceptación real pendiente

Verificar SHA del DMG descargado, firma/notarización/recursos/pins; reemplazar únicamente la app conservando datos; dejar que prepare Ads 0.2.6 por sí misma; crear el negocio desde su UI; abrir cockpit y Conexiones sin errores de configuración ausente; comprobar MCP, permisos de archivos, salud y reapertura estable. Sin SQL manual ni campañas de prueba reales.

Google/Meta todavía requieren el inicio de sesión del propietario y consentimiento. No hay cliente OAuth Google ni app Meta configurados en el Mac. El modelo LLM tampoco está conectado. La revisión global sigue en `REVISION-GLOBAL-2026-09-13.md` y no se declara completado todo el backlog histórico.
