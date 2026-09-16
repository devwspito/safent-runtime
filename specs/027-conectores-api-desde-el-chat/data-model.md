# Data Model — Conectores + enlace CRM→anuncios con valor de cliente

Dos repos, dos contextos, una costura de datos. Sin anotaciones ORM, sin SQL, sin decoradores de serialización: esto es el modelo del negocio.

## Lenguaje ubicuo

| Término | Definición |
|---|---|
| **Conector** | Origen externo consumible: dominio, autenticación, catálogo de operaciones y estado. Vive en el runtime. |
| **Operación** | Capacidad invocable derivada de la descripción formal. Una de ellas es la **prueba de vida**. |
| **Prueba de vida** (`LivenessProof`) | Registro de una llamada real de sólo lectura que devolvió éxito. **Sin ella no existe el estado `listo`.** |
| **Deriva de forma** | La respuesta del origen ya no encaja con el mapa vigente. Si toca un campo de dinero, todo se detiene. |
| **Mapa de campos** | Traducción versionada origen→dominio. Versión nueva ⇒ `effective_from`; jamás reescritura. |
| **Enlace** | Unión viva entre un conector de CRM y el compañero de anuncios: mapa + ritmo + informe. |
| **Identidad de cliente** | Digest irreversible (sal por negocio) que agrupa los hechos económicos de una persona. |
| **Hecho económico** (`RevenueEvent`) | Primera compra, cobro recurrente, devolución o baja. Solo-anexable. |
| **Valor de cliente** (`CustomerValue`) | Contribución acumulada (y proyectada) de una identidad. Se **deriva**, no se almacena como verdad. |
| **Puente** (`CrmBridge`) | El conducto conector→compañero. Su salud gobierna si se puede subir gasto. |
| **Tarjeta** | Decisión del dueño sobre seguridad o dinero. Nada la sustituye. |

## Bounded contexts

- **`connectors`** *(repo `safent-runtime`, paquete nuevo `src/hermes/connectors/`)* — posee `Connector`, `Webhook`, `FieldMapping`, `CrmLink`, `SyncRun`. Contrato hacia fuera: puertos `SecretStore`, `EgressGrantPort`, `ToolRegistryPort`, `SchedulePort`, `AuditPort`, `AdsBridgePort`. **Nadie del SCC de 16 paquetes lo importa**; su raíz de composición es propia.
- **`crm`** *(repo `safent-ads`, extiende el existente)* — posee `Customer`, `RevenueEvent`, `IdentityMapping`, `CrmBridgeHealth`, junto a la ya existente `LeadAttribution`. Contrato: REST `/crm/*` y lectura desde `economics`.
- **`economics`** *(repo `safent-ads`, sin cambios estructurales)* — **consume** `CustomerValue` para dejar de suponer `collection_rate(H)` y `refund_rate`. No posee nada nuevo.

Dirección: `connectors → (HTTP) → crm → economics → optimization/panel`. Sin vuelta atrás, sin ciclos.

---

# Repo `safent-runtime` — contexto `connectors`

## Connector *(raíz de agregado)*

- **Invariantes**
  1. `state = listo` **exige** una `LivenessProof` con `succeeded_at` no nulo. No hay otro camino.
  2. `state ≠ listo` ⇒ el catálogo publicado está **vacío** (ausentes, no presentes-y-rotas).
  3. Toda llamada sale hacia un host de `granted_hosts`; `granted_hosts` sólo crece por tarjeta, nunca por respuesta del origen.
  4. `credential_ref` es una **referencia**; el agregado nunca conoce el valor.
  5. `friendly_name` único por instalación; renombrar **no** cambia `slug` ni pierde historia.
  6. En v1 el catálogo no contiene operaciones mutantes (se excluyen al derivar).
- **Estado / ciclo de vida**
  `configurando → esperando_autorizacion → sin_verificar → listo → degradado → listo` · desde cualquiera → `suspendido` → `sin_verificar`. `degradado → listo` sólo tras una nueva `LivenessProof`.
- **Atributos**: `ConnectorId` · `Slug` (`connector-<friendly>`) · `FriendlyName` · `BaseUrl` (host validado) · `AuthMethod` · `CredentialRef` · `EndpointCatalogue` · `LivenessProof?` · `HealthVerdict` · `GrantedHosts` · `RateBudget` · `SpecSource` (`formal_spec|examples`) · `CreatedAt`, `LastCheckedAt`
- **Eventos**: `ConnectorVerified` (al lograr la prueba) · `ConnectorDegraded{cause}` · `ConnectorRepaired{action}` · `ConnectorSuspended` · `ConnectorDeleted`

