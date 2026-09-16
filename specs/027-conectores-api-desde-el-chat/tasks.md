# Tasks — Conectores de API desde el chat + enlace CRM→anuncios con valor de cliente

18 tareas: **13 en `safent-runtime`**, **5 en `safent-ads`**. `[P]` = paralelizable (sin solape de ficheros con otra `[P]` de la misma banda). Toda tarea trae su prueba; una tarea sin prueba no está hecha. Rutas absolutas al repo correspondiente.

Convención de entrega por historia: **US1** = T001-T009 (conector verificado desde el chat) · **US3** = T010 (webhook) · **US2** = T012, T014-T017 (enlace y valor de cliente) · **US4** = T012 (salud y reparación) · Escenario final = T018.

---

## Banda A — `safent-runtime` · cimientos (US1)

### T001 `[P]` `[US1]` — Fixtures deterministas y origen falso · `qa-engineer`
- `tests/unit/connectors/fixtures/golden_openapi.json` — spec de 12 operaciones (6 `GET`, 6 mutantes), `$ref` internos anidados, `deepObject`, array en query.
- `tests/unit/connectors/fixtures/fake_source.py` — origen en memoria (sin red): responde 200, 401, 429, 500 y una respuesta con forma derivada.
- `tests/unit/connectors/conftest.py` — reloj inyectable, vault falso, registro de egress falso.
- **Prueba**: la suite base corre **sin red, sin Chromium, sin BD** (constitución §V).

### T002 `[P]` `[US1]` — Dependencias pineadas y auditadas · `devops-engineer`
- `pyproject.toml` → extra `connectors = ["fastmcp-slim[server]==4.0.3", "standardwebhooks==1.1.0"]`; `authlib` llega transitivo.
- `ops/container/Containerfile:104` — instalar el extra **respetando `mcp==2.0.0`**; verificar que la resolución no lo mueve (`fastmcp` pide `mcp<3,>=2`).
- Import perezoso: `import hermes.connectors` sin el extra **no** debe fallar (constitución §Restricciones).
- **Prueba**: `tests/unit/connectors/test_lazy_import.py` + `pip-audit` sin críticas/altas + escaneo de `security_center` sobre los ~12 paquetes nuevos.

### T003 `[US1]` — Capa de dominio pura · `backend-engineer` · *depende de T001*
- `src/hermes/connectors/domain/`: `connector.py`, `auth_method.py`, `endpoint_catalogue.py`, `liveness_proof.py`, `health.py`, `webhook.py`, `field_mapping.py`, `crm_link.py`, `sync_run.py`, `events.py`.
- Sin framework, sin E/S, sin `yaml`. La máquina de estados vive en `Connector`, no en un servicio.
- **Pruebas** `tests/unit/connectors/domain/`: `listo` sin `LivenessProof` **levanta excepción**; `state ≠ listo` ⇒ catálogo publicado vacío; `not_proved` nunca vacía con > 1 operación; `MappingVersion` nueva no muta la vigente; devolución ⇒ importe ≤ 0.

### T004 `[US1]` — Puertos y casos de uso · `backend-engineer` · *depende de T003*
- `src/hermes/connectors/application/ports.py` — `SecretStore`, `EgressGrantPort`, `ToolRegistryPort`, `SchedulePort`, `AuditPort`, `AdsBridgePort`, `Clock`.
- `application/`: `create_connector_from_chat.py`, `verify_connector.py`, `repair_connector.py`, `sync_crm_to_ads.py`, `run_health_check.py`, `forget_customer.py`.
- **Pruebas**: casos de uso contra dobles en memoria; `VerifyConnector` **no** publica operaciones si la llamada real falla; `CreateConnectorFromChat` devuelve `missing[]` agrupado y **no** pregunta lo que ya sabe (FR-003).

---

## Banda B — `safent-runtime` · la jaula (US1)

### T005 `[P]` `[US1]` — Descarga de la descripción formal · `security-engineer` · *depende de T004*
- `src/hermes/connectors/infrastructure/spec_fetcher.py` — **reutiliza la guarda SSRF única compartida** del repo (`resolve-then-connect`); prohibido copiarla.
- Topes: ≤ 5 MiB crudo / 8 MiB expandido, ≤ 2 000 operaciones, `$ref` profundidad ≤ 20, **`$ref` externos rechazados**, 15 s totales, sin redirección cross-host, salida por el proxy.
- **Pruebas** `tests/security/test_connector_spec_fetch.py`: `127.0.0.1`, `[::ffff:127.0.0.1]`, `169.254.169.254`, DNS que resuelve a privada, redirección a interna, spec de 50 MiB, bomba de `$ref`, `$ref` remoto. **Todas rechazadas.**

