# Contrato — `GET /api/v1/cockpit` (FR-004 … FR-009)

Un solo endpoint, una sola instantánea: cabecera de cartera + filas de cotización + franja de cambios.
Se **compone** de los read models existentes; no hay SQL nuevo donde ya existe fuente.

## 1. Petición

`GET /api/v1/cockpit?business_id=<uuid>&window=7d`

`business_id` obligatorio (`require_business_access`: 401 sin sesión, 404 si no existe — nunca 403).
`window` ∈ `{today, 7d, 30d}`, por defecto `7d` (Assumption 4 de `spec.md`). **No hay parámetros de
filtro ni de orden**: se devuelve la cartera entera y el cliente filtra/ordena (FR-006, estado en la URL);
así el tablero y `GET /portfolio` no pueden divergir (SC-003).

Cabeceras: `ETag` + `Cache-Control: private, no-store`; `If-None-Match` → `304`. Memoización servidora por
`(business_id, window)` con TTL = mitad de la ventana de frescura (tope 60 s). El cliente **nunca** vacía lo
leído en un fallo: conserva la última instantánea y marca la frescura (NFR-001).

## 2. `Measure<T>` — el envoltorio que hace imposible fabricar cifras

Toda celda numérica de FR-005/FR-007 viaja así. No existe la forma "número suelto" (FR-009, SC-006):

```ts
type MeasureStatus =
  | "available"           // hay valor
  | "no_data"             // sin dato en la ventana
  | "insufficient_volume" // n por debajo del mínimo del motor
  | "immature_window"     // LagCurve: maturity < umbral de la acción
  | "learning"            // entidad en aprendizaje
  | "not_controllable"    // presupuesto compartido / sin palanca
  | "no_customer_source"  // CRM de clientes no conectado (spec 027)
  | "stale"               // fuera de la ventana de frescura
type Measure<T> = { status: "available"; value: T } | { status: Exclude<MeasureStatus,"available">; value: null; reason: string }
type Money = { amount: string; currency: string }   // decimal como cadena, nunca float
```

## 3. Respuesta

```ts
interface CockpitView {
  business_id: string; window: "today"|"7d"|"30d"; currency: string;
  generated_at: string; freshness: { last_ingested_at: string; lag_minutes: number; is_stale: boolean };
  is_partial: boolean;                                   // NFR-006: total incompleto declarado
  degraded_accounts: { platform_account_id: string; platform: string; status: string; reason: string }[];
  header: PortfolioHeader; rows: TickerRow[]; changes_since: ChangeStrip;
}

interface PortfolioHeader {                              // FR-007
  spend: { today: Money; mtd: Money; window: Money };
  caps:  { daily: Money|null; monthly: Money|null; source: "guardrail"|"broker_hard_cap" };
  pacing: { index_pct: number; projection_pct: number; days_remaining: number };
  projected_month_end: Measure<Money>;
  leads:     { today: Measure<number>; week: Measure<number> };
  customers: { today: Measure<number>; week: Measure<number> };   // no_customer_source hasta 027
  roi:  Measure<number>; roi_basis: "contribution"|"revenue"; roi_basis_reason: string|null;
  roas: Measure<number>;
  cost_per_lead: { actual: Measure<Money>; target: Measure<Money>; delta_pct: Measure<number> };
  brake: { engaged: boolean; mode: "AUTONOMOUS"|"ALL"|null; since: string|null };
  proposals: { pending: number; deferred: number; critical: number };
}

interface TickerRow {                                    // FR-005, una por campaña o conjunto
  entity_ref: string; name: string; level: "campaign"|"ad_set";
  platform: string; platform_account_id: string; status: string;
  signal: { signal_id: string; kind: "BUY"|"HOLD"|"SELL"|"EXIT"; strength: number; cause: string; emitted_at: string } | null;
  money_at_stake: Money;                                 // orden por defecto, DESC (FR-006)
  expected_contribution_delta: Measure<Money>;           // columna ordenable alternativa
  roi: Measure<number>; roas: Measure<number>;
  leads: Measure<number>; customers: Measure<number>; customer_value: Measure<Money>;
  cost_per_lead: { actual: Measure<Money>; target: Measure<Money>; delta_pct: Measure<number> };
  spend: Money; cap: Money|null; pacing_index_pct: Measure<number>;
  sparkline: { metric: "spend"; points: number[] };      // 14 puntos diarios, `spend_14d`
  freshness: { lag_minutes: number; is_stale: boolean };
  is_controllable: boolean; is_degraded: boolean;
  learning_state: { is_learning: boolean; reason: string|null; since: string|null };
  action: RowAction;
}

interface RowAction {                                    // FR-010, FR-011, FR-013
  kind: "approve_increase"|"apply_decrease"|"apply_exit"|"review_proposal"|"none";
  mode: "inline_approval"|"autonomous_applied"|"proposal"|"blocked";
  friction: "none"|"expand_evidence"|"typed_confirmation"|"fresh_totp";
  proposal_id: string|null;
  applied_change: { parameter: string; before: string; after: string; applied_at: string; undo_deadline: string } | null;
  blocked_reason: "brake_engaged"|"stale_data"|"guardrail"|"not_controllable"|"immature_window"|null;
  target: { method: "POST"|"PATCH"; path: string };      // ruta EXISTENTE de propuestas/ejecución
}

interface ChangeStrip {                                  // FR-008
  since: string;                                         // último cierre diario
  is_partial: boolean;
  items: { kind: "signal_changed"|"entity_entered"|"entity_exited"|"autonomous_applied"|"threshold_crossed";
           entity_ref: string; entity_name: string; before: string|null; after: string|null;
           occurred_at: string; detail_ref: string|null }[];
}
```

