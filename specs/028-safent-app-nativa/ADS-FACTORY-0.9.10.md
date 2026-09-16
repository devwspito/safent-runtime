# Safent Desktop 0.9.10 — red de Ads y progreso real

Estado: recuperación del instalador y reapertura **aprobadas en GUI real**. Sigue candidata: la primera entrada a Ads con base vacía ha revelado que falta el alta del negocio en el producto. No se declara aceptado el recorrido funcional completo de Anuncios. Las versiones anteriores 0.9.7–0.9.9 permanecen candidatas por fallos funcionales reales, aunque su CI y firmas aprobaran.

## Corrección de red implementada

La prueba 0.9.9 reprodujo una colisión real: el IPAM dinámico asignó al broker la IP `10.201.0.10`, reservada sólo en la configuración de la API. La migración de datos terminó correctamente; lo que falló fue el arranque de la API. No confundir el código genérico de migración de la UI con la causa observada en el motor.

Diseño acordado: redes nuevas con pool dinámico `10.201.0.128/25`, fuera de direcciones de producto `.10–.14`; IPs fijas para los servicios Ads. La recuperación de redes antiguas sólo puede retirar ocupantes cruzados inequívocamente propios, validados por ID, labels de Compose, servicio y archivo de configuración. Conserva volúmenes, core y red. Un core personalizado o recurso ajeno ocupando esas direcciones provoca un bloqueo explícito sin tocarlo. No se promete migración transparente universal de redes antiguas ni se implementa una máquina adicional de migración/journal.

Commit `53b16a3e14c5d38310fd16eba908d3e7abcdbec3`: 138 pruebas focales de red, provisión y CLI aprobadas; Ruff y sintaxis shell aprobados. La función exacta también pasó siete casos con `/bin/bash` 3.2 del Mac (doble de runtime, ninguna mutación real): inventario vacío, reservas correctas, contenedor no conectado, broker cruzado, ocupante ajeno, error de inspección y respuesta malformada. No se añade dependencia de Python/jq al instalador.

QA independiente con Podman 4.9.3/netavark real en DGX: pool nuevo asigna al core `.129` y permite las cinco IPs fijas; segunda ejecución conserva IDs y horas de arranque. En red legacy aislada se reprodujo el error IPAM con broker `.10`; la función productiva retiró sólo ese broker propio y permitió broker `.12` + API `.10`. Core `.9`, DB `.11` y marcador de volumen permanecieron intactos; repetición sin mutaciones. Ocupante ajeno, core personalizado, configuración de otra instalación y servicio desconocido devolvieron código 78 antes de modificar contenedores. Redes de QA `10.253.*`, nunca la instalación real `10.201.*`. Evidencia DGX: `/tmp/safent-network-real.OQ6tLj/results.log`.

Límite de esa QA: funciones productivas y reservas reales, no la aplicación Ads/Compose completa ni el Podman 6.1.1 del Mac. Pendiente aceptación GUI desde el estado actual del Mac, sin reparación manual oculta.

## Revisión del progreso nativo (Emil Kowalski)

| Before | After | Motivo |
|---|---|---|
| Preflight comenzaba pero nunca emitía su finalización; «Comprobando este equipo» quedaba activo junto a descargas posteriores. | Se emite `StageCompleted(Preflight)` una vez tras una observación y validación inicial satisfactorias. | El estado visible refleja el trabajo real; evita sugerir un bucle o comprobación interminable. |
| El test de lifecycle añadía a mano una finalización que el emisor real no generaba. | Regresión del emisor y reproducción de eventos reales en DOM. | Probar la integración del progreso, no una secuencia idealizada. |

Arreglo commit `bb168900d2ae9cdc91869386a50999018490217d`: no completa fases con error, no elimina concurrencia válida y no cambia la autoridad de `Ready`. 16 tests Rust y 36 tests renderer/lifecycle aprobados; typecheck, build y clippy aprobados.

El commit `d378021e1b3ee77fa32c52f90a6c485a283a5a61` evita confundir un error IPAM inequívoco con una migración fallida: sólo reclasifica durante la fase activa de preparación/arranque Ads, con patrón completo y mensaje fijo sin IP ni ID privados. Los errores de base de datos y de otras fases no cambian. Rust completo: 153 + 58 + 43 ejecuciones aprobadas, sin fallos ni skips; clippy aprobado. Son ejecuciones por binario, no 254 tests únicos.

## Versiones independientes

Está probado que Desktop 0.9.10 puede usar core 0.9.9 por digest: `BootService` mantiene la versión nativa separada de la imagen deseada y reconcile no exige igualdad con la versión informada por el core. El workflow soporta `engine_tag` y `companion_tag` independientes. Intención del paquete: core v0.9.9 + Ads v0.2.5, incluyendo los nuevos scripts de host dentro del bundle nativo.

