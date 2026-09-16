# Contract — Conectores: superficie D-Bus, herramientas del chat y REST de supervisión

Tres superficies, una fuente de verdad. **La gobernanza es D-Bus** (constitución P0.3): crear, verificar, mapear y borrar un conector son verbos del daemon. **El REST es supervisión** (P0.2). **El chat es la puerta de entrada** del dueño, y lo que ejecuta son herramientas del bróker, no un enrutador de palabras clave.

---

## 1. Herramientas del agente (la superficie que usa el dueño)

Skill firmada `connector-setup` (`SKILL.md` en el hub, misma firma y gobernanza que cualquier otra). La skill **describe el procedimiento**; el agente decide cuándo aplicarlo **por comprensión del texto**, jamás por coincidencia de cadenas (FR-001). Las herramientas van registradas en el catálogo del bróker con riesgo declarado **por el servidor**, nunca por el modelo.

```ts
// connectors/tools.d.ts — firmas; sin implementación.

type ConnectorId = string;          // opaco
type Slug = string;                 // ^connector-[a-z0-9]([a-z0-9-]*[a-z0-9])?$
type SecretRef = string;            // referencia; NUNCA el valor
type Iso8601 = string;

type AuthMethod =
  | { kind: "api_key";            header_name: string }
  | { kind: "bearer" }
  | { kind: "basic" }
  | { kind: "client_credentials"; token_endpoint: string; scopes: string[] };

type ConnectorState =
  | "configurando" | "esperando_autorizacion" | "sin_verificar"
  | "listo" | "degradado" | "suspendido";

/** Riesgo LOW · auto_executable. No sale a la red, no toca secretos.
 *  Crea el borrador y devuelve EXACTAMENTE lo que falta, agrupado (FR-003). */
declare function connector_draft(args: {
  friendly_name: string;
  base_url: string;
  auth: AuthMethod;
  spec_url?: string;                 // descripción formal, si la hay
  spec_inline?: string;              // o pegada (≤ 5 MiB)
  examples?: string[];               // o 1-3 respuestas de ejemplo (FR-005)
  liveness_hint?: string;            // operación segura de sólo lectura
}): Promise<{
  connector_id: ConnectorId;
  slug: Slug;
  state: "configurando";
  missing: Array<{ field: string; why: string }>;   // vacío ⇒ listo para autorizar
  hosts_needed: string[];                            // base + token_endpoint
}>;

/** Riesgo HIGH · TARJETA obligatoria, una por host. Host EXACTO, jamás comodín.
 *  No concede nada por su cuenta: presenta la tarjeta y espera (FR-027). */
declare function connector_request_egress(args: {
  connector_id: ConnectorId;
  hosts: string[];
}): Promise<{
  granted: string[];
  pending_card: string[];
  state: ConnectorState;             // "esperando_autorizacion" mientras quede pendiente
}>;

/** Riesgo HIGH · TARJETA. El valor entra por canal de secreto y sale del contexto
 *  del modelo de inmediato: la herramienta devuelve la REFERENCIA, nunca el valor,
 *  y el valor no reaparece en chat, bitácora ni panel (FR-004 de seguridad, NFR-001). */
declare function connector_store_credential(args: {
  connector_id: ConnectorId;
  secret_material: string;           // se consume y se descarta
}): Promise<{ credential_ref: SecretRef; state: ConnectorState }>;

/** Riesgo LOW · auto_executable. EJECUTA DE VERDAD una operación de sólo lectura.
 *  Es el único camino a "listo" (FR-006). Declara lo probado y lo NO probado (FR-007). */
declare function connector_verify(args: {
  connector_id: ConnectorId;
  operation_name?: string;           // por defecto, la candidata de vida
}): Promise<{
  state: "listo" | "sin_verificar";
  proof?: {
    operation_name: string; status_code: number; latency_ms: number;
    succeeded_at: Iso8601; response_shape_hash: string;
  };
  proved_what: string[];
  not_proved: string[];              // nunca vacío si hay > 1 operación
  failure?: { cause: string; what_is_needed: string };   // p.ej. permiso que falta
  published_operations: string[];    // [] mientras no sea "listo"
}>;

/** Riesgo LOW. Estado honesto, para narrarlo en el chat. */
declare function connector_status(args: { connector_id?: ConnectorId }): Promise<{
  connectors: Array<{
    connector_id: ConnectorId; friendly_name: string; slug: Slug;
    state: ConnectorState; host: string; auth_kind: AuthMethod["kind"];
    last_checked_at: Iso8601 | null;
    health: { verdict: "sano" | "degradado"; cause?: string; last_good_at?: Iso8601 };
    incomplete_derivatives: string[];      // FR-026: qué queda incompleto
  }>;
}>;

/** Riesgo HIGH · TARJETA. Revoca credencial Y concesión de egress (FR-010). */
declare function connector_delete(args: { connector_id: ConnectorId }): Promise<{
  deleted: true; credential_revoked: true; hosts_revoked: string[];
}>;
```