## AuthMethod *(VO)*

`ApiKey{header_name}` · `Bearer` · `Basic` · `ClientCredentials{token_endpoint_host, scopes}`.
**Invariante**: el valor del secreto **nunca** forma parte del VO; sólo `CredentialRef`. `ClientCredentials` exige que `token_endpoint_host` esté en `granted_hosts` — su propia tarjeta, no la del `base_url`.

## EndpointCatalogue / Endpoint *(VO inmutables)*

- **Invariantes**: `catalogue_hash` cubre nombre + esquema de entrada de cada operación → **la deriva es una comparación, no una opinión**. Publicar una operación exige `is_read_only`. Tamaño ≤ 2 000 operaciones.
- **Atributos de `Endpoint`**: `OperationName` (slug ≤ 56) · `HttpMethod` (v1: `GET|HEAD`) · `PathTemplate` · `InputSchema` · `IsLivenessCandidate`
- Derivar un catálogo nuevo con el mismo `catalogue_hash` es un no-evento.

## LivenessProof *(VO)*

`operation_name` · `requested_at` · `succeeded_at` · `status_code` · `latency_ms` · `response_shape_hash` · `proved_what` (lista de lo demostrado) · `not_proved` (lista de lo **no** demostrado, FR-007).
**Invariante**: `not_proved` nunca está vacía cuando el catálogo tiene más de una operación — declarar «todo probado» con una llamada es mentir.

## HealthCheck / HealthVerdict *(VO)*

`checked_at` · `verdict` (`sano|degradado`) · `cause` (`credential_expired|schema_drift|rate_limited|source_error|none`) · `last_good_at` · `attempts` (acciones de reparación con resultado).
**Invariante**: `verdict = degradado` ⇒ todo derivado se marca **incompleto**; nunca se degrada en silencio (FR-026).

## Webhook *(raíz de agregado propia)*

- **Invariantes**
  1. `secret_ref` vigente y `previous_secret_ref` conviven durante la rotación: no hay ventana ciega ni entregas perdidas.
  2. Entrega sin firma válida o fuera de la tolerancia de reloj (5 min) ⇒ **rechazada, registrada, estado inalterado**.
  3. `state = listo` exige `first_delivery_at` no nulo.
  4. `delivery_id` visto ⇒ descartada por duplicada; jamás duplica hechos.
  5. Muestras caducan a los 30 días y se purgan.
- **Estado**: `esperando_primera_entrega → listo → degradado`
- **Atributos**: `WebhookId` · `ConnectorId` · `InboundPath` · `SecretRef` + `PreviousSecretRef?` · `SignatureScheme` (v1: `standard_webhooks`) · `FirstDeliveryAt?` · `PayloadSamples` (caducas) · `SeenDeliveryIds` (ventana)
- **Eventos**: `WebhookDeliveryAccepted` · `WebhookDeliveryRejected{reason}` · `WebhookSecretRotated`

## FieldMapping / MappingVersion

- **Invariantes**
  1. Solo-anexable: una versión nueva lleva `effective_from`; **nunca** se reescribe una vigente (FR-020).
  2. Todo campo con `is_money = true` lleva `requires_card = true`: no se activa sin tarjeta.
  3. Sin versión vigente a una fecha ⇒ nada se sincroniza para esa fecha (fail-closed).
  4. `MoneyField` declara **divisa y unidad menor** explícitas; adivinar la divisa está prohibido.
- **Atributos**: `MappingVersionId` · `ConnectorId` · `Version` · `EffectiveFrom` · `Rules` (`source_path → domain_field`, con `is_money`, `currency`, `timezone`) · `ConfirmedAt`, `ConfirmedBy`
- **Eventos**: `MappingProposed` · `MappingConfirmed{version}` · `MappingDriftDetected{field, is_money}`

## CrmLink *(raíz de agregado)*

- **Invariantes**: **sólo lee del origen** (FR-022), en v1 y en P3 · exige conector `listo` y mapa confirmado · un enlace vivo por conector · borrar el conector suspende el enlace, no lo borra en silencio.
- **Atributos**: `LinkId` · `ConnectorId` · `BusinessId` · `MappingVersionId` · `Cadence` · `IdentitySaltRef` · `Cursor` · `State` (`activo|pausado|detenido_por_deriva`)
- **Eventos**: `LinkActivated` · `LinkHaltedByMoneyDrift` · `IdentitySaltRotated`