### T006 `[P]` `[US1]` — Cliente HTTP y credencial · `backend-engineer` · *depende de T004*
- `infrastructure/http_client.py` — sólo por el proxy; `connect 5 s / read 20 s / total 30 s`; sin redirecciones cross-host; cubo de fichas 60 req/min y 5 concurrentes.
- `infrastructure/vault_credential_resolver.py` — resuelve por llamada **por socket unix**; `AsyncOAuth2Client` (authlib) sólo en `client_credentials`.
- **Pruebas**: `tests/unit/connectors/test_no_secret_leak.py` — barrido de `argv`, `env`, logs, respuestas de herramienta y contexto del modelo: **cero apariciones** del material secreto (NFR-001, SC-006); el 429 dispara espera creciente con jitter y tope de 6 intentos.

### T007 `[US1]` — Runner: OpenAPI → herramientas · `backend-engineer` · *depende de T002, T005, T006*
- `src/hermes/connectors/infrastructure/runner.py` — envoltura fina de `FastMCP.from_openapi(spec_dict, client=...)`; `RouteMap(methods=["POST","PUT","PATCH","DELETE"], mcp_type=EXCLUDE)`; entrypoint `python3 -m hermes.connectors.runner --connector-id <id>`.
- **Pruebas** `tests/unit/connectors/test_runner_catalogue.py`: contra `golden_openapi.json`, el catálogo derivado tiene **exactamente las 6 operaciones `GET`** y **cero mutantes**; prueba de contrato con el **esquema dorado** de tres herramientas (nombres y `inputSchema` congelados) — si FastMCP cambia la derivación, rompe aquí, no en producción.

### T008 `[US1]` — Registro MCP por conector · `backend-engineer` · *depende de T007*
- `infrastructure/mcp_registry_adapter.py` — registra `connector-<slug>` como `MANAGED_REMOTE`, `env` sin secretos.
- `src/hermes/agents_os/infrastructure/dbus_runtime_service.py:7425` — `_MANAGED_REMOTE_MCP_SLUGS` pasa de `frozenset` a **predicado** `is_managed_remote_slug(slug)`; el host se resuelve **sólo** desde el almacén local autorizado (regla de oro intacta).
- **Pruebas** `tests/unit/agents_os/test_managed_remote_predicate.py`: un slug `connector-*` **ausente** del almacén local **no** obtiene concesión; un host propuesto desde el `argv`/`env` del bundle **se ignora**; `degradado` ⇒ el servidor se desregistra y las operaciones **desaparecen** (FR-008).

### T009 `[US1]` — Verbos D-Bus y raíz de composición · `backend-engineer` · *depende de T004, T008*
- Verbos con validación de esquema **obligatoria** (decorador, no `json.loads` crudo): `CreateConnectorDraft`, `StoreConnectorCredential`, `VerifyConnector`, `ConfirmFieldMapping`, `RotateWebhookSecret`, `ForgetCustomer`, `DeleteConnector`.
- `src/hermes/connectors/composition/__init__.py` — raíz propia; **una línea** de arranque en `src/hermes/runtime/__main__.py`. Nadie del SCC importa `connectors`.
- `src/hermes/shell_server/connectors_api.py` — GET de supervisión + `verify`/`sync-now` como passthrough con `OperatorToken`, `{ok:false}` ⇒ 400/403 (nunca 2xx).
- **Pruebas**: `tests/contract/test_connectors_api.py` (todo GET exige credencial; `{ok:false}` nunca sale 2xx); `tests/unit/connectors/test_no_cycles.py` — grafo de imports: **cero aristas** desde el SCC hacia `connectors`.

---

## Banda C — `safent-runtime` · superficies (US3, US4, US2)

### T010 `[P]` `[US3]` — Webhook entrante · `backend-engineer` · *depende de T003, T009*
- `src/hermes/shell_server/webhooks_api.py` — `POST /api/v1/webhooks/inbound/{opaque_id}` con `standardwebhooks`; puerta de 7 comprobaciones en orden; muestras anonimizadas **en el borde, antes de tocar disco**, caducas a 30 días.
- **Pruebas** `tests/security/test_webhook_gate.py`: sin firma → 401 y **estado inalterado**; timestamp a +6 min → 400; `webhook-id` repetido → `duplicate` **sin duplicar hechos**; cuerpo > 1 MiB → 413; secreto anterior válido durante la rotación y muerto después; **ninguna muestra contiene un identificador crudo**.

