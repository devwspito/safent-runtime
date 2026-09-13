# Safent 0.9.12 — revisión nativa y seguridad OAuth

Estado: candidata en preparación. No declarar aceptada hasta comprobar el DMG firmado y su arranque real. No equivale al cierre del backlog completo de Safent.

## Por qué se detuvo 0.9.11

La auditoría previa al uso de cuentas reales reprodujo una fuga de secretos ficticios: la llamada GET del intercambio OAuth Meta llevaba `client_secret` y `code` en la URL; HTTPX los emitía mediante logging estándar a INFO, fuera de la redacción de structlog. La reproducción fue offline, con MockTransport y canarios; no se usaron credenciales reales ni se contactó con Meta.

Se canceló el workflow nativo `34741776480`. La release `v0.9.11`, ID `387808735`, quedó como borrador prerelease con cero assets. No se movieron tags ni se reemplazaron artefactos. La imagen Runtime 0.9.11 ya había terminado su build, pero no se reutiliza para esta candidata. La corrección de logging debe superar pruebas y revisión independiente antes de publicar Ads 0.2.7.

## Contenido del corte

- Alta inicial humana del negocio Ads desde la UI, conservando el modelo de propietario único, CSRF, identidad de sesión y transacción contra duplicados. Rechaza propietario ambiguo, configuración gestionada y negocios ya existentes. No crea cuentas de proveedores ni permisos de gasto.
- Sesión fresca y negocio válido antes de montar consultas; recuperación de envío incierto mediante lectura, sin repetir automáticamente el alta. Cockpit vacío con la moneda elegida.
- Cliente Google de escritorio propuesto sólo en loopback; web en origen remoto. Sigue requiriendo configuración del cliente y consentimiento real. No certifica Meta HTTP loopback.
- Corrección acotada de logging OAuth para impedir que URLs o excepciones revelen credenciales, códigos o tokens. Mantener el contrato del proveedor; no cambiar GET por POST sin evidencia oficial.
- Runtime conserva las trazas HTTP y excluye query/fragmento/credenciales de Uvicorn y HTTPX; los diagnósticos de transporte opacos no se imprimen. Enterprise aplica la misma exclusión de query a todos sus callbacks y elimina el texto crudo del error de login, con commit independiente del paquete nativo.
- Diálogo de tareas: cierre con Escape, foco contenido y retorno al disparador, interacción protegida durante envío, sin animación innecesaria.

## Revisión UI con Emil Kowalski

| Antes | Después | Motivo |
|---|---|---|
| Primera entrada Ads sin negocio: selector vacío y error genérico. | Formulario inicial con nombre, moneda y zona; después Conexiones. | Ofrecer el siguiente paso real y no confundir falta de configuración con fallo de red. |
| Respuestas de sesión almacenables y rutas con negocio vacío/ajeno. | `no-store`, lectura de sesión y selección limitada a negocios de la sesión. | Evitar estado obsoleto y consultas inválidas. |
| Diálogo de tareas sin cierre con Escape ni retorno de foco. | Reutilización del diálogo accesible existente. | Comportamiento predecible de teclado, sin otro sistema de componentes. |

## Pruebas