## 4. Procedencia — de dónde sale cada campo (no se inventa fuente)

| Bloque | Fuente existente |
|---|---|
| `spend`, `caps`, `pacing`, `freshness`, `is_partial`, `degraded_accounts` | `PortfolioView` (`panel.infrastructure.sql_read_model`) |
| `leads`, `customers` | `PortfolioView.conversions_by_kind` (`lead`, `business_conversion`) |
| `cost_per_lead.actual` | `PortfolioView.cost_per_lead` · `.target` ← `UnitEconomicsProfile.target_cost_per_lead()` |
| `roas` / `roas` objetivo | gasto e ingreso de `metrics` · `UnitEconomicsProfile.target_roas()` |
| `roi` | contribución: `business_conversion × contribution_margin − spend) / spend`; sin fuente de clientes → `basis:"revenue"` |
| `expected_contribution_delta` | `optimization` (`MarginalEstimate`); `inconclusive` → `insufficient_volume` |
| filas, `money_at_stake`, `sparkline`, `learning_state`, `is_controllable` | `PortfolioRow` (incluye `spend_14d`) |
| `signal` | `SignalView` / `list_signals` (última vigente por entidad) |
| `action`, `friction`, `proposal_id` | `proposals` (clasificación + autorización) y `execution` (cola, `undo_policy`) |
| `brake`, `proposals.*` | `Badges` (`get_badges`) |
| `changes_since` | log de decisiones (`audit`) + señales emitidas desde el cierre + entradas/salidas de la cartera |

**Reglas de honestidad** (verificables una a una): `is_learning` → toda métrica de rendimiento
`learning`; `is_controllable=false` → `action.kind="none"` y `not_controllable`; `freshness.is_stale` →
todas las filas `stale` y `action.mode="blocked"` con `blocked_reason="stale_data"` (dato obsoleto, sin
escritura); `maturity < 0,60` bloquea `approve_increase` (`immature_window`), `< 0,30` bloquea también
`apply_decrease`/`apply_exit`; perfil económico `provisional` → `roi.status="insufficient_volume"` y
ninguna subida; cuenta caída → sus filas `is_degraded` y `is_partial=true` en la cabecera; **cero jamás
sustituye a "sin dato"**.

## 5. Fricción por acción (Assumption 2 del plan)

| Acción | `friction` |
|---|---|
| `SELL`/`EXIT` aplicada en autónomo, dentro de guardarraíl | `none` + ventana de gracia (`undo_deadline`) |
| Aprobar `BUY` (subida de gasto) | `expand_evidence` + `fresh_totp` |
| Activar el freno | `none` (una pulsación + confirmar, FR-014) |
| Desactivar el freno · subir un tope · activar autonomía | `typed_confirmation` + `fresh_totp` |

## 6. Puertos (safent-ads)

```py
class CockpitReadPort(Protocol):                       # panel/application/ports.py, Protocol propio (ISP)
    async def get_cockpit(self, business_id: str, *, window: str) -> CockpitView: ...
    async def get_changes_since(self, business_id: str, *, since: datetime) -> ChangeStrip: ...
```
Adaptador: `panel/infrastructure/sql_cockpit_read_model.py` — compone los read models citados en §4;
`panel` sigue sin importar `signals`/`accounts` (plan.md §4 de la 001). DTOs en
`panel/application/cockpit_dto.py`; `Measure`/`MeasureStatus` en `shared/read_models/dto.py`
(los reusará `mcp` sin redeclarar).
