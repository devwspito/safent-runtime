# Safent 0.9.14 — alta Ads en la app nativa

Estado: en preparación. No declarar aceptada hasta repetir el alta real con el paquete firmado. Antecedentes verificables: `ADS-FACTORY-0.9.13.md`; pendientes de producto: `REVISION-GLOBAL-2026-09-13.md`.

## Correcciones

- El traductor del puente conserva `Secure` salvo cuando el request ASGI real es HTTP y su hostname es exactamente `127.0.0.1`, `localhost` o `::1`. No confía en `X-Forwarded-*` aportados por el cliente. La cookie CSRF mantiene Path `/ads`, SameSite y TTL; la cookie de sesión Ads permanece en el servidor. No se desactiva CSRF ni se repiten mutaciones rechazadas.
- Recargas SPA (`bbe7d37bed1604a3d2b64aeeb032a87272d49661`): raíz y diez rutas canónicas exactas sólo GET/HEAD. Mutaciones sobre SPA se rechazan antes de SSO/upstream; no hay comodines ni nuevas rutas login/MCP. Los detalles hijos de `campanas` no tienen contrato acotado y siguen rechazados en navegación HTTP directa (no cambia navegación cliente). HEAD conserva un eventual 405 del upstream; no se inventa éxito.
- Incluye diagnóstico CLI restore posterior al corte 0.9.13: fallo al reiniciar se propaga a su postcondición y mensaje de datos importados/motor no arrancado, sin falso éxito ni cambiar importación. `fcba3ef`, 52 focales PASS.
- Conserva la recuperación de red: core fijo `.2`, preflight de identidad y ocupantes, convergencia previa a Ads y volúmenes/puerto preservados.
- Diálogo de tareas en WebKit: Escape cerraba, pero devolvía el foco a «Actividad y encargos» porque un click Safari no enfoca el botón. Se guarda el disparador real `event.currentTarget`, incluido día de calendario; no se depende de `document.activeElement`. Sin guardar tareas ni añadir animación.

## Evidencia previa a empaquetar

CSRF `4a6d966f1854f0966dd27557428eaa117e682cea`: rojo con cookie Secure realista del upstream; **50 bridge PASS**, Ruff/diffcheck aprobados. Revisión independiente usa el middleware CSRF real de Ads y traductor Runtime con un jar HTTP: **7 PASS**, incluyendo POST legítimo desde cookie recibida sin plantarla manualmente, rechazo por token ausente/erróneo y sin excepción para HTTP público/lookalike/forwarded spoof. Harness `/tmp/safent-csrf-real-middleware.LHw6yg/probe.py`. No son prueba de negocio/DB ni de WebKit: esa aceptación sigue pendiente.

La suite completa más reciente sobre producto 0.9.13 + fixture corregida (`62a5f65`) acredita 7519 PASS/21 SKIP/250 exclusiones, no estos cambios nuevos. Registrar aquí las nuevas pruebas del corte sin trasladar cifras de otro snapshot.

Puente final cookie+SPA: **93 PASS**, Ruff/diffcheck aprobados. Calendar `e9245eb`: **5 focales PASS**, TypeScript/build aprobados en npm ci aislado; la instalación node_modules canónica incompleta no se utilizó para acreditar pruebas. Metadatos 0.9.14: **2 PASS**. Rust 0.9.14 `cargo test --locked`: **153 + 58 + 43 ejecuciones**, cero fallos/ignoradas, `/tmp/safent-0914-rust-full.log` (tres targets, no 254 tests únicos).

## Imágenes y distribución

Nuevo core y native 0.9.14 necesarios; Ads mantiene 0.2.7 por digest. No se reutiliza el core 0.9.12/.13 porque no contiene la corrección HTTP. No mover tags ni reemplazar artefactos anteriores; prerelease hasta aceptación.

La OCI 0.9.13 independiente terminó correctamente (`34744745986`, source ed41934, índice `sha256:6c38abc3d27f41bc86cc937a50f715078a2ff1d4d1f167dc067052ba470df3c9`). Acceso anónimo/configuración/manifest/capa comprobados en amd64 y arm64. No cambia los pins del DMG 0.9.13 ni sustituye el core 0.9.14 necesario.

## Aceptación macOS pendiente

1. SHA/firma/notarización/manifest/pins, reemplazando sólo app y conservando datos.
2. Arranque automático con core 0.9.14 + Ads 0.2.7; puerto/volúmenes, roles/DB/MCP/proyección privada.
3. GET autenticado obtiene cookie CSRF utilizable en el origen nativo; crear Friendog Center una vez desde UI y comprobar una única alta, EUR/Europe-Madrid, paso a Conexiones y cockpit vacío.
4. Verificar Google Desktop predeterminado, Meta pendiente de sus credenciales/consentimiento, sin crear campañas ni permisos reales. No afirmar OAuth conectado.
5. Diálogo de tareas Escape/foco y reapertura sin recreaciones, conservando negocio/IDs/puerto. No probar automatizaciones con efectos reales mientras no haya proveedor/LLM conectado.

## Revisión de experiencia (Emil Kowalski)

| Antes | Después | Motivo |
|---|---|---|
| Primer formulario parece listo pero POST nativo recibe 403. | Cookie CSRF compatible con el transporte local y validación intacta. | La primera acción debe funcionar de extremo a extremo. |
| Navegación cliente a una ruta que no se puede recargar. | Rutas de navegación explícitas en el puente, sin abrir métodos mutadores. | Una recarga no debe romper el panel ni ampliar permisos. |
| Restore falla con diagnóstico oculto al reiniciar. | Mensaje y postcondición explícitos sobre el resultado parcial. | No ocultar qué se hizo y qué falta por recuperar. |

## Bloqueos externos

Google Friendog sigue en verificación de identidad; Facebook en login. Cliente/app OAuth y consentimiento aún pendientes. Meta HTTP loopback requiere validación de la app del proveedor; la documentación oficial consultada devuelve 429/no disponible. El modelo LLM no está conectado. Enterprise, CRM, Folder, operación multiinstancia y demás alcance histórico permanecen diferenciados en el informe global.