## SyncRun *(entidad)*

- **Invariantes**: idempotente por `(link_id, cursor)`; reanudable — una interrupción no repite hechos ni los pierde. El informe se cierra **siempre**, incluso al fallar.
- **Atributos**: `SyncRunId` · `LinkId` · `StartedAt`, `EndedAt?` · `Cursor` inicial y final · `Report{read, sent, rejected, duplicated, divergence_pct}` · `Outcome` (`ok|parcial|fallido`)
- **Eventos**: `SyncCompleted{report}` · `SyncDiverged{pct}` (> 2 %, SC-004)

## Eventos de dominio (runtime)

`ConnectorVerified` · `ConnectorDegraded` · `ConnectorRepaired` · `MappingConfirmed` · `MappingDriftDetected` · `WebhookDeliveryAccepted` · `WebhookDeliveryRejected` · `LinkHaltedByMoneyDrift` · `SyncCompleted` · `SyncDiverged` · `CustomerForgotten`.
Todos llevan `connector_id`, `occurred_at` y `correlation_id`, y se anexan a la bitácora WORM **sin cuerpo de respuesta, sin secreto y sin identificador crudo**.

## Relationships (runtime)

```
Connector 1─0..1 Webhook
Connector 1─n MappingVersion (solo-anexable)
Connector 1─0..1 CrmLink 1─n SyncRun
Connector 1─1 EndpointCatalogue 1─n Endpoint
Connector 1─0..1 LivenessProof   ·   Connector 1─n HealthCheck
Todo ──→ AuditEntry (WORM, sin FK dura)
```

## Persistencia (handoff a `database-engineer`)

Estado del daemon en `/var/lib/hermes/connectors/` (JSON por conector, `0600`, dueño `hermes`), **al lado** de `egress-grants.json` y del almacén `managed_remote_endpoints` — mismo patrón, misma copia de seguridad, mismo dueño. Sin base de datos nueva: un conector es gobernanza, no analítica. Los secretos **no** viven aquí: `SecretsVault` guarda el valor y el fichero sólo la `CredentialRef`.

---

# Repo `safent-ads` — extensión del contexto `crm`

## Customer *(raíz de agregado)*

- **Invariantes**
  1. **Prohibido el dato personal.** La identidad es `HashedIdentity` (sal por negocio). No hay campo para email, teléfono ni nombre — ni opcional.
  2. `first_paid_conversion_at` es el mínimo `occurred_at` de sus `RevenueEvent` de tipo `first_payment`; se deriva.
  3. Un `Customer` pertenece a un `business_id`; el mismo digest en otro negocio es otra persona.
  4. `state = churned` no borra nada: la historia queda.
- **Estado**: `lead → customer (activo) → churned`, con retorno a `activo` si vuelve a pagar.
- **Atributos**: `CustomerId` · `BusinessId` · `HashedIdentity` · `EntityRef?` (campaña de origen, si la escalera de atribución la resolvió) · `AttributionRung` · `FirstPaidConversionAt?` · `State` · `Currency`
- **Eventos**: `CustomerIdentified` · `CustomerChurned` · `CustomerForgotten{customer_hash, rows_deleted}`

## RevenueEvent *(VO solo-anexable, dentro de `Customer`)*

- **Invariantes**
  1. **Solo-anexable.** Una devolución es un `RevenueEvent` de importe **negativo**, jamás el borrado del cobro original (FR-019, «sin reescribir la historia»).
  2. `source_event_id` único por `(business_id, connector_id)` ⇒ idempotencia de ingesta; una entrega repetida no duplica hechos.
  3. `occurred_at` en la zona del negocio; `observed_at` cuando llegó. Los dos, siempre.
  4. `amount` es `Money` con divisa explícita del mapa vigente; adivinar divisa está prohibido.
  5. Cambiar el mapa de dinero **no** reescribe hechos ya ingeridos: se aplican desde `effective_from` (versión del mapa grabada en el hecho).
- **Atributos**: `RevenueEventId` · `CustomerId` · `Kind` (`first_payment|recurring_payment|refund|churn`) · `Amount: Money` · `OccurredAt`, `ObservedAt` · `SourceEventId` · `MappingVersion`
- **Eventos**: `RevenueEventIngested` · `LateRevenueObserved` (dispara `Restatement`, como ya hace `LateConversionObserved`)

## CustomerValue *(VO derivado — nunca almacenado como verdad)*