### T011 `[P]` `[US1]` — Skill del chat y herramientas del bróker · `backend-engineer` · *depende de T004*
- `skills/connector-setup/SKILL.md` firmada (escritor único: `SkillStoreAdapter`); herramientas `connector_draft`, `connector_request_egress`, `connector_store_credential`, `connector_verify`, `connector_status`, `connector_delete` en el catálogo del bróker con **riesgo fijado en servidor**.
- **Pruebas** `tests/unit/connectors/test_skill_surface.py`: **cero enrutado por palabras clave** (ninguna comparación de cadenas del mensaje decide invocación); `connector_request_egress`, `connector_store_credential` y `connector_delete` son `HIGH`; sin token HITL ⇒ `PENDING_APPROVAL`, jamás ejecución.

### T012 `[US2][US4]` — Sincronización, salud y reparación acotada · `backend-engineer` · *depende de T006, T009*
- `application/sync_crm_to_ads.py` (idempotente por `(link_id, cursor)`, reanudable, `SyncReport` siempre cerrado) · `infrastructure/identity_hasher.py` (**hasheo en el borde**, sal `SecretsVault.derive_subkey`) · `infrastructure/ads_http_bridge.py` · `run_health_check.py` + `repair_connector.py` (6 intentos, espera creciente con jitter, 50 %→25 % de ritmo, renovación `client_credentials`, re-derivación **sólo si ningún campo de dinero cambió**) · `infrastructure/cron_schedule_adapter.py` (catálogo `cron` existente, un job por conector, desfasado).
- **Pruebas**: interrumpir un `SyncRun` a mitad y reanudarlo ⇒ **cero duplicados, cero pérdidas**; credencial revocada ⇒ `degradado` en un ciclo con causa y `last_good_at`; **deriva en campo de dinero ⇒ sincronización detenida y nada recalculado**; el mensaje de escalada trae las seis partes (`qué falló`, `desde cuándo`, `último bueno`, `qué se intentó`, `qué necesito`, `qué queda incompleto`); ningún identificador crudo sale del proceso.

### T013 `[P]` `[US1]` — Vista de Conectores · `frontend-engineer` · *depende de T009*
- `src/hermes/frontend/src/views/ConnectorsView.tsx` + entrada en la navegación. Estado, última comprobación, dominio, autenticación, **lo probado y lo no probado**, y el banner de `degradado` con «qué queda incompleto».
- **Prueba**: `tests/frontend/ConnectorsView.test.tsx` — un conector `sin verificar` muestra **cero operaciones**; `degradado` nunca aparece como sano; ningún secreto ni identificador en el DOM.

---

## Banda D — `safent-ads` · cliente, ingreso recurrente y LTV (US2)

### T014 `[P]` `[US2]` — Migraciones · `database-engineer`
- `alembic/versions/0030_customers.py`, `0031_revenue_events.py`, `0032_crm_bridge_health.py` (**ids ≤ 32 caracteres**). Sólo `expand`: nada se borra, `lead_attributions` intacta.
- UNIQUE `(business_id, connector_id, source_event_id)`; CHECK `kind`; CHECK «`refund`/`churn` ⇒ `amount <= 0`»; trigger de **no-UPDATE/DELETE** en `revenue_events`; índice `(business_id, occurred_at DESC)` para el corte de 24 h.
- **Prueba** `tests/integration/migrations/test_customers_revenue.py`: `upgrade`→`downgrade`→`upgrade` limpio; **leer la salida completa de alembic y verificar `alembic_version`** (un `lock_timeout` deja el código por delante del esquema en silencio).

### T015 `[US2]` — Dominio y casos de uso de cliente · `backend-engineer` · *depende de T014*
- `src/safent_ads/crm/domain/`: `customer.py`, `revenue_event.py`, `customer_value.py`, `identity_mapping.py`, `bridge_health.py`. `crm/application/`: `ingest_customers.py`, `ingest_revenue_events.py`, `forget_customer.py`, `compute_customer_value.py`.
- **Pruebas** `tests/unit/crm/`: devolución ⇒ resta y **no borra**; el mismo `source_event_id` dos veces ⇒ un solo hecho; cohorte inmadura ⇒ `projected = None` **con `no_number_reason`**, jamás un número; fundir identidades escribe `merged_into` y no borra.

