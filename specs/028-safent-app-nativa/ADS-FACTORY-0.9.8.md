# Safent 0.9.8 / Ads 0.2.4 — aceptación funcional del paquete nativo

Esta versión corrige dos fallos detectados en la GUI real de 0.9.7; no basta con construir y firmar archivos para declararla terminada. Ver `ADS-FACTORY-0.9.7.md` para la evidencia anterior y el rescate de su publicación.

## Cambios

- La comprobación nativa exige que los contenedores Ads en ejecución correspondan a los IDs de contenido de las imágenes fijadas, incluido PostgreSQL. No considera convergencia tener la imagen nueva descargada y un API antiguo saludable. El inventario no sobrescribe el pin deseado con el persistido.
- El HTML de Ads usa metadata no ejecutable para el prefijo `/ads`, y assets con rutas explícitas. No se rebaja la CSP ni se usan scripts inline o `<base>` prohibidos. El router y las llamadas API permanecen en el proxy autenticado; no se expone la cookie de sesión interna al navegador.
- Las compilaciones OCI de Ads se separan en runners nativos amd64/arm64 con caché. El índice versionado se publica sólo tras terminar ambas arquitecturas, desde tags internos del mismo run. No depende de emulación QEMU ni mezcla imágenes de ejecuciones diferentes. Reintentar un job fallido conserva la otra arquitectura correcta del mismo source.
- El pipeline desktop resuelve un único borrador antes de la matriz y pasa su ID a todas las plataformas; no repite la carrera de 0.9.7. Código en `agents-autonomy` commit `ff5e6d1f`.

## Validación previa a publicación

Ads: **3416/3416 pruebas unitarias backend aprobadas**, sin skips, sobre commit `340d93f` (incluidas las 8 de HTML/CSP; no sumar conjuntos solapados). 295/295 pruebas de panel con `npm ci` y el lock real (React Router 7), typecheck/build y mypy/ruff aprobados. Los mocks no equivalen a cuentas Google/Meta conectadas. Una revisión independiente de Chrome confirmó el bundle limpio `index-PMH5cwg5.js`, CSP literal, modo embebido y standalone, navegación y recarga profunda, a 1280 y 390 px. No hay violaciones CSP, errores JS ni desbordamiento; las llamadas permanecen en `/ads/api/v1` al estar embebido. Sesión y negocio de esa prueba de navegador son simulados, y los endpoints restantes devuelven 503 explícito, no resultados ficticios exitosos.

Guard de imágenes: 245 pruebas Rust aprobadas. La suite Python afectada terminó con 184 aprobadas y un fallo de fixture: un falso Podman no consumía la entrada de una tubería y provocaba SIGPIPE bajo pipefail. Se corrigió únicamente ese falso proceso, sin rebajar ninguna aserción del producto; el archivo completo de provisión se volvió a ejecutar con 79/79 aprobadas. No afirmar que hubo una única ejecución de 185/185. `sh -n` y `git diff --check` pasan; revisión independiente sin P0/P1/P2 accionable. Las comprobaciones de contenedores son observaciones secuenciales, no una transacción atómica frente a cambios manuales externos.

Pendiente antes de afirmar entrega: imágenes OCI exactas, DMG final firmado/notarizado, instalación sin limpiar `.safent`, arranque GUI con Ads 0.2.4 y panel sin segundo login, reapertura sin reinstalación y prueba de actualizador. Registrar resultados reales, no sustituirlos por reparaciones manuales ocultas.

## Accesos externos

Google y Meta siguen sin conexión real: se requiere iniciar sesión con Friendog y otorgar el consentimiento correspondiente. La cuenta Google `arturo.soria@friendog.market` exige reautenticación y su CLI devuelve `invalid_grant`; la sesión accesible de otra cuenta no se utiliza como sustituto. Meta pide login en la superficie de Friendog. No se han modificado campañas ni presupuestos ni copiado secretos del navegador.

La app instalada tampoco tiene un modelo LLM conectado. No se afirma que el agente haya realizado tareas autónomas o llamadas reales a proveedores.