`observed_contribution` (Σ de hechos, devoluciones incluidas con su signo) · `projected_contribution` (con la curva de madurez ya existente en `economics`) · `maturity` · `horizon_days` · `is_provisional`.
**Invariante**: sin volumen o sin ventana madura ⇒ **no hay número**, hay estado honesto (FR-009 de spec 026; §7 «no hay número» de `profitability-engine.md`).

## IdentityMapping *(entidad)*

- **Invariantes**: solo-anexable. Fundir dos identidades escribe `merged_into`, **nunca** borra la absorbida — el mismo cliente por dos vías (edge case de `spec.md`) queda trazable. La sal se versiona: `salt_version` acompaña a cada digest.
- **Atributos**: `MappingId` · `BusinessId` · `HashedIdentity` · `MergedInto?` · `SaltVersion` · `ObservedAt`

## CrmBridgeHealth *(entidad — la costura de A-3)*

- **Invariantes**: una fila viva por `(business_id, connector_id)`. `has_recent_events_24h` se **calcula** desde el último `RevenueEvent` ingerido, no se declara; el estado del conector sólo puede **empeorarlo**, nunca mejorarlo.
- **Atributos**: `BusinessId` · `ConnectorId` · `ConnectorState` · `LastEventAt?` · `HasRecentEvents24h` · `Cause?` · `UpdatedAt`
- **Consumo**: `SqlMeasurementFreezeGate` sustituye su `bridge_has_recent_events_24h=True` cableado por esta lectura ⇒ `MEASUREMENT_FROZEN` a nivel de cuenta: **BUY congelado, `pause`/`lower` defensivos intactos**, banner + Telegram.

## Domain events (ads)

`CustomerIdentified` · `CustomerChurned` · `CustomerForgotten` · `RevenueEventIngested` · `LateRevenueObserved` · `CrmBridgeDegraded` · `CrmBridgeRecovered`. Todos con `business_id`, `occurred_at` y `cycle_id`, anexados al `decision_log` vía `DecisionRecorder` (patrón existente).

## Relationships (ads)

```
Business 1─n Customer 1─n RevenueEvent
Customer 0..1─1 LeadAttribution (por HashedIdentity; la escalera ya existente)
Customer n─1 AdEntity (EntityRef, cuando la atribución resolvió campaña)
Business 1─n CrmBridgeHealth ──→ SqlMeasurementFreezeGate (lectura)
Customer ──(derivado)──> CustomerValue ──> UnitEconomicsProfile (collection_rate, refund_rate)
```

## Índices clave (ads)

- `customers`: UNIQUE `(business_id, identity_digest)`; índice `(business_id, first_paid_conversion_at DESC)`; parcial `WHERE state = 'active'`.
- `revenue_events`: UNIQUE `(business_id, connector_id, source_event_id)` — **la idempotencia es del esquema, no del código**; índice `(customer_id, occurred_at)`; índice `(business_id, occurred_at DESC)` para el corte de 24 h del puente.
- `identity_mappings`: UNIQUE `(business_id, identity_digest, salt_version)`; índice `(merged_into)`.
- `crm_bridge_health`: PK `(business_id, connector_id)`.

## Migration plan (handoff a `database-engineer`)

Expand/contract sobre esquema vivo; nombres de revisión Alembic **≤ 32 caracteres** (el arranque que migra entra en bucle si se pasan):

1. **`0030_customers`** *(expand)* — `customers`, `identity_mappings`, con sus UNIQUE e índices. No toca `lead_attributions`.
2. **`0031_revenue_events`** *(expand)* — `revenue_events` con UNIQUE `(business_id, connector_id, source_event_id)`, CHECK `kind IN (...)`, CHECK «`refund` y `churn` ⇒ `amount <= 0`», FK a `customers`. Trigger de no-UPDATE/DELETE (solo-anexable), igual que `decision_log`.
3. **`0032_crm_bridge_health`** *(expand)* — tabla del puente + índice parcial `WHERE has_recent_events_24h = false`.
4. **Sin `contract`.** Nada se borra: `lead_attributions` sigue intacta y `customers` la complementa. La correlación se hace por `identity_digest`, no por FK, para no acoplar dos ciclos de vida distintos.

**Retención y olvido**: `revenue_events` sigue la retención de `metrics_daily` (37 meses). `ForgetCustomer` borra por `identity_digest` en las tres tablas dentro de **una transacción** y anexa `CustomerForgotten{customer_hash, rows_deleted}` al `decision_log` — sin el identificador crudo, que nunca existió aquí.
