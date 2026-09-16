# Implementation Plan — Conectores de API desde el chat + enlace CRM→anuncios con valor de cliente

**Spec**: `spec.md` (484b6aa) · **Fase 0**: `research.md` · **Fase 1**: `data-model.md`, `contracts/` · **Tareas**: `tasks.md`
**Repos**: `safent-runtime` (contexto `connectors`) · `safent-ads` (extensión del contexto `crm` + `economics`)

## Technical Context

Python 3.12 · daemon `hermes-runtime` con cola propia y control-plane D-Bus · jaula: proxy de egress (nftables, por dominio), `SecretsVault` (AES-GCM-256), lanzador MCP (netns por defecto-deniega + Landlock), bitácora WORM encadenada, bróker de capacidades con HITL. Compañero `safent-ads`: FastAPI + Postgres, contexto `crm` con `lead_attributions` e identidad hasheada, contexto `economics` con `UnitEconomicsProfile`. **Ninguna incógnita abierta**: las tres de `spec.md` quedan resueltas en `research.md` §Aclaraciones (A-1 lectura, A-2 olvido por hash, A-3 congelación de BUY).

## Constitution Check — GATE PRE-DISEÑO

| Principio | Veredicto | Nota |
|---|---|---|
| **0.1** Trigger = cola del daemon | **PASS** | Crear, verificar, reparar y sincronizar son `WorkItem`. La skill del chat encola; nadie más dispara. |
| **0.2** HTTP sólo supervisión | **PASS con nota** | `GET /api/v1/connectors*` es lectura. Los dos mutadores (`verify`, `sync-now`) son passthrough fino con `OperatorToken` al mismo verbo D-Bus, calcado de `mcp_api.py`. **La creación NO es REST.** |
| **0.3** Gobernanza = estado nativo | **PASS** | Conectores, mapas y concesiones viven en `/var/lib/hermes/connectors/` y sólo los muta un verbo D-Bus. |
| **0.4** Autoría por `sender_uid` | **PASS** | Sin cambios. |
| **0.5** Primitivas del SO | **PASS** | Egress = nftables/proxy existente; confinamiento = lanzador MCP; ritmo = catálogo `cron` único. Cero mecanismo nuevo. |
| **0.6** Confinamiento del kernel | **PASS** | El runner corre en el netns MCP. La guarda SSRF es defensa en profundidad, **no** el confinamiento. |
| **II** HITL irreversible | **PASS** | Escrituras fuera de v1; cuando lleguen, `HIGH` + tarjeta por llamada. |
| **III** PII antes del LLM | **PASS reforzado** | Hasheo en el borde: el crudo no existe fuera del proceso conector. |
| **IV** Fail-closed | **PASS** | Sin verificar ⇒ cero operaciones publicadas. Degradado ⇒ BUY congelado. Egress ausente ⇒ no se llama. |
| **V** Tests deterministas | **PASS** | Spec dorado en fixture; el escenario en vivo va marcado. |

**Veredicto: PASS.** Una desviación conocida entra en Complexity Tracking (nueva dependencia).

## Contexto acotado `connectors` (runtime)

Paquete **nuevo y limpio** `src/hermes/connectors/`, **fuera del SCC de 16 paquetes** (`radiografia.md §1`): nada del SCC lo importa. Tiene **raíz de composición propia** (`connectors/composition/`), invocada con **una línea** desde `runtime/__main__`. Depende de la jaula sólo por **puertos declarados en `application/ports.py`** con adaptadores en `infrastructure/`. Sin ciclos por construcción.

**Domain** (puro, sin framework): `Connector` (raíz de agregado — dueña de la máquina de estados `configurando → esperando_autorizacion → sin_verificar → listo → degradado → suspendido`; **invariante: sólo `listo` publica operaciones**, y sólo se alcanza con una `LivenessProof` real registrada) · `AuthMethod` (VO: `ApiKey|Bearer|Basic|ClientCredentials`, sólo la **referencia** al secreto) · `EndpointCatalogue` + `Endpoint` (catálogo derivado; VO inmutable con `catalogue_hash`) · `HealthCheck` y `HealthVerdict` (`sano|degradado`, con causa tipada `credential_expired|schema_drift|rate_limited|source_error`) · `Webhook` (agregado propio: secreto vigente + anterior, `first_delivery_at`, muestras caducas) · `FieldMapping` + `MappingVersion` (**versionado solo-anexable con `effective_from`**; los campos de dinero llevan `requires_card=True`) · eventos: `ConnectorVerified`, `ConnectorDegraded`, `ConnectorRepaired`, `MappingConfirmed`, `WebhookDeliveryAccepted`, `CustomerForgotten`.

