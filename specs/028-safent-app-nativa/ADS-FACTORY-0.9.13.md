# Safent 0.9.13 — convergencia de red del motor nativo

Estado: candidata en preparación; **no aceptada todavía en macOS**. Este corte no cierra el backlog global ni conecta cuentas de proveedores sin consentimiento.

## Defecto reproducido y alcance

El DMG 0.9.12 pasó CI/firma/notarización pero falló al actualizar el core en una red legacy con IPAM dinámico sin reservas: ocupó `.14`, necesaria para migraciones Ads. El Mac conserva ese estado como caso real de recuperación. No se borran redes/volúmenes ni se reparan contenedores manualmente.

El CLI empaquetado asigna al core `10.201.0.2`, fuera de las reservas Ads `.10`–`.14`. Antes de retirar el core verifica identidad inmutable, referencia/digest de imagen, volumen de datos y puerto loopback exacto; un ocupante ajeno de `.2` bloquea antes de modificar contenedores. Facts y convergencia exigen la topología correcta incluso cuando la imagen y health son correctos. La preparación converge el core antes de arrancar Compose, y después sólo revalida: no recrea un core inesperado durante el registro. Si falla scaffold, Ads obligatorio no permite el fallback que retiraba el core por nombre sin esas comprobaciones. El modo explícito `--no-companion` se mantiene.

No se modifica la red legacy, el esquema de volúmenes, Compose ni el modo de red de la migración. No se promete rollback transaccional ante cualquier fallo del sistema operativo.

## Paquete reproducible

- Native/CLI: nueva versión 0.9.13 y nuevo tag inmutable, por registrar tras pruebas.
- Core reutilizado: `v0.9.12`, source `e6cd08e120d99d93730912082906f34cf7a75371`, arm64 `sha256:d6d522e2046e751bcf46c945e31d83f60db1f3339c28f251e3f6c487c3a6d8a2`.
- Ads reutilizado: `v0.2.7`, source `a7c1fc8cc4c9b79f748880c0f91fe9b98d7c4e41`, arm64 `sha256:39eff335f003f50989b1ce59e347eb41479e4803c1ddd08058ad9601f03c7086`.
- La imagen OCI disparada por el tag 0.9.13 es independiente del pin del instalador; no esperar ni sustituir los pins verificados durante la build nativa.

## Evidencia y límites

- QA real Podman Linux aislada: core dinámico `.14` con una imagen → `.2` con otra imagen, puerto y centinelas core/DB preservados, cuatro servicios sin sustitución, migración `.14` arranca y reaperturas idempotentes. Ocupante ajeno `.2`: rechazo 78 sin cambiar IDs, PID, mounts, imagen o red. Evidencia DGX `/tmp/safent-core-network-qa.WPGrHY/results-final-v2.log`. Procesos inocuos y red QA: no sustituye arranque completo en macOS.
- CLI `d8fd82950afd40ed4452067482e8860b5f3a2632`: 43 pruebas de instalación y 78 de regresión aprobadas antes del refuerzo final; guard final 2 PASS y focal final de identidad/topología/proyección/scaffold 15 PASS. Orden independiente `89521ec`: 4 PASS. Sintaxis shell y diffcheck aprobados. Los archivos de tests conservan deuda previa Ruff; no se declara lint global verde. Detalles en `docs/core-companion-topology-2026-09-13.md`.
- Metadatos 0.9.13: 2 PASS tras corregir un bump local que inicialmente apuntó a una dependencia homónima de versión; la dependencia quedó intacta antes de compilar. Rust `cargo test --locked`: 153 + 58 + 43 ejecuciones aprobadas, cero fallos/ignoradas, `/tmp/safent-0913-rust-full.log` (tres targets, no 254 tests únicos).
- Baterías previas backend/UI, seguridad OAuth y primera configuración: `ADS-FACTORY-0.9.12.md` y `REVISION-GLOBAL-2026-09-13.md`; no se atribuyen retrospectivamente a los nuevos cambios CLI.

## Criterios de aceptación macOS

1. DMG final postnotarización: checksum, firma Apple, notarización, recursos y pins.
2. Sustituir sólo `/Applications/Safent.app`, conservando una copia y todos los datos. Producto recupera por sí mismo el core `.14` y prepara Ads 0.2.7, sin intervención Podman manual.
3. Comprobar contenido real de las imágenes, puerto conservado, core `.2`, cuatro roles Ads, migración terminada, DB sana y proyección de secretos inaccesible para UID 886.
4. Primera configuración Friendog Center por UI; Conexiones y cockpit vacíos honestos. No confundir negocio local con autorización de Meta/Google.
5. Diálogo de tarea: Escape y retorno de foco sin guardar. MCP disponible sin ejecutar cambios publicitarios.
6. Cerrar proceso y reabrir: mismos IDs/puerto, sin reinstalación/recreación.

## Revisión de experiencia (Emil Kowalski)

| Antes | Después | Motivo |
|---|---|---|
| Se podía aceptar health con una dirección que bloqueaba la siguiente migración. | Ready exige topología y pins reales. | El estado visible debe representar una instalación utilizable. |
| Scaffold fallido podía continuar retirando el core por nombre. | Error recuperable antes de retirarlo, conservando su estado. | Evitar agravar un fallo y dar un resultado predecible. |
| Convergencia del core después de arrancar Ads. | Convergencia previa dentro de la fase de preparación. | Progreso fiel al trabajo y dependencias resueltas en orden. |

## Acceso externo pendiente

Google Friendog requiere volver a iniciar sesión; Meta muestra login. Faltan configuración del cliente/app y consentimiento real; no se han leído secretos, creado campañas ni concedido presupuestos. Enterprise central todavía no monta las rutas de onboarding OAuth Ads: el OAuth de inventario Analytics/GTM no lo sustituye. El LLM tampoco está conectado en este Mac.
