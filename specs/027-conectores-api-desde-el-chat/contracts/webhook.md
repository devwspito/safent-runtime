# Contract — Webhook entrante

El dueño no configura un webhook: **pide uno**. Safent entrega dirección y secreto, el dueño los pega en el origen, y la primera entrega válida —no la promesa de una— es lo que pone el conector en `listo`.

Como Safent **emite** la dirección y el secreto, Safent **elige el esquema de firma**: Standard Webhooks (`standardwebhooks==1.1.0`, MIT). Un origen que imponga el suyo (Stripe, GitHub) es P3 y hasta entonces se declara incompatible en voz alta, en vez de aceptarse sin firma.

---

## 1. Crear el webhook (desde el chat)

```ts
/** Riesgo HIGH · TARJETA (guarda un secreto). El secreto se muestra UNA SOLA VEZ
 *  y se avisa de que no se repite (FR-011). No vuelve a aparecer jamás. */
declare function connector_create_webhook(args: {
  connector_id: string;
  purpose: string;                    // "cobros del CRM", en palabras del dueño
}): Promise<{
  webhook_id: string;
  inbound_url: string;                // https://<host>/api/v1/webhooks/inbound/<opaque>
  secret_shown_once: string;          // whsec_...  ← única aparición en toda su vida
  signature_scheme: "standard_webhooks";
  state: "esperando_primera_entrega";
  paste_instructions: string;         // en castellano, listo para copiar
}>;
```

`inbound_url` lleva un identificador **opaco y de alta entropía**: ni el nombre del conector ni el del negocio. La URL sola no autoriza nada — sin firma válida no hay entrega aceptada.

---

## 2. Recibir la entrega — `POST /api/v1/webhooks/inbound/{opaque_id}`

**Sin sesión.** El CRM del dueño no tiene cookie de Safent; se autentica con la firma. Es la única superficie de este diseño que acepta tráfico no autenticado por sesión, y por eso es la más estrecha.

**Cabeceras** (Standard Webhooks):

```
webhook-id          identificador único de la entrega   → clave de idempotencia
webhook-timestamp   epoch en segundos                    → tolerancia ±5 min
webhook-signature   v1,<base64(HMAC-SHA256(id.timestamp.payload))>
                    admite VARIAS firmas separadas por espacio → rotación atómica
```

**Puerta de entrada, en orden y fail-closed** — se rechaza en la primera que falle:

| # | Comprobación | Fallo ⇒ |
|---|---|---|
| 1 | `Content-Length` ≤ 1 MiB | `413`, registrada, estado inalterado |
| 2 | `opaque_id` existe y su webhook no está `suspendido` | `404` (no filtra existencia) |
| 3 | Las tres cabeceras presentes | `400` |
| 4 | `|now − webhook-timestamp| ≤ 300 s` | `400` (defensa de reintento) |
| 5 | Firma válida contra el secreto **vigente o el anterior**, comparación en tiempo constante | `401` |
| 6 | `webhook-id` no visto en la ventana de 24 h | `200 {"status":"duplicate"}` — **no duplica hechos** (FR-015) |
| 7 | Cuerpo es JSON y encaja con el mapa vigente | `202` + `MappingDriftDetected` si no |

**Respuesta**: `202 { "status": "accepted", "delivery_id": "<webhook-id>" }`. Nunca se devuelve traza, ni el motivo exacto de un fallo de firma, ni eco del cuerpo.

**Efectos de la primera entrega válida**: `first_delivery_at` se fija, el webhook pasa a `listo`, **se avisa en el chat** y se muestra la forma de la carga (claves y tipos, valores anonimizados) para proponer el mapa.

**Límite de ritmo**: 600 entregas/min por webhook; por encima, `429` con `Retry-After`. El desbordamiento **no** degrada el conector: es el origen quien va rápido.

---

## 3. Muestras de carga

```
GET /api/v1/connectors/{connector_id}/webhook/samples
    → { items: [{ received_at, shape: { "<key>": "<type>" },
                  redacted_sample: {...},   # valores anonimizados, nunca crudos
                  expires_at }] }           # 30 días desde la recepción
```

Las muestras caducan y se purgan solas. Una muestra es la **forma**, no el dato: los valores que parezcan identificadores personales se sustituyen por su tipo antes de guardarse, en el borde, antes de tocar disco (NFR-002).

---

## 4. Rotar el secreto

```
D-Bus  RotateWebhookSecret { webhook_id } → { secret_shown_once, previous_valid_until }
```

El anterior sigue aceptándose hasta la **primera entrega firmada con el nuevo** (o 24 h, lo que ocurra antes): sin ventana ciega, sin entregas perdidas. Después queda invalidado y cualquier entrega con él es `401` registrada. Rotar exige tarjeta.

---

## 5. Lo que un webhook NO puede hacer

Es la frontera más importante de este contrato, y es corta:

- **No amplía permisos.** Nada en el cuerpo concede egress, cambia un mapa, activa un enlace ni crea una herramienta (FR-029). El cuerpo es dato, jamás instrucción.
- **No es contenido confiable.** Lo que llega queda marcado (`taint`) igual que cualquier respuesta MCP: si el ciclo del agente lo lee, las escrituras se elevan a HITL forzado.
- **No entra en el contexto del modelo con datos personales.** El hasheo ocurre en el borde, antes del mapeo.
- **No cambia el estado del conector al fallar.** Una entrega rechazada se registra y se acabó: un tercero no degrada un conector nuestro a base de basura firmada mal.
