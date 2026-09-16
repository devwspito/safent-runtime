# Safent 0.9.14 — alta Ads en la app nativa

Estado: **aceptación nativa acotada aprobada** sobre el DMG firmado: actualización del motor, alta inicial Ads, recarga, teclado, consulta del actualizador y reapertura reales. No equivale a certificar OAuth, campañas, todo Enterprise ni el backlog completo. Antecedentes verificables: `ADS-FACTORY-0.9.13.md`; pendientes de producto: `REVISION-GLOBAL-2026-09-13.md`.

Source/tag final: `c3c91b5638b344d200ec39760f093cd9084a2115` / `v0.9.14`. Batería Python exacta: **7572 PASS, 0 FAIL, 21 SKIP, 250 exclusiones, 8 warnings**, 466.28 s; `/tmp/safent-python-c3c91b5-full.log`. Informe reproducible `docs/runtime-python-c3c91b5-2026-09-13.md` (`8e44d8c`, sólo documentación). Calendar full final: **364 PASS / 55 archivos**, 3.44 s, mismo snapshot frontend que focal/typecheck/build.

## Correcciones

- El traductor del puente conserva `Secure` salvo cuando el request ASGI real es HTTP y su hostname es exactamente `127.0.0.1`, `localhost` o `::1`. No confía en `X-Forwarded-*` aportados por el cliente. La cookie CSRF mantiene Path `/ads`, SameSite y TTL; la cookie de sesión Ads permanece en el servidor. No se desactiva CSRF ni se repiten mutaciones rechazadas.
- Recargas SPA (`bbe7d37bed1604a3d2b64aeeb032a87272d49661`): raíz y diez rutas canónicas exactas sólo GET/HEAD. Mutaciones sobre SPA se rechazan antes de SSO/upstream; no hay comodines ni nuevas rutas login/MCP. Los detalles hijos de `campanas` no tienen contrato acotado y siguen rechazados en navegación HTTP directa (no cambia navegación cliente). HEAD conserva un eventual 405 del upstream; no se inventa éxito.
- Incluye diagnóstico CLI restore posterior al corte 0.9.13: fallo al reiniciar se propaga a su postcondición y mensaje de datos importados/motor no arrancado, sin falso éxito ni cambiar importación. `fcba3ef`, 52 focales PASS.
- Conserva la recuperación de red: core fijo `.2`, preflight de identidad y ocupantes, convergencia previa a Ads y volúmenes/puerto preservados.
- Diálogo de tareas en WebKit: Escape cerraba, pero devolvía el foco a «Actividad y encargos» porque un click Safari no enfoca el botón. Se guarda el disparador real `event.currentTarget`, incluido día de calendario; no se depende de `document.activeElement`. Sin guardar tareas ni añadir animación.

## Evidencia previa a empaquetar

CSRF `4a6d966f1854f0966dd27557428eaa117e682cea`: rojo con cookie Secure realista del upstream; **50 bridge PASS**, Ruff/diffcheck aprobados. Revisión independiente usa el middleware CSRF real de Ads y traductor Runtime con un jar HTTP: **7 PASS**, incluyendo POST legítimo desde cookie recibida sin plantarla manualmente, rechazo por token ausente/erróneo y sin excepción para HTTP público/lookalike/forwarded spoof. Harness `/tmp/safent-csrf-real-middleware.LHw6yg/probe.py`. No son prueba de negocio/DB ni de WebKit: esa aceptación sigue pendiente.

La suite histórica sobre producto 0.9.13 + fixture corregida (`62a5f65`) acredita 7519 PASS/21 SKIP/250 exclusiones, no los cambios posteriores. El corte final 0.9.14 tiene su propia batería de 7572 PASS indicada arriba; no se trasladan cifras entre snapshots.

Puente final cookie+SPA: **93 PASS**, Ruff/diffcheck aprobados. Calendar `e9245eb`: **5 focales PASS**, TypeScript/build aprobados en npm ci aislado; la instalación node_modules canónica incompleta no se utilizó para acreditar pruebas. Metadatos 0.9.14: **2 PASS**. Rust 0.9.14 `cargo test --locked`: **153 + 58 + 43 ejecuciones**, cero fallos/ignoradas, `/tmp/safent-0914-rust-full.log` (tres targets, no 254 tests únicos).

## Imágenes y distribución