Tags/core y Ads previos se mantienen inmutables. Workflows OCI ahora no actualizan `latest` automáticamente: Runtime `a20b9d1` (8 tests), Ads `6f1ca9c` (4 tests). La corrección de workflow no alteró imágenes ya publicadas ni sus tags.

## Paquete candidato

Fuente/tag inmutable: `a6a407d9bcbccaa994b0280c6ff2f4f0a2fc6b73`, `v0.9.10`. Workflow Desktop `34739742481`, pipeline `1f87bf34a580e9138f5574a7816d5f20cffc017e`; `engine_tag=v0.9.9`, `companion_tag=v0.2.5`, DMG Apple Silicon y paquetes Linux, sin PKG ni Windows. Candidata prerelease; no promoción automática a stable/latest.

La limpieza de QA independiente terminó: 21 contenedores, seis redes y un volumen exclusivamente de pruebas, verificados por IDs/etiqueta propios. Ningún recurso real afectado.

## Aceptación real del paquete

Workflow `34739742481` SUCCESS, release `387799193` prerelease, 15 assets. DMG final postnotarización `560626723`, 1.016.797.756 bytes, SHA256 `35aeeb73c3ea2bc2d1e2b62b4ce98b3132e4e18229da97199200eb4e82123054`. SHA de los bytes descargados comprobado en el Mac; `stapler validate`, `codesign --verify --deep --strict`, `spctl` (Notarized Developer ID), 28 recursos y pins verificados. Los ocho Mach-O firmados se verifican mediante el sello Apple, no contra sus hashes anteriores a la firma. Manifiesto minisign, checksums de publicación y tres plataformas de actualización también comprobados independientemente en DGX. No se promovió stable/latest.

Se reemplazó sólo `/Applications/Safent.app`, conservando la app 0.9.9 en `/Users/luiscorrea/.codex/tmp/safent-0910-release.P69Kui/Safent-0.9.9-original.app`. No se limpiaron datos, volúmenes ni VM, ni se ejecutó reparación manual previa. El arranque GUI retiró el broker propio cruzado, arrancó API/broker/worker/DB, aplicó la migración con salida 0 y llegó a `/app/chat`. «Comprobando este equipo» ya se marca como terminado.

Verificación posterior: core `aa1515e29e94f2a9d96a4e06ed11ab06e1417eccce1d67423c80b9dba0ae6009` conservado; puerto `127.0.0.1:43337` conservado; todos los contenedores ejecutan el contenido fijado por el bundle; DB saludable; reservas `.10–.13` correctas. Los volúmenes `safent-data` y `safent-companion-runtime` siguen montados y la proyección es de sólo lectura; UID agente 886 no puede leer los cuatro secretos de Ads comprobados. Compose recreó sus servicios cuando cambió su configuración, conservando volúmenes; no se confunde esto con la retirada selectiva inicial del broker.

MCP real: `initialize` y `tools/list` HTTP 200, servidor `ads-control` 0.2.5, **64 herramientas**, incluidas propuestas de campaña/presupuesto e informes. No se ejecutaron operaciones publicitarias. Bridge `/ads/` y `/ads/api/v1/auth/me` HTTP 200, sin login adicional ni scripts inline de prefijo. Cierre y reapertura GUI llegaron otra vez al chat; **todos los IDs y el puerto quedaron idénticos**, sin reinstalación.

## Defecto funcional encontrado al abrir Ads

La base recién instalada carece de negocio. SSO crea el propietario, pero no existe ruta de alta de negocios; `/auth/me` devuelve una lista vacía. La UI monta rutas con filtro vacío y presenta como error una consulta deshabilitada. Cockpit muestra «No se ha podido cargar», selector de negocio vacío y estado de freno/frescura sin verificar; Conexiones sí muestra la configuración OAuth, pero tampoco puede operar cuentas sin negocio. Esto no es otro fallo de red ni de salud del servicio.

Se está preparando una alta inicial humana desde UI/API, limitada al modelo de propietario único existente; sin SQL manual, negocio Friendog sembrado implícitamente, cuentas OAuth inventadas ni ampliación de permisos. Requiere nueva versión Ads y paquete con su digest verificado antes de cerrar aceptación funcional.

Google y Meta requieren login/consentimiento de Friendog, que siguen pendientes. No hay cliente Google ni app Meta configurados en esta instalación; tampoco modelo LLM conectado. No declarar operación de campañas reales ni cierre global de Enterprise/CRM/conocimiento.

Comprobación adicional real del actualizador antes del reemplazo: app instalada 0.9.9, botón «Buscar actualización de la app», pasó por «Buscando…» y devolvió «No hay una versión más reciente», sin instalar nada ni cambiar el estado de Ads. Es coherente con el canal estable aún en 0.9.5 y candidatas excluidas. Se acredita integración y consulta real, no descarga/instalación completa entre versiones.
