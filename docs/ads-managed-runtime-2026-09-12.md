# Ads administrado: política y transporte Runtime — 2026-09-12

## Resultado y frontera

Corte backend probado, **sin activar el despliegue managed ni declarar terminada su UI**.
El perfil central `ADS_MANAGED_CENTRAL` sigue false por defecto; no se han cambiado variables, credenciales, bases de datos ni infraestructura reales.

Enterprise publica `payload.ads` firmado. Community verifica el envelope completo contra la asociación activa, tenant, instancia, firma y versión. Omisión de Ads en una instancia asociada, corrupción, cambio de confianza, retirada de asociación o replay quedan cerrados antes del companion local.
La ausencia de asociación y de historial Ads en una instalación personal nunca asociada conserva el companion libre.

El binding Enterprise existente es el estado durable de administración:
- Nunca vinculada: publica `mode=free` explícito.
- Vinculada alguna vez: publica `mode=managed`; revocar todas las asignaciones produce `bindings=[]`, no free.
- No existe transición managed→free en este corte; incluso un bundle posterior free se rechaza tras managed. Una liberación futura requiere un flujo humano explícito separado.
- Crear un grant y publicar una política no equivale a que Community la haya recibido/aplicado. No se promete revocación de operaciones admitidas antes de ese punto; la autoridad EE vuelve a verificar cada acceso central y cada efecto.

## Contrato

`ads = {mode: free|managed, instance_id, central_origin: HTTPS origin|null, bindings: exact AdsBindingSpec[]}`.
Bindings llevan grant/revision, org/user/employee/instance, business/platform/connection/remote-account NUMÉRICO, resource_revision y techo fijo de capacidades.
Sin OAuth, pairing bearer, token de delegación ni assertion humana en la política.
El campo omitido sigue firmando byte-exacto para bundles históricos; no se reinterpretan como permiso managed.

Aplicación: `ApplyManagedAdsPolicy(envelope)` autorizado por D-Bus y verificado de nuevo en Runtime; SQLite `runtime_ads_policy` aditiva, una fila de envelope+fingerprint. Mantiene replay floor y no introduce migración destructiva. La sección del applier recibe el envelope firmado, nunca un objeto del cliente.
Cada llamada:
1. Selección explícita de grant_id entre bindings firmados; no primera conexión ni búsqueda por cuenta remota sola.
2. Bootstrap `POST EE /v1/ads/grants/token` mediante asociación existente. Validación local de firma, audiencia, TTL120 y claims exactos numéricos.
3. Nuevo token privado en `POST central /api/v1/managed/tools/{operation}`. Sin caché, proxy env, redirects, cookies, retries ni transporte de assertion humana.
4. Revalidación de asociación/política antes de enviar al central y antes de revelar la respuesta. La autoridad central comprueba además revocación/identidad actuales.
5. La aprobación sigue en EE: browser recibe únicamente preview y recibo, MFA allí; worker/broker conservan fresh admission y UNKNOWN.

HTTP: HTTPS443, origen firmado/servidor, paths fijos, argumentos≤32KiB, respuesta≤512KiB, deadlines7/8s. Errores saneados y bloqueo de eco literal de credenciales. No nuevo resolvedor DNS/proxy ni autorización de egress arbitrario por parámetros.

## MCP y jaula

Se añade un selector de cliente por slug a McpServerManager. Sólo `safent-ads` cambia; otros MCP conservan la factoría previa. Managed no inicia subprocess ni carga ADS_BEARER/CA/env local. Un cliente libre viejo queda invalidado al cambiar de modo; aplicación y reconexión renuevan el cliente managed sin volver al local.
Seed, autowire, SSO-owner y escritura de configuración nativa Ads rechazan managed. Los overrides managed_remote_endpoints NO conceden autoridad.
El cliente usa las mismas siete operaciones centrales (lecturas, pausa/presupuesto propuestos y revisión), más inventario de bindings firmados. Ese inventario no afirma acceso vigente.
Hallazgo corregido: McpTool descartaba inputSchema; ahora se conserva hasta ToolSpec y registro global Hermes, sin modificar la clasificación de riesgo ni el broker.
Los schemas empaquetados se comparan contra TOOL_MODELS central en el E2E.

## Comprobaciones

- Runtime: **221 PASS** en pruebas policy/transport, bridge, manager, schemas, seed/SSO y config applier. Sin red real.
- EE: **27 PASS**, SQLite + PostgreSQL16 efímero + round-trip contra Runtime explícito, sin skips. **5 PASS** adicionales de autenticación/import sin construcción lateral de app/MCP.
- Cross-repo: **4 PASS**. `create_app/Container` Ads reales + Postgres/reservas + EE router/autenticación/cookie/TOTP reales + delivery privado + Unix broker/SO_PEERCRED. Variantes directas y por publisher/store/vault/transport Runtime. Revocación niega la próxima lectura Runtime y deja cero mutaciones SDK al ejecutar. Sólo SDK del proveedor y destino HTTP son dobles.
- Imagen Hermes **0.21.1**, id `365e584d7f5c`, `--network=none`: PASS catálogo8 con argumentos exactos → broker una vez; broker puede rechazar; ni discovery/subprocess local ni escritura nativa del slug managed. No solicitud LLM/proveedor.
- Mypy focal Runtime4 módulos: strict con `--follow-imports=silent --ignore-missing-imports`, PASS. EE4 módulos PASS. Ruff nuevos módulos/consumidores MCP/bridge/harness PASS; no se afirma lint global de los módulos legacy.
- No nuevo full de los tres repos; root ejecuta integración global.