Nuevo core y native 0.9.14 necesarios; Ads mantiene 0.2.7 por digest. No se reutiliza el core 0.9.12/.13 porque no contiene la corrección HTTP. No mover tags ni reemplazar artefactos anteriores; prerelease hasta aceptación.

OCI 0.9.14 `34746150601` aprobada: índice `sha256:616db381654e310c64a8b8ab65f2090cbd03a074762af3804c05cd710a7bce67`, amd64 `sha256:1e273961737459d7a298471adb1702dc8c9daaf64c219d4978f636436758c756`, arm64 `sha256:59a70a553f5c9a09424fb218e4119dc63d773697bbc1cf4a8dee471e2afb6232`. Source/config/plataformas y acceso anónimo verificados. Native workflow `34746708811` sobre pipeline `1f87bf34`: todos los jobs SUCCESS.

Release `387833819`, pública **prerelease**, stable/latest sigue 0.9.5. DMG final post-notarización `560850486`, 1.016.798.977 bytes, SHA256 `8e5de1a3de8b371981c80fcd67cca68ceb14d4d275b377b703527317954b0129`. Descarga: https://github.com/devwspito/safent-runtime/releases/download/v0.9.14/Safent_0.9.14_aarch64.dmg . Revisión independiente: 15 assets / 14 SUMS + SUMS, metadatos y hashes API/bytes coherentes, Minisign PASS, latest.json con tres plataformas y versiones/digests esperados. Evidencia `/tmp/safent-0914-manifests.ieGaoX` (Mac), `/tmp/safent-0914-manifests-ieGaoX` (DGX).

La OCI 0.9.13 independiente terminó correctamente (`34744745986`, source ed41934, índice `sha256:6c38abc3d27f41bc86cc937a50f715078a2ff1d4d1f167dc067052ba470df3c9`). Acceso anónimo/configuración/manifest/capa comprobados en amd64 y arm64. No cambia los pins del DMG 0.9.13 ni sustituye el core 0.9.14 necesario.

## Aceptación macOS ejecutada

1. SHA/tamaño idénticos al asset final, `xcrun stapler validate` PASS, Gatekeeper `accepted / Notarized Developer ID`, `codesign --verify --deep --strict` PASS. Manifest: 28 recursos verificados. Ocho Mach-O cambian hash tras firma Apple y se verifican individualmente y con el sello íntegro de la app; no se ignoran diferencias arbitrarias. Pins arm64 engine 0.9.14 y Ads 0.2.7 exactos.
2. Sólo se reemplazó `/Applications/Safent.app`; backup recuperable 0.9.13 en `/Users/luiscorrea/.codex/tmp/safent-0914-release.N8jDhS/Safent-0.9.13-original.app`. Sin borrar datos, volúmenes, VM ni reparar contenedores a mano. La propia app descargó y actualizó core 0.9.12 → 0.9.14 y llegó al chat con versión 0.9.14.
3. Core en `10.201.0.2`, mismo puerto `127.0.0.1:33015`, volumen `safent-data` y proyección `safent-companion-runtime` de sólo lectura. Caché de cuatro scripts idéntica al paquete. UID 886 no puede leer las cuatro credenciales privadas. DB healthy y migración exited 0. Los **cinco IDs Ads permanecen idénticos** durante esta actualización sólo del core; no se recreó Ads innecesariamente.
4. MCP real `initialize` y `tools/list`: HTTP 200, servidor ads-control 0.2.7, **64 herramientas**, incluidas propuestas de campaña/presupuesto e informes. Sin llamadas mutadoras. Canario sintético en query HTTP: no aparece en journal real `hermes-shell-server.service`, se conserva método/ruta/status.
5. Antes del alta: GET `/ads/cockpit` y `/ads/api/v1/auth/me` HTTP 200, business_count 0. Cookie CSRF en HTTP loopback: Secure false, Path /ads, SameSite Strict; `ads_session` no se reenvía. Desde **WebKit real**, se envió una sola vez Friendog Center con valores EUR/Europe-Madrid: abrió Conexiones sin el 403 anterior.
6. Lecturas autenticadas posteriores `auth/me` y `settings`: exactamente **un negocio**, nombre Friendog Center, timezone Europe/Madrid, currency EUR. «Recargar panel» vuelve al cockpit vacío y conserva negocio; no se afirma que conserve la subruta Conexiones. Tras salir de la app y reabrir, se repitieron las lecturas y la GUI conserva el mismo negocio.
7. Google muestra por defecto «Aplicación de escritorio (Safent nativo)». Google y Meta continúan `configured=false`; ambos botones Conectar están bloqueados hasta configurar cliente/app. Callback visible en UI usa el puerto 33015 correcto. El probe interno observa 7517 por su propio origen interno; no es el callback del navegador nativo.
8. «Nueva tarea»: foco inicial Nombre; Escape cierra y AX confirma foco devuelto a Nueva tarea. Desde día 14: apertura preselecciona 14/9/26 y Una vez; Escape cierra y Enter abre de nuevo ese mismo día, acreditando retorno de teclado aunque AX no imprima siempre el foco. Se cancelaron todos los diálogos; no se guardó ninguna tarea.
9. En reapertura se pulsó el botón real «Buscar actualización de la app»: primero «Buscando…», después **«No hay una versión más reciente»**. El actualizador sí está registrado/operativo para consulta. No se descargó ni instaló otra versión. Canal fijo estable de GitHub; .14 está por delante de stable .5. La instalación entre dos versiones firmadas sigue pendiente, no se certifica por una consulta ni por tests que nunca instalan.
10. Reapertura aprobada: mismo core ID `bf85331cb61704624c379c01a3c437336d74b3a3362896d4156569533569c735`, mismos cinco IDs Ads, mismo puerto/datos y negocio. Safent queda abierta en Anuncios → Conexiones. Modelo LLM aún sin configurar.