### T016 `[US2]` — Borde REST de ingesta · `backend-engineer` · *depende de T015*
- `src/safent_ads/crm/presentation/rest.py` (extiende el existente): `POST /crm/customers`, `POST /crm/revenue-events`, `PUT /crm/bridge-health`, `POST /crm/customers/forget`, `GET /crm/sync-runs/{id}`. `X-Bridge-Token`, `RequireBusinessAccess` **como dependencia del router**, lotes ≤ 1 000.
- **Pruebas** `tests/contracts/test_crm_bridge_api.py`: un `identity_digest` que no sea hex de 64 ⇒ **400 `IDENTITY_NOT_HASHED`**; `refund` con importe positivo ⇒ 400; reenviar el lote entero ⇒ `duplicated`, nunca doble ingesta; ningún `details` de error contiene un identificador.

### T017 `[US2]` — Congelación por puente y lectura de LTV · `backend-engineer` · *depende de T016*
- `src/safent_ads/orchestration/infrastructure/measurement_freeze_gate.py:69` — sustituir `bridge_has_recent_events_24h=True` por la lectura real de `crm_bridge_health`; el estado remoto **sólo empeora**, nunca mejora.
- `src/safent_ads/economics/` — `GET /economics/customer-value`; `BuildUnitEconomicsProfile` usa el LTV observado para `collection_rate(H)` y `refund_rate` cuando hay muestra suficiente, y sigue `provisional` cuando no.
- **Pruebas** `tests/integration/orchestration/test_freeze_by_bridge.py`: puente sin hechos en 24 h ⇒ **cero propuestas de subida** y `pause`/`lower` **intactas**; recuperado el puente ⇒ BUY vuelve al ciclo siguiente; `tests/unit/economics/`: muestra insuficiente ⇒ perfil sigue `provisional`.

---

## Escenario final

### T018 — Escenario en vivo, de la frase al cuadro de mando · `qa-engineer` · *depende de todo*
`specs/027-conectores-api-desde-el-chat/quickstart.md` + `tests/e2e/test_connector_live.py` (marcado `requires_network`, **fuera de la puerta base**).

Contra una API pública de prueba, en el chat y sin tocar un formulario:

1. **Describir**: «necesitamos consumir esta API, aquí está su OpenAPI y la clave». El agente pregunta **≤ 3 cosas**, agrupadas.
2. **Autorizar**: aparece la tarjeta con el **host exacto**. Se concede. El secreto se guarda y **no reaparece** en chat, panel ni bitácora.
3. **Verificar**: llamada real de sólo lectura. El agente reporta **lo probado y lo no probado**. Estado `listo` en **≤ 5 min** (SC-001).
4. **Ver las herramientas**: las operaciones aparecen bajo el nombre amistoso; **cero operaciones mutantes** en el catálogo (A-1).
5. **Enlazar**: un fixture con forma de CRM (un cliente, una primera compra, tres cobros recurrentes, una devolución). El agente propone el mapa con ejemplos **anonimizados**; el dueño confirma en **una** tarjeta.
6. **Sincronizar**: un ciclo. El informe declara leídos, enviados, rechazados, duplicados y divergencia. Los cinco hechos caen bajo **la misma identidad** y el valor de cliente **suma los cuatro cobros y resta la devolución**.
7. **Cuadro de mando**: `clientes`, `valor de cliente` y `ROI` aparecen en la fila y **cuadran con el origen dentro del ±2 %** (SC-004).
8. **Romper**: revocar la credencial. En **≤ 1 ciclo** el conector pasa a `degradado` con causa y último dato bueno; el negocio entra en **BUY congelado** con banner y aviso; `pause`/`lower` siguen vivas; el mensaje de escalada trae sus seis partes (A-3, SC-003, SC-005).
9. **Olvidar**: `crm_forget_customer` sobre esa identidad ⇒ desaparece de ambos lados y **queda la anotación de la supresión** sin el identificador (A-2).

**Verificación transversal, obligatoria al cierre**: barrido de bitácora, panel, journal del contenedor y contexto del modelo — **cero secretos y cero identificadores personales** (SC-006). Y la comprobación que el dueño paga si falla: **ninguna cifra del cuadro de mando existe sin una llamada real detrás en la bitácora** (SC-002).