## Reproducir sin producción

Runtime scratch: `/tmp/safent-runtime-managed-ads.VeHNeY`.
Intérprete DGX: `/tmp/safent-enterprise-review-20260911/.venv/bin/python`, `PYTHONPATH=src`.
Tests:
```
tests/unit/test_managed_ads_policy.py
tests/unit/test_managed_ads_transport.py
tests/unit/shell_server/test_ads_bridge.py
tests/unit/mcp/test_mcp_server_manager.py
tests/unit/mcp/test_mcp_tool_specs_wiring.py
tests/unit/agents_os/test_seeded_companion_mcp.py
tests/unit/agents_os/test_companion_sso_assertion.py
tests/unit/config_sync/test_applier.py
```
EE scratch: `/tmp/safent-ee-ads-policy.SXHHfS`, intérprete Files `/tmp/safent-files-bytes.Hsmwnd/.venv/bin/python`.
`SAFENT_RUNTIME_SRC=/tmp/safent-runtime-managed-ads.VeHNeY/src`, `TEST_DATABASE_URL` sólo contenedor temporal, tests `test_ads_policy.py test_round_trip.py`.
PG testcontainers usa `DOCKER_HOST=unix:///run/user/1000/podman/podman.sock TESTCONTAINERS_RYUK_DISABLED=true`.

Ads scratch `/tmp/safent-managed-central.OyYbph/ads`, intérprete `/home/luiscorrea-dev/Desktop/safent-ads/.venv/bin/python`,
`PYTHONPATH=src:/tmp/safent-ee-ads-policy.SXHHfS/src:/tmp/safent-runtime-managed-ads.VeHNeY/src`,
`pytest tests/crossrepo/test_managed_central_composition.py`.
La extracción de instance_auth evita importar app.py/FastApiMCP de EE por un bootstrap: MCP EE1 y MCP Ads2 pueden seguir en servicios separados sin un import lateral en el router.

Smoke:
```
podman run --rm --network=none --env HERMES_HOME=/tmp/ads-native-home --env HOME=/tmp/ads-native-home --env PYTHONPATH=/work/src:/usr/lib/hermes-agent --volume /tmp/safent-runtime-managed-ads.VeHNeY:/work:ro --entrypoint python3 365e584d7f5c /work/tests/integration/managed_ads_native_smoke.py
```

## Pendiente, no afirmado como resuelto

- UI Community managed: AdsView todavía usa iframe local; el backend lo deniega correctamente. Próximo corte separado: asignaciones, lectura/propuesta y enlace a EE; sin confirmar desde Community.
- Verificación de despliegue con config-sync/D-Bus/systemd/red/TLS reales de una instancia y egress configurado; los tests ejercitan aplicación y registros reales, no un despliegue productivo.
- No reversión automática a modo libre; no propagación instantánea antes de aplicar el bundle.
- No cobertura universal Google/Meta: creación jerárquica/creativos y otros tools quedan fuera de la allowlist managed actual.
- Los claims/cifrado/grants EE son autoridad; acceso root al almacenamiento de la instancia sigue fuera del aislamiento de agente. No se promete protección contra borrado privilegiado de toda la base.

## Ownership exacto

Runtime (excluir fixtures UI y cambios de otros agentes):
- `pyproject.toml`
- `src/hermes/config_sync/ads_policy_contract.py`
- `src/hermes/config_sync/policy_document.py`
- `src/hermes/config_sync/applier.py`
- `src/hermes/runtime/managed_ads_policy.py`
- `src/hermes/runtime/managed_ads_transport.py`
- `src/hermes/runtime/managed_ads_mcp.py`
- `src/hermes/runtime/managed_ads_tool_schemas.json`
- `src/hermes/runtime/__main__.py`
- `src/hermes/mcp/application/mcp_server_manager.py`
- `src/hermes/mcp/domain/entities.py`
- `src/hermes/runtime/mcp_tool_specs.py`
- `src/hermes/runtime/nous_engine.py`
- `src/hermes/agents_os/infrastructure/dbus_fast_runtime_adapter.py`
- `src/hermes/agents_os/infrastructure/dbus_runtime_service.py`
- `src/hermes/shell_server/ads_bridge.py`
- `src/hermes/shell_server/main.py`
- `tests/unit/test_managed_ads_policy.py`
- `tests/unit/test_managed_ads_transport.py`
- `tests/integration/managed_ads_native_smoke.py`
- `docs/ads-managed-runtime-2026-09-12.md` (este informe).

Enterprise:
- `src/safent_control/domain/ads_policy_contract.py`
- `src/safent_control/domain/policy_document.py`
- `src/safent_control/application/ads_policy.py`
- `src/safent_control/application/policy_publisher.py`
- `src/safent_control/api/instance_auth.py`
- `src/safent_control/api/ads.py` sólo import de auth
- `src/safent_control/api/app.py` SÓLO extracción/reexport de dos funciones auth (mount Knowledge ajeno)
- `tests/test_ads_policy.py`
- `tests/test_instance_auth_module.py`

Ads:
- `tests/crossrepo/runtime_ads_driver.py`
- `tests/crossrepo/test_managed_central_composition.py`

`main.py` Runtime también tiene un mount CRM ajeno: sólo el cambio Ads bridge es de este corte. No uv.lock, índices ni commits propios.