Helpers de verificación local de esta aceptación: `/Users/luiscorrea/.codex/tmp/safent-0914-release.N8jDhS/{verify_bundle,verify_live,check_mcp,check_live_http_logging,check_ads_cookie_attributes,check_business_readiness}.py`, con copias versionadas en `qa-native-0.9.14/` junto a este informe. Todos son probes de sólo lectura; la única alta se hizo expresamente en GUI. No extraen cookies del navegador ni imprimen tokens. Sus rutas/IDs corresponden a esta aceptación, no son configuración de ejecución de producto.

## Revisión de experiencia (Emil Kowalski)

| Antes | Después | Motivo |
|---|---|---|
| Primer formulario parece listo pero POST nativo recibe 403. | Cookie CSRF compatible con el transporte local y validación intacta. | La primera acción debe funcionar de extremo a extremo. |
| Navegación cliente a una ruta que no se puede recargar. | Rutas de navegación explícitas en el puente, sin abrir métodos mutadores. | Una recarga no debe romper el panel ni ampliar permisos. |
| Restore falla con diagnóstico oculto al reiniciar. | Mensaje y postcondición explícitos sobre el resultado parcial. | No ocultar qué se hizo y qué falta por recuperar. |
| Safari devuelve el foco al control equivocado al cerrar el diálogo. | Retorno al disparador real, verificado desde botón y día. | El teclado debe continuar donde empezó, sin saltos inesperados. |

## Bloqueos externos

Google Friendog sigue en verificación de identidad; Facebook en login. Cliente/app OAuth y consentimiento aún pendientes. Meta HTTP loopback requiere validación de la app del proveedor; la documentación oficial consultada devuelve 429/no disponible. El modelo LLM no está conectado. Enterprise, CRM, Folder, operación multiinstancia y demás alcance histórico permanecen diferenciados en el informe global.

También se probó la sesión CLI Google ya existente con `--account=arturo.soria@friendog.market --project=sylvan-plane-508309-q0`, sin cambiar la cuenta activa: una lectura de proyecto falla al refrescar con `invalid_grant`. No se usó otra identidad ni se creó un grant. Requiere login humano.

Lectura UI nativa sobre 0.9.13/core 0.9.12 mientras se construía la candidata: chat indica sin modelo y bloquea envío; Tareas vacías, aprobaciones activadas, sin MFA Community en ese panel; herramientas Excel 25/Word 54/PowerPoint 37/Ads 64; En vivo vacío porque no hay ejecución, no equivale a probar VNC real. Ads se muestra en Activas y Presets gestionados: duplicación visual pendiente de simplificar, no 128 herramientas distintas. La reapertura .14 y el alta sí se acreditan arriba por separado. La captura real de Conexiones muestra dos niveles de navegación y formularios de configuración; esta entrega no declara acabado el rediseño integral.

DMG usa arrastre estándar a Applications y no autoabre tras copiar. El gate de distribución valida identidad, APPL, icono, flags GUI y enlace Applications; aparición efectiva en Launchpad no se certificó en esta aceptación. No se reseteó Launch Services ni se alteraron índices del Mac.