**Application** (casos de uso; aquí se declaran los puertos): `CreateConnectorFromChat` — **una SKILL firmada** (`connector-setup`) más cuatro herramientas del bróker (`connector_draft`, `connector_request_egress`, `connector_store_credential`, `connector_verify`) que el agente usa **conversacionalmente**; el agente decide por comprensión del texto del dueño, **jamás por palabras clave ni por enrutado determinista** (FR-001) · `VerifyConnector` — ejecuta de verdad la operación de vida y escribe la `LivenessProof`; sin ella no hay `listo` · `RepairConnector` — reparación **acotada** (abajo) · `SyncCrmToAds` — reanudable e idempotente por `(connector_id, cursor)`, con `SyncReport` de leídos/enviados/rechazados/duplicados/divergencia · `ForgetCustomer` (A-2) · `RunHealthCheck`.
**Puertos**: `SecretStore`, `EgressGrantPort`, `ToolRegistryPort`, `SchedulePort`, `AuditPort`, `AdsBridgePort`, `Clock`.

**Infrastructure**: `SpecFetcher` (guarda SSRF **única compartida**, resolve-then-connect, topes de `research.md`) · `ConnectorRunner` (proceso stdio por conector: `FastMCP.from_openapi(spec, client)` con `RouteMap` que **excluye** todo método mutante) · `ProxiedHttpClient` (salida **sólo** por el proxy; `connect 5 s / read 20 s / total 30 s`; sin redirecciones cross-host; cubo de fichas 60 req/min, 5 concurrentes) · `VaultCredentialResolver` (resuelve por llamada **por socket unix**; nada en `argv`/`env`) · `McpRegistryAdapter` (registra `connector-<slug>` como `MANAGED_REMOTE`) · `CronScheduleAdapter` · `WormAuditAdapter` · `AdsHttpBridge`.

**Presentation**: `shell_server/connectors_api.py` (lectura + dos passthrough) y las tarjetas del chat.

## Compañero `safent-ads` — cliente, ingreso recurrente y valor de cliente

Extensión del contexto **`crm`** (no un contexto nuevo: es el mismo lenguaje). `Customer` (raíz; identidad **hasheada**, `first_paid_conversion_at`, estado `active|churned`) · `RevenueEvent` (VO solo-anexable: `first_payment|recurring_payment|refund|churn`; **las devoluciones son importes negativos, nunca borrados** — FR-019 sin reescribir historia) · `CustomerValue` (contribución acumulada + proyectada; deriva, no se almacena como verdad) · `IdentityMapping` (versionado, `merged_into` solo-anexable) · `CrmBridgeHealth`.
**`economics`** lee el LTV observado para dejar de suponer `collection_rate(H)` y `refund_rate` (`profitability-engine.md §1`) y expone `GET /economics/customer-value`. El cuadro de mando (spec 026, FR-005/FR-017) consume `customers`, `customer_value` y `roi` por fila; **con el puente caído no fabrica cifras: declara «incompleto»** (FR-009 de 026).

### Costuras entre contextos (4, todas explícitas)

1. **runtime → ads (datos)**: `POST /crm/customers` y `POST /crm/revenue-events` — por lotes, idempotentes por `source_event_id`, **sólo identidades hasheadas**. El runtime nunca toca la BD del compañero.
2. **ads → runtime (conciliación)**: `GET /crm/sync-runs/{id}` devuelve el informe; el runtime lo narra en el chat.
3. **ads → cuadro de mando (026)**: `GET /economics/customer-value` y el campo `customer_value` en cartera.
4. **runtime → ads (salud, A-3)**: `PUT /crm/bridge-health` marca el puente. `SqlMeasurementFreezeGate` deja de cablear `bridge_has_recent_events_24h=True` y lo lee de ahí: **BUY congelado, defensivas intactas, banner + Telegram**. Sin lógica nueva — el parámetro ya estaba esperando esta fuente.

## Gobernanza — qué paso levanta qué tarjeta