**Prohibido en v1**: cualquier herramienta que escriba en el origen. No están «desactivadas»: **no existen** (`RouteMap … EXCLUDE` al derivar el catálogo). Cuando lleguen (P3) serán `RiskLevel.HIGH`, `auto_executable = false` y tarjeta por llamada.

---

## 2. Verbos D-Bus (`org.hermes.Runtime1`) — la gobernanza

Autoría por `sender_uid` del bus, nunca por payload. Todos con validación de esquema obligatoria por verbo (el decorador de §4.6 de la radiografía, no `json.loads` crudo).

| Verbo | Entrada | Salida | Nota |
|---|---|---|---|
| `CreateConnectorDraft` | JSON acotado | `{connector_id, missing[]}` | No sale a la red |
| `StoreConnectorCredential` | `{connector_id, secret}` | `{credential_ref}` | Único escritor del vault para conectores |
| `VerifyConnector` | `{connector_id, operation_name?}` | `{state, proof, proved_what, not_proved}` | **Encola** un `WorkItem`; el loop lo drena |
| `ConfirmFieldMapping` | `{connector_id, version, rules[]}` | `{mapping_version_id, effective_from}` | Exige tarjeta si hay campo de dinero |
| `RotateWebhookSecret` | `{webhook_id}` | `{shown_once: true}` | El anterior sigue válido hasta la primera entrega con el nuevo |
| `ForgetCustomer` | `{business_id, identifier \| digest}` | `{rows_deleted, recorded: true}` | A-2; propaga al compañero |
| `DeleteConnector` | `{connector_id}` | `{credential_revoked, hosts_revoked[]}` | |

**Fallos**: el daemon codifica el rechazo como `{"ok": false, "error": …}` (D-Bus no lleva excepciones). La capa REST **nunca** lo reporta como 2xx — `blocked:true` ⇒ `403`, cualquier otro `ok:false` ⇒ `400`. Mismo criterio que `mcp_api.py`; el `{ok:false}` bajo 200 es exactamente la forma sobre la que un `try/catch` no salta nunca.

---

## 3. REST de supervisión — `/api/v1/connectors`

Lectura con la misma autenticación que el resto del plano de control (dependencia en el `APIRouter`, no disciplina por handler). **Nada de crear conectores por HTTP.**

```
GET  /api/v1/connectors
     → { items: [{ connector_id, friendly_name, slug, state, host, auth_kind,
                   last_checked_at, health: { verdict, cause, last_good_at },
                   operations_published: int, incomplete_derivatives: [string] }] }

GET  /api/v1/connectors/{connector_id}
     → { ...ficha..., catalogue: [{ operation_name, method, path_template, is_read_only }],
         proof: { operation_name, status_code, succeeded_at, latency_ms } | null,
         proved_what: [string], not_proved: [string],
         mapping: { version: int, effective_from: date, has_money_fields: bool } | null }
     404 si no existe (nunca 403: no filtra existencia)

GET  /api/v1/connectors/{connector_id}/health-history?limit=50
     → { items: [{ checked_at, verdict, cause, attempts: [{action, at, outcome}] }] }

POST /api/v1/connectors/{connector_id}/verify        # passthrough fino → D-Bus VerifyConnector
POST /api/v1/connectors/{connector_id}/sync-now      # passthrough fino → encola un SyncRun
     Ambos: OperatorToken firmado (call_mutator), 503 fail-hard si el daemon no responde,
     202 { work_item_id } — no ejecutan, ENCOLAN (constitución P0.1).
```

**Jamás expuesto por REST**: valor de secreto, sal de identidad, cuerpo de respuesta del origen, identificador crudo de cliente.

---

## 4. Registro MCP — un servidor por conector

Al alcanzar `listo`, `ToolRegistryPort` registra:

```
slug          connector-<friendly-slug>
transport     stdio · argv: [python3, -m, hermes.connectors.runner, --connector-id, <id>]
              env: SIN secretos (allowlist de _build_mcp_env); la credencial se
                   resuelve por llamada contra el vault por socket unix
trust_level   MANAGED_REMOTE
```

`MANAGED_REMOTE` no es una etiqueta cómoda: es la postura correcta y ya escrita. Lecturas fluidas (`LOW` + `auto_executable`); **escrituras nunca auto-ejecutables**; **toda respuesta del origen es contenido no confiable** (`taint` «mcp») ⇒ un origen comprometido no puede conducir escrituras ni ampliar permisos (FR-029).

`_MANAGED_REMOTE_MCP_SLUGS` pasa de conjunto fijo a **predicado**: `slug in {"safent-control","safent-ads"} or (slug.startswith("connector-") and slug in managed_remote_endpoints)`. **Regla de oro intacta**: el host se resuelve **sólo** desde fuente local autorizada por el dueño, nunca desde el argv/env del bundle.

Al pasar a `degradado` o `suspendido`, el servidor se desregistra: las operaciones **desaparecen** del catálogo (FR-008), no se quedan rotas.

---

## 5. Ritmo

Un job del catálogo `cron` único por conector (`trigger_gate.py` conserva la autorización), desfasado para que no coincidan: chequeo de salud horario y, si hay enlace, `SyncRun` horario. Cada disparo **encola** un `WorkItem`; nada razona fuera de la cola del daemon.
