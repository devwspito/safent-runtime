# Safent Desktop 0.9.10 — red de Ads y progreso real

Estado: en preparación. No es una versión aceptada hasta completar la prueba GUI del artefacto final. Las versiones anteriores 0.9.7–0.9.9 permanecen candidatas por fallos funcionales reales, aunque su CI y firmas aprobaran.

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

## Aceptación final — pendiente

Registrar fuente exacta, workflow, DMG postnotarización y hashes; instalación preservando datos; recuperación automática de la colisión; imágenes por contenido, salud/PostgreSQL/MCP, panel integrado sin login extra y reapertura estable. Google y Meta requieren login/consentimiento de Friendog, que siguen pendientes. No declarar operación de campañas reales ni cierre global de Enterprise/CRM/conocimiento.
