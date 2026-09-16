# OAuth externo Community: cierre del callback

Estado: primitivas verificadas; no se concedió OAuth real ni se modificaron credenciales.

- GET/PUT de aplicaciones muestran el mismo callback que `reconnect/start`, incluido el origen/prefijo del puente Community. El exchange conserva el redirect ligado al state.
- Sólo los GET exactos de callback Google/Meta pueden atravesar `/ads` sin cookie. No hay mint SSO, cookie heredada, cabeceras del caller, redirect HTTP, reintento ni URL destino del caller. Destino/CA/IP proceden del registro confiable.
- State limitado y proveedor ligado, expirable y de un uso. PostgreSQL bloquea la fila de sesión durante completar: dos callbacks concurrentes no repiten el exchange ni sobrescriben éxito con error. Cancelación pasa a estado terminal sin exchange.
- Se aceptan HTTP loopback y HTTPS. Un despliegue con terminación TLS debe establecer el scheme ASGI mediante su proxy de confianza; el puente no confía directamente en `X-Forwarded-Proto` del caller.
- RFC6749: extensiones de respuesta se ignoran; sólo state/code o error normalizado llegan al companion. Query acotada, campos de autoridad sin duplicados, sin query en scope al responder, HTML estático y no-store.
- Tauri abre únicamente endpoints oficiales Google/Meta con callback al origen vivo y proveedor exactos, siempre deniega popup embebido. Navegación sólo al origen autorizado o al origen Tauri exacto de la plataforma. Fallos muestran diálogo nativo sin URL/state.
- macOS usa `/usr/bin/open --`; Linux `/usr/bin/xdg-open`, sin shell ni búsqueda PATH. Windows no tiene opener en este corte y muestra error explícito, nunca éxito fingido.

## Evidencia

Runtime canónico DGX: `tests/unit/shell_server/test_ads_bridge.py`: **41 PASS**; `cargo test --locked window_policy::tests`: **9 PASS**; `cargo fmt --check` y `cargo clippy --locked -- -D warnings`: PASS; ruff focal PASS.

Ads scratch `/tmp/safent-oauth-pg.XAv5yW`, Python `/home/luiscorrea-dev/Desktop/safent-ads/.venv/bin/python`, `PYTHONPATH=src DOCKER_HOST=unix:///var/run/docker.sock TESTCONTAINERS_RYUK_DISABLED=true`: **24 PASS** en unit `test_complete_oauth_connect.py` y PostgreSQL real `test_connections_router.py` + `test_platform_apps_router.py`, con broker Unix/encrypted store real y HTTP proveedor simulado. Última pasada incluye fuentes Google Desktop del otro carril. Ruff focal PASS. El primer intento no llegó a PG porque el socket Podman anterior ya no existía; no se alteró ningún servicio y se usó Docker disponible para contenedores efímeros.

## Límites de conexión reales

Google Desktop usa loopback y PKCE con secreto opcional: https://developers.google.com/identity/protocols/oauth2/native-app . El carril paralelo añade client_type explícito; Web mantiene secreto requerido. No concede acceso a Google Cloud/Tag Manager: el scope Ads no implica esos permisos.

La excepción callback no certifica que Meta acepte localhost HTTP/puerto dinámico para esta app. Requiere verificar la configuración y redirect admitido del cliente real. No se distribuye ni debe distribuirse un secreto maestro compartido de Meta dentro de Community. No se implementó relay nuevo ni se afirmó Login for Business configurado.

No prueba live de navegador/proveedor, consentimiento, tokens ni cuentas publicitarias. La readiness actual observada no tenía credenciales vendor configuradas. RFC6749 §4.1.2: https://www.rfc-editor.org/rfc/rfc6749#section-4.1.2 .
