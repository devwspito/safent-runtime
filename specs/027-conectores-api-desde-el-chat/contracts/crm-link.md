# Contract — Enlace CRM→anuncios: cliente, ingreso recurrente y valor de cliente

El motivo económico de toda la feature. Un negocio de servicios no gana en la primera venta: gana en el ingreso recurrente neto. Este contrato es el que hace que el compañero de anuncios **mida al cliente entero** en vez de al ticket.

Dos lados: el runtime **lee y traduce**, el compañero **acumula y calcula**. La frontera entre ambos es HTTP y **sólo cruzan digests**.

---

## 1. Chat: proponer y confirmar el enlace

```ts
/** Riesgo LOW. Propone el mapa con ejemplos ANONIMIZADOS. No sincroniza nada (FR-017). */
declare function crm_link_propose(args: {
  connector_id: string;
  business_id: string;
}): Promise<{
  proposal: {
    identity: { source_path: string; kind: "email" | "phone" | "external_id" };
    first_payment:     { source_path: string; amount_path: string; date_path: string };
    recurring_payment: { source_path: string; amount_path: string; date_path: string };
    refund:            { source_path: string; amount_path: string; date_path: string } | null;
    churn:             { source_path: string; date_path: string } | null;
    currency: string;            // explícita SIEMPRE — adivinarla está prohibido
    timezone: string;
  };
  anonymized_examples: Array<Record<string, string>>;   // 3 filas, valores enmascarados
  money_fields: string[];                                // los que exigen tarjeta
  requires_card: true;
}>;

/** Riesgo HIGH · TARJETA ÚNICA y editable (FR-020). Versiona con effective_from;
 *  jamás reescribe historia. Activar el enlace no retro-recalcula nada. */
declare function crm_link_confirm(args: {
  connector_id: string;
  business_id: string;
  mapping: object;                 // el mapa, editado por el dueño si quiere
  cadence: "hourly";
}): Promise<{
  link_id: string;
  mapping_version: number;
  effective_from: string;          // ISO date
  state: "activo";
  identity_salt_version: number;   // la sal viaja UNA vez al compañero, por canal de secreto
}>;

/** Riesgo LOW. El informe del último ciclo, en palabras (FR-021). */
declare function crm_link_report(args: { link_id: string }): Promise<{
  sync_run_id: string; started_at: string; ended_at: string | null;
  outcome: "ok" | "parcial" | "fallido";
  read: number; sent: number; rejected: number; duplicated: number;
  divergence_pct: number;           // > 2 % ⇒ SE NOMBRA en el informe (SC-004)
  divergence_named: string | null;  // "3,1 %: 12 cobros del origen sin identidad resoluble"
}>;

/** Riesgo HIGH · TARJETA. A-2. Borra por digest en ambos lados y ANOTA la supresión. */
declare function crm_forget_customer(args: {
  business_id: string;
  identifier?: string;             // se hashea y se descarta; nunca se persiste
  digest?: string;
}): Promise<{ rows_deleted: { runtime: number; ads: number }; recorded: true }>;
```

---

## 2. Ingesta en el compañero — `safent-ads`

Base `https://ads.<host>/api/v1`. Autenticación: cabecera `X-Bridge-Token` (mismo patrón que `X-Webhook-Token` de `/conversions/webhook`; el runtime no tiene cookie de `ads-api`). `RequireBusinessAccess` como dependencia del router. Errores con la forma canónica `{ "error": { code, message, details } }`.

### `POST /crm/customers`

```
{ business_id, connector_id, sync_run_id,
  items: [ { identity_digest: string,        # hex sha256 — JAMÁS el crudo
             salt_version: int,
             first_seen_at: Timestamp,
             entity_ref: EntityRef | null,   # campaña, si la atribución la resolvió
             attribution_rung: "click_id"|"utm"|"hashed_identity"|"aggregate" } ] }
→ 202 { accepted: int, merged: int, rejected: [{ index, code }] }
   400 IDENTITY_NOT_HASHED   si algún item trae algo que parece un identificador crudo
   409 SALT_VERSION_UNKNOWN  si la sal no fue entregada previamente
```

**Rechazo estructural**: el endpoint valida que `identity_digest` sea hex de 64 caracteres. No es cortesía — es la última barrera antes de que un dato personal entre en una base de datos que no debe tenerlo. Máximo 1 000 items por lote.

### `POST /crm/revenue-events`