| Paso | Tarjeta | Contenido | Bitácora |
|---|---|---|---|
| Dominio nuevo (base, `token_endpoint`, origen del webhook) | **Sí**, una por host | Host **exacto**, nunca comodín; qué operación lo necesita | `EgressGrantRequested/Granted` |
| Guardar o rotar credencial | **Sí** | Tipo, ámbito declarado, conector; **jamás el valor** | `CredentialStored{ref}` |
| Confirmar mapa de **dinero** | **Sí**, una vez, editable | Campo→significado con ejemplos **anonimizados**, `effective_from` | `MappingConfirmed{version}` |
| Herramienta de escritura (P3) | **Sí, por llamada** | Diff antes/después | `WriteToolInvoked` |
| Rotar la sal de identidad | **Sí** | Reetiqueta la historia | `IdentitySaltRotated` |
| Eliminar conector | **Sí** | Revoca credencial **y** egress | `ConnectorDeleted` |
| Reparación acotada | **No** | — | `RepairAttempted{action,outcome}` |

**Límites**: 60 req/min y 5 concurrentes por conector, 600 global; presupuesto de error por ciclo. **Timeouts**: 5/20/30 s de llamada, 15 s de spec. **PII**: hasheo en el borde con sal derivada por negocio; el crudo nunca se persiste, ni se registra, ni entra en el contexto del modelo. **Bitácora**: solo-anexable y encadenada, con `connector_id`, operación, código, latencia y bytes — **sin cuerpo, sin secreto, sin identificador crudo**.

## Autorreparación

**Chequeo horario por conector**: (1) sonda de credencial = la operación de vida; (2) deriva de forma = hash del esquema de los campos **mapeados**; (3) ritmo y errores del último ciclo.

**Reparación permitida sin tarjeta** (acotada, registrada, máx. 6 intentos con espera creciente y jitter): reintento de lectura idempotente · reducción de ritmo al 50 % y luego al 25 % · renovación del token cuando el método es `ClientCredentials` (la credencial ya está concedida) · re-lectura del spec desde la **misma URL ya autorizada** y re-derivación del catálogo **si ningún campo de dinero cambió** · **pausar la sincronización** (siempre es defensivo).

**Exige tarjeta**: cualquier dominio nuevo · credencial o ámbito nuevos · **cualquier deriva en un campo de dinero** (la sincronización se detiene y **nada se recalcula** hasta confirmar el mapa nuevo, FR-020) · rotar el secreto del webhook.

**Forma de la escalada** (chat + Telegram + banner): `qué falló` (una frase) · `desde cuándo` (primer chequeo malo) · `último dato bueno` · `qué se intentó` (lista de acciones con resultado) · `qué necesito de ti` (**una** frase imperativa) · `qué queda incompleto` (derivados afectados). Nunca «error 500».

## Constitution Check — GATE POST-DISEÑO

Re-verificado: **PASS**. El diseño no añade endpoint de mecanismo (P0.2), no razona fuera de la cola (P0.1), no reimplementa primitiva del SO (P0.5 — egress, confinamiento y cron son los existentes), no confía en allowlists de aplicación como confinamiento (P0.6 — la guarda SSRF es defensa en profundidad sobre nftables) y refuerza III y IV.

## Complexity Tracking

| Desviación | Por qué la necesita | Alternativa más simple, rechazada |
|---|---|---|
| Dependencia `fastmcp-slim[server]==4.0.3` (+ ~12 transitivas) en la imagen | Derivar herramientas desde OpenAPI a mano son ~400 L de serialización propia donde el fallo es **una llamada silenciosamente incorrecta**, justo lo que FR-006/FR-007 prohíben | **Escribirlo** sobre `openapi-pydantic`+`jsonref`: menos dependencias, pero código propio permanente y frágil. Mitigado con pin exacto, escaneo de `security_center`, `pip-audit` y prueba de contrato con spec dorado |
| `_MANAGED_REMOTE_MCP_SLUGS` pasa de conjunto fijo a predicado | Un servidor por conector con nombre del dueño | Un servidor MCP único con todos dentro: un conector caído tumbaría los demás (NFR-003) y borraría la frontera de credenciales |

## Preguntas abiertas para el dueño

1. **A-3 (congelación)**: ¿confirmas que un CRM caído congela **subidas** de presupuesto y no toda actuación? Un fin de semana sin CRM = un fin de semana sin subir gasto.
2. **A-2 (olvido)**: ¿basta con borrar por hash y anotar la supresión, o quieres además un **informe mensual** de olvidos ejecutados?
3. **Escritura P3**: ¿hay algún origen concreto donde escribir sea urgente, o esperamos a que aparezca la necesidad?
4. **Sal de identidad**: rotarla reetiqueta toda la historia de clientes. ¿Cadencia fija (anual) o sólo bajo sospecha de filtración?