- Desktop 0.9.12: `cargo test --locked` aprobado, 153 + 58 + 43 ejecuciones y cero fallos/ignoradas. Son tres targets, no 254 tests únicos. Log DGX `/tmp/safent-0912-rust-full.log`.
- Consistencia de las versiones 0.9.12: 2 PASS.
- Onboarding Ads previo al arreglo de logging: backend 3430 unitarias PASS y 47 focales/PostgreSQL PASS; panel 318 tests PASS, typecheck/build aprobados. UI revisada con fixture explícita a 1280/390 px, foco/teclado y movimiento reducido. Falta registrar nueva regresión de seguridad y aceptación nativa.
- Ads seguridad final `998ff6c19676337df66347f3243800198b3c5af4`: 3446 unitarias PASS, 56 focales PASS; Ruff/mypy aprobados. Canarios INFO/DEBUG, HTTPX/Uvicorn, handlers propios y excepciones; revisión independiente aprobada. Tag 0.2.7 `a7c1fc8cc4c9b79f748880c0f91fe9b98d7c4e41`, imagen workflow `34742346095` aprobado. Acceso anónimo confirmado; índice `sha256:18d016df9466fe0e419f1c694634554fdf3d48331b4ff6f899424ac8763e0849`, arm64 `sha256:39eff335f003f50989b1ce59e347eb41479e4803c1ddd08058ad9601f03c7086`, amd64 `sha256:69020365178de195b555d35616848f7d4dde291b3254038fe039d6db8a988121`.
- Diálogo Community `c2687efd5451fcf2aa11c49b88f8980b6fb9c4cb`: tres regresiones rojo → verde, 362 pruebas frontend PASS y TypeScript/build aprobados. Evidencia en `docs/calendar-task-dialog-2026-09-13.md`.
- Runtime seguridad `f60802510ed163e49e722bba55eb4f71e2f47239`: batería completa sobre el snapshot inicial del parche, **7502 PASS, 0 FAIL, 21 SKIP, 250 exclusiones opt-in**, 439.04 s. Dos refuerzos finales (excepciones/stack y reason phrase remota) se certifican separadamente con **15 focales PASS**; no se atribuye retrospectivamente la full a esos hunks. Ruff aprobado. Mypy detecta una anotación `list` genérica preexistente, conservada como límite. Evidencia exacta en `docs/core-http-log-redaction-2026-09-13.md`.
- Enterprise logging `c0a2b5ae552ca172d9be8e65983395964c7ee0fe`: 5 FAIL/1 PASS antes → 19 focales PASS después; revisión independiente con Uvicorn/importación app reales. Typecheck con el comando de CI aprueba 89 módulos; ejecución sin ignorar imports falla por stubs ausentes. No desplegado en cloud.
- Baterías globales conservadas: Runtime 7493 PASS, 21 SKIP, 250 exclusiones opt-in; Enterprise 1681 backend PASS, 11 SKIP, frontend 332 PASS. Límites y evidencia en `REVISION-GLOBAL-2026-09-13.md`.

## Aceptación real pendiente

1. Registrar commits, digests inmutables y workflow: Desktop/core 0.9.12 y Ads 0.2.7.
2. Verificar SHA del DMG descargado, firma Apple, notarización, recursos y pins.
3. Salir realmente del proceso nativo anterior, no sólo cerrar la ventana. Reemplazar únicamente la app, conservando una copia y todos los datos de `~/.safent`.
4. Dejar que la app haga la actualización por sí misma. Verificar imágenes por contenido, DB/worker/API/broker y migración exitosa, volumen de datos y proyección privada; no aceptar Ready o el marcador `image` como prueba suficiente.
5. Crear Friendog Center desde la UI, comprobar Conexiones y cockpit vacío, probar Escape/foco sin guardar tareas y reabrir sin recreaciones.
6. Handshake MCP y herramientas disponibles; secretos ilegibles para UID del agente. No ejecutar operaciones publicitarias reales.

El mecanismo inspeccionado detecta el cambio de digest Ads aun con el core sano: exige coincidencia de todos los roles y salud autenticada antes de Ready. No ofrece rollback transaccional automático. Esta candidata cambia también el core para incluir el arreglo del diálogo; su aceptación no se presentará como una prueba real de actualización exclusiva de Ads.

## Bloqueos externos y límites

Google permanece en «Demuestra que eres tú» para la cuenta Friendog; Meta requiere login. Faltan consentimiento y configuración de clientes OAuth. El modelo LLM tampoco está conectado. No se afirma que OAuth Ads otorgue Cloud/IAM/GTM/Analytics ni que toda operación de Meta esté certificada en loopback. No se han creado ni cambiado campañas, presupuestos o concesiones externas.

La aceptación cloud Enterprise, proveedores reales y demás pendientes de producto siguen separados en el informe global. No reiniciar un cron ni borrar datos del Mac para ocultar errores de instalación.