```
{ business_id, connector_id, sync_run_id,
  items: [ { source_event_id: string,        # clave de idempotencia
             identity_digest: string,
             kind: "first_payment"|"recurring_payment"|"refund"|"churn",
             amount: Money,                  # refund/churn ⇒ amount <= 0
             occurred_at: Timestamp,         # zona del negocio
             observed_at: Timestamp,
             mapping_version: int } ] }
→ 202 { ingested: int, duplicated: int, rejected: [{ source_event_id, code }] }
   400 CURRENCY_MISMATCH   divisa distinta de la del negocio
   400 REFUND_MUST_BE_NEGATIVE
   409 MAPPING_VERSION_UNKNOWN
```

**Idempotencia por esquema, no por código**: UNIQUE `(business_id, connector_id, source_event_id)`. Reenviar un lote entero es seguro por construcción; un `SyncRun` interrumpido se reanuda sin miedo.
**Solo-anexable**: una devolución **resta** con su propio hecho negativo; nunca se borra ni se corrige el cobro original (FR-019, «sin reescribir la historia»).

### `PUT /crm/bridge-health` — la costura de A-3

```
{ business_id, connector_id, connector_state: "listo"|"degradado"|"suspendido",
  last_event_at: Timestamp | null, cause: string | null }
→ 204
```

El compañero **calcula** `has_recent_events_24h` desde su propio último `RevenueEvent`; el estado que envía el runtime sólo puede **empeorarlo**, nunca mejorarlo. Un runtime comprometido no puede descongelar el gasto mintiendo.

**Efecto**: `SqlMeasurementFreezeGate` deja de cablear `bridge_has_recent_events_24h=True` y lee esta fila ⇒ `is_measurement_broken` ⇒ cuenta en `MEASUREMENT_FROZEN`:
- **BUY congelado**: ninguna propuesta de subida de presupuesto, ni AUTO ni sugerida.
- **Defensivas intactas**: `pause` y `lower` por señal dura siguen ejecutándose. Con medición rota, dejar de gastar es seguro; gastar más, no.
- **Banner** en el cuadro de mando + **aviso por Telegram**, con la causa y desde cuándo.

### `POST /crm/customers/forget`

```
{ business_id, identity_digest }
→ 200 { rows_deleted: int, recorded: true }
```

Borra en `customers`, `revenue_events` e `identity_mappings` **en una transacción** y anexa `CustomerForgotten{customer_hash, rows_deleted, requested_at}` al `decision_log`. Los agregados ya publicados **no se reescriben**: se recalculan al ciclo siguiente y la diferencia se declara en el informe.

---

## 3. Lectura: valor de cliente

```
GET /economics/customer-value?business_id&product_id[&entity_ref]
    → { product_id, entity_ref: string | null,
        cohort_size: int,
        observed_contribution: Money,
        projected_contribution: Money | null,     # null si maturity insuficiente
        projected_low: Money | null, projected_high: Money | null,
        maturity: number, horizon_days: int,
        is_provisional: bool,
        no_number_reason: string | null }          # "cohorte de 4 clientes: sin número"

GET /crm/sync-runs/{sync_run_id}
    → { sync_run_id, link_id, outcome, read, sent, rejected, duplicated,
        divergence_pct, divergence_named: string | null,
        started_at, ended_at }
```

**`no_number_reason` no es un adorno**: sin volumen o sin ventana madura, el contrato devuelve `null` en las cifras y una frase honesta. Nunca un número inventado (`profitability-engine.md §7`, FR-009 de spec 026, SC-006).

**Consumo aguas abajo**: `economics` usa el LTV observado para dejar de suponer `collection_rate(H)` y `refund_rate` (`profitability-engine.md §1: «se infiere del CRM»`), y el cuadro de mando de la spec 026 pinta `clientes`, `valor de cliente` y `ROI` por fila — declarando «incompleto» cuando el puente está caído, en vez de una cifra bonita y falsa.

---

## 4. Invariantes de la costura (las cuatro que no se negocian)

1. **Sólo cruzan digests.** Ningún email, teléfono ni nombre atraviesa esta frontera, en ninguna dirección, en ningún campo, ni siquiera en `details` de un error.
2. **El enlace sólo lee del origen** (FR-022). No existe endpoint que escriba en el CRM. No es que esté cerrado: no está construido.
3. **Nada retro-reescribe.** Cambiar el mapa versiona con `effective_from`; los hechos ya ingeridos conservan su `mapping_version`.
4. **La deriva en un campo de dinero detiene la sincronización** y nada se recalcula hasta que el dueño confirma el mapa nuevo (FR-020). Un campo de dinero mal mapeado envenena toda decisión de gasto: es el único fallo de esta feature que cuesta dinero de verdad.
