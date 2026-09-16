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

## Resultado de aceptación real: NO aprobada

La construcción, firma y notarización terminaron correctamente, pero el arranque GUI sobre el estado existente del Mac falló en `companion_scaffold` con `companion_network_conflict`. Se ha marcado v0.9.8 como prerelease; no es una entrega estable. El core arrancó con su digest correcto, mientras Ads permaneció en 0.2.2. No se borró estado ni se reparó manualmente para ocultar el fallo.

La causa se reprodujo con un volumen QA efímero del Podman privado incluido: la segunda llamada a `volume create` devuelve 125 si el volumen ya existe. `publish_runtime_projection()` la ejecutaba incondicionalmente con `set -e`. Los dobles de prueba no reproducían esa diferencia respecto de Docker. La corrección y las pruebas de reejecución real pertenecen a la siguiente versión, no a esta etiqueta inmutable.

El DMG descargado, de 1.016.789.839 bytes, coincide con SHA-256 `aec43d477d0a29e7d65b1106cc34fafd154950017af692da631edeb278fb97d0`. `codesign --verify --deep --strict`, `spctl` (Notarized Developer ID), `stapler validate` y los pins del bundle pasaron. La consulta del actualizador nativo por GUI respondió «No hay una versión más reciente» para 0.9.8; no se ha probado una instalación entre versiones mediante el actualizador.

Pendiente: arranque GUI con Ads 0.2.4, panel integrado sin segundo login y reapertura sin reinstalación sobre la versión corregida. La estabilidad de puerto/CID se exige entre reaperturas de la misma versión; una actualización de versión puede escoger otro puerto local.

## Accesos externos

Google y Meta siguen sin conexión real: se requiere iniciar sesión con Friendog y otorgar el consentimiento correspondiente. La cuenta Google `arturo.soria@friendog.market` exige reautenticación y su CLI devuelve `invalid_grant`; la sesión accesible de otra cuenta no se utiliza como sustituto. Meta pide login en la superficie de Friendog. No se han modificado campañas ni presupuestos ni copiado secretos del navegador.

La app instalada tampoco tiene un modelo LLM conectado. No se afirma que el agente haya realizado tareas autónomas o llamadas reales a proveedores.

## Construcción y seguimiento del artefacto

- Runtime `40e88627b372c7591ab32642266ecd1991fe0732`, tag v0.9.8, workflow `34735610448` aprobado. Índice OCI `sha256:3db9890f3f64d527225298f3f9bc2345623b59d0aa4fd000f2e3450a931d81d2`; arm64 `sha256:fae3dd4fff5752ead81d73df09f1b72c64bc1f5181c39da57b7685cf1be2e41d`. Ambas arquitecturas tienen etiquetas OCI de revisión y versión concordantes.
- Ads `340d93f72b6fa2a147f0a5cb29de19ade701dd00`, tag v0.2.4, workflow `34735283876` aprobado. Índice OCI `sha256:a9c01c97a6547ca2fc103d3e4fd75bc4375df1bd656805c973cd2794224b105b`; arm64 `sha256:7e2f3f4d3bc9cb22e128b24e57b48893add0c5ff935907ce4a831ae86d4a6d61`. Tag SHA y tag de versión resuelven el mismo índice.
- Desktop `34736032220`, source pipeline `ff5e6d1f`: el primer intento se detuvo en preflight porque LIST no reflejaba todavía el borrador recién creado. Se comprobó por ID el borrador único `387783277`, tag/SHA exactos y sin archivos, y se reintentó únicamente lo fallido. El segundo intento completó todas las plataformas y gates sobre ese mismo borrador, con 15 assets y sus 14 sumas verificadas. Esto no sustituye la aceptación GUI, que falló como se describe arriba.
- Prevención adicional de consistencia eventual en pipeline, commit `d7d7a4ed`: tras un único POST, verificar el ID directamente y releer LIST hasta cinco veces con esperas 1/2/4/8 s sólo ante ausencia. Nunca repetir POST ni tolerar duplicados, publicación, cambio de SHA/ID o error HTTP. Suite completa: 89 aprobadas y 3 skips de herramientas Apple en Linux. Esta prevención se commitea aparte del pipeline utilizado por el build en curso.
- Acceso anónimo verificado independientemente para ambas imágenes: token público de pull, índices y manifiestos arm64, config blobs GET 200 y HEAD de una capa real HTTP 200. Sin PAT, cookies, proxy del entorno ni credenciales Docker. No se descargaron capas completas en esta prueba, por lo que no se presenta como instalación integral anónima de una máquina vacía.
