# Research — Conectores de API desde el chat (Fase 0)

2026-09-10 · `software-architect` · Entrada: `spec.md` (484b6aa), constitución v1.1.0, `specs/025-safent-repaso/radiografia.md`, la jaula (`egress_api.py`, `SecretsVault`, `mcp_api.py`, `dbus_runtime_service.py`, `hermes-mcp-launcher`) y el compañero `safent-ads` (`crm/`, `economics/`, `metrics/domain/reconciliation.py`).

**Criterio rector**: minimizar el código que Safent mantiene. Lo que Safent **no puede delegar** es exactamente lo que la jaula define — obtener la descripción formal con guarda SSRF y tope de tamaño, salir sólo por el proxy de egress, resolver la credencial desde el almacén, limitar el ritmo, encadenar la bitácora y publicar la herramienta por el bróker. Todo lo demás (parsear OpenAPI, derivar el esquema de la herramienta, colocar parámetros en ruta/consulta/cabecera/cuerpo, firmar y verificar webhooks, canjear credenciales de cliente) es problema resuelto: se consume, no se escribe.

---

## Decisión: derivar operaciones desde una descripción formal (OpenAPI → herramientas)

- **Elegido**: **WRAP** de `FastMCP.from_openapi()` — `fastmcp-slim[server]==4.0.3` (Apache-2.0, PrefectHQ/fastmcp, 27.6 k ★, último push 2026-09-09), consumido **dentro del proceso runner por conector**, nunca en el daemon.
- **Por qué**:
  - Resuelve la parte cara y sutil: `$ref` internos, colocación por `in` (`path|query|header|cookie`), cuerpos compuestos, arrays/`deepObject`, `operationId` → nombre de herramienta (slug, 56 chars). Es código de serialización donde un error no produce una excepción sino **una llamada silenciosamente incorrecta** — el peor fallo posible contra FR-006/FR-007.
  - **La inyección encaja con la jaula sin torcerla**: `from_openapi(spec: dict, client: AsyncClient, route_maps=...)`. Safent entrega el spec **ya descargado, validado y acotado** (FastMCP nunca ve una URL, luego no puede hacer SSRF) y **el cliente HTTP ya configurado** (proxy de egress, timeouts, sin redirecciones, hook de credencial contra el vault). No hay superficie donde el conector elija a dónde salir.
  - `RouteMap(methods=["POST","PUT","PATCH","DELETE"], mcp_type=EXCLUDE)` implementa la aclaración (a) **por ausencia**: las operaciones de escritura ni siquiera existen en el catálogo v1 — FR-008 («ausentes, no presentes-y-rotas») se cumple con una línea de configuración en vez de con una puerta que se puede olvidar.
  - `mcp<3.0.0,>=2.0.0` es compatible con el pin duro del Containerfile (`mcp==2.0.0`, determinismo con `hermes-agent 0.21.1`).
- **Descartadas**:
  - **Escribir la derivación** sobre `openapi-pydantic` (MIT) + `jsonref` (MIT) — ~400 líneas de serialización propia, mantenidas para siempre, sin ganancia funcional. Es exactamente el «puerto con una sola implementación» que el dueño señala como sospechoso. *(Alternativa más simple en dependencias, rechazada; se refleja en `plan.md → Complexity Tracking`.)*
  - **`mcp-openapi-proxy` 0.4.0** (MIT, matthewhand, 154 ★, activo) — **incompatible con la jaula, no por calidad sino por postura**: la credencial viaja en `API_KEY` de entorno (viola «secretos nunca en argv/env/logs»), `MCP_ALLOWED_HOSTS` por defecto `*` (falla abierto), `IGNORE_SSL_SPEC`/`IGNORE_SSL_TOOLS` desactivan verificación TLS, y **descarga el spec él mismo desde una URL arbitraria** (SSRF fuera de nuestra guarda). Adoptarlo obligaría a parchear las cuatro cosas: más trabajo que envolver FastMCP.
  - **`janwilmake/openapi-mcp-server`** (MIT, 900 ★, TypeScript) — modelo de **proxy alojado** (openapisearch): el spec del dueño sale de la máquina hacia un tercero. Además, runtime Node adicional dentro del contenedor.
  - **Generadores tipo Speakeasy / Stainless** — generan SDK en **tiempo de build**. Aquí el spec lo pega el dueño en **tiempo de ejecución**: generar y ejecutar código a partir de una entrada no confiable dentro de la jaula es ejecución arbitraria con otro nombre. Además son SaaS: el spec viaja fuera. Rechazo categórico, no de matiz.
- **Riesgos**: (1) FastMCP 4.x es joven (4.0.3, sept-2026) → **pin exacto** + prueba de contrato con spec dorado que fija el esquema derivado de tres operaciones; si el esquema cambia, la prueba rompe antes que producción. (2) El árbol de `fastmcp-slim[server]` añade ~12 paquetes transitivos al contenedor (`httpx2`, `authlib`, `joserfc`, `openapi-pydantic`, `jsonschema-path`, `py-key-value-aio`, `cyclopts`, `griffelib`, `uncalled-for`, `watchfiles`, `pyperclip`, `opentelemetry-api`) → pins exactos, `security_center` los escanea y `pip-audit` los cubre; el runner corre en netns por defecto-deniega y Landlock, no en el daemon. (3) FastMCP advierte que un servidor auto-convertido rinde peor que uno curado → mitigado porque el catálogo se filtra a lectura y la skill nombra las operaciones útiles.
- **Links**: FR-005, FR-006, FR-008, FR-009, NFR-003; constitución §IV, §Restricciones (lazy-import).

## Decisión: recepción y verificación de webhooks

- **Elegido**: **USE** `standardwebhooks==1.1.0` (MIT; especificación Apache-2.0, standard-webhooks/standard-webhooks, 1.7 k ★, push 2026-09-03) para el esquema **que emite Safent**.
- **Por qué**: Safent entrega dirección y secreto (FR-011), luego Safent elige el esquema. Standard Webhooks trae hecho lo que siempre se hace mal a mano: HMAC-SHA256 sobre `id.timestamp.payload`, comparación en tiempo constante, tolerancia de reloj de 5 min y cabeceras (`webhook-id`, `webhook-timestamp`, `webhook-signature`) con **rotación por múltiples firmas simultáneas** — que es justamente FR-015 («rotar el secreto invalida el anterior», sin ventana ciega). El `webhook-id` da además la clave de idempotencia para no duplicar hechos.
- **Descartadas**: **`svix` 2.4.0** (MIT) — es el SDK de *envío* del servicio alojado; nosotros recibimos. **HMAC propio** — reescribir tolerancia de reloj y comparación constante es el autogol clásico; además nos quedaríamos sin el formato de firma múltiple que hace atómica la rotación.
- **Riesgos**: un origen que **no deja elegir** el esquema (Stripe-Signature, X-Hub-Signature-256) no encaja; se declara **P3** con un `WebhookSignatureScheme` adicional, y en v1 el conector queda `sin verificar` diciendo exactamente eso, en vez de aceptar entregas sin firma.
- **Links**: FR-011, FR-012, FR-013, FR-015; US3.

## Decisión: autenticación OAuth2 client-credentials

- **Elegido**: **USE** `authlib>=1.7` (BSD-3-Clause, authlib/authlib, 5.4 k ★, push 2026-08-31), `AsyncOAuth2Client` en modo `client_credentials` **exclusivamente**.
- **Por qué**: canje, caducidad, renovación anticipada y desviación de reloj resueltos y auditados; llega ya como dependencia transitiva de `fastmcp-slim[server]`, luego **coste marginal cero**. Es además la pieza que el dueño ya nombró como preferente frente a lo propio.
- **Descartadas**: `oauthlib`/`requests-oauthlib` — síncronos, y el runner es asyncio. **Canje a mano** — carreras de renovación concurrente y expiración mal calculada; ninguna ganancia.
- **Riesgos**: el `token_endpoint` es **otro dominio** distinto del `base_url` → exige su **propia tarjeta de egress** (nunca se concede implícitamente); el `client_secret` sale del `SecretsVault` por llamada y jamás toca `env` ni `argv`. Flujos de usuario (authorization_code, device) quedan **fuera de v1**: exigen navegador y redirección, y el conector no es una sesión humana.
- **Links**: FR-002, FR-027; NFR-001.

## Decisión: descarga y validación de la descripción formal

- **Elegido**: **WRITE mínimo** — `SpecFetcher` propio (~60 líneas) que **reutiliza la guarda SSRF única compartida** del repo (`resolve-then-connect`, sin copias) y aplica topes duros; el parseo lo hace `openapi-pydantic` (ya presente vía FastMCP).
- **Por qué**: es precisamente lo indelegable. Topes: ≤ 5 MiB comprimido y ≤ 8 MiB expandido, ≤ 2 000 operaciones, profundidad de `$ref` ≤ 20, **`$ref` externos rechazados** (un `$ref` remoto es SSRF con esmoquin), `Content-Type` JSON/YAML, 15 s totales, sin redirecciones cross-host. Salida por el proxy de egress igual que cualquier llamada.
- **Descartadas**: **`openapi-spec-validator` 0.9.0** — arrastra `jsonschema-path`, `openapi-schema-validator`, `lazy-object-proxy` y `pydantic-settings` para responder una pregunta que no nos importa («¿es el spec conforme al meta-esquema?»). La pregunta útil es **«¿parsea y produce al menos una operación de lectura?»**, y esa la responde el propio parseo. **`prance`** — resuelve `$ref` externos por diseño: lo contrario de lo que queremos.
- **Riesgos**: orígenes sin descripción formal → camino por ejemplos (FR-005): el dueño pega 1-3 respuestas, la skill infiere un esquema por observación y el conector nace con **una sola operación declarada**, marcada `derivada de ejemplos` en el panel y en la bitácora. Nunca se anuncia más de lo demostrado.
- **Links**: FR-005, edge «origen sin descripción formal»; riesgo «dirección base hacia red interna».

## Decisión: exposición como herramientas y ciclo programado

- **Elegido**: **USE lo existente, cero mecanismo nuevo.** Un servidor MCP stdio **por conector**, con `ServerSlug = connector-<friendly-slug>`, lanzado por `hermes-mcp-launcher` (netns por defecto-deniega + Landlock, ya endurecido) y registrado por el verbo D-Bus `add_mcp_server` con `TrustLevel.MANAGED_REMOTE`. El ritmo, por el catálogo de cron ya único (`hermes_cli.cron` vía `tasks/triggers`, `trigger_gate.py` conserva la autorización).
- **Por qué**: `MANAGED_REMOTE` describe **exactamente** esta postura y ya está escrita en `mcp/domain/value_objects.py`: código de primera parte que egresa a un servicio de terceros → lecturas fluidas (`LOW` + `auto_executable`), **escrituras nunca auto-ejecutables**, y **toda respuesta es contenido no confiable** (`taint` «mcp») — que es FR-029 (el diputado confundido) sin escribir una línea. La concesión de egress por conector reutiliza el mismo fichero de grants y el mismo socket de control que ya usa el dueño.
- **Descartadas**: **`TrustLevel.BUILTIN`** — daría fluidez a un host de terceros; prohibido explícitamente en el propio comentario del código. **Un servidor MCP único con todos los conectores dentro** — un conector caído tumbaría los demás (NFR-003) y borraría la frontera de credenciales. **Scheduler propio** — la radiografía ya declaró `cron` como catálogo único (fila 7).
- **Riesgos**: `_MANAGED_REMOTE_MCP_SLUGS` es hoy un `frozenset` de dos elementos; hay que convertirlo en **predicado** que acepte además los slugs `connector-*` presentes en el almacén local autorizado por el dueño (`managed_remote_endpoints`), **manteniendo la regla de oro**: el host se resuelve **sólo** desde fuente local, jamás desde el argv/env del bundle. Cambio pequeño, en un fichero que la oleada 3 de la spec 025 ya va a partir.
- **Links**: FR-009, FR-010, FR-021, FR-029, NFR-003; constitución P0.5/P0.6.

## Decisión: identidad de cliente y borde de PII

- **Elegido**: **USE** `HashedIdentity.compute()` del compañero, **replicando el algoritmo en el borde del runtime** (sha256 sobre `salt:identificador-normalizado`), con sal por negocio derivada de `SecretsVault.derive_subkey(label="crm-identity-<business_id>")`.
- **Por qué**: el compañero ya ingiere identidades irreversibles y ya prohíbe el dato personal en `lead_attributions` (`test_no_pii_in_model_context`, threat-model C-31). Hashear **antes** de que el dato salga del proceso conector significa que ni el modelo, ni la bitácora, ni la red, ni el compañero ven jamás un email — NFR-002 deja de ser una promesa y pasa a ser una imposibilidad estructural.
- **Descartadas**: **enviar crudo y hashear en el compañero** — mueve la frontera al sitio equivocado y mete PII en tránsito. **Tokenización reversible** — el enlace sólo necesita agrupar, nunca des-hashear; la reversibilidad es una responsabilidad sin cliente.
- **Riesgos**: (1) La sal debe ser **idéntica** en ambos lados o los hechos no agrupan → la sal se deriva en el runtime y se **entrega una vez** al compañero al crear el enlace, por el mismo canal que el token de webhook, y se versiona; rotarla **reetiqueta la historia**, luego exige tarjeta y se documenta como operación P3. (2) Colisión de identidad (mismo cliente por email y por teléfono) → `identity_mappings` con `merged_into` solo-anexable, nunca borrado.
- **Links**: FR-018, NFR-002, SC-006; `safent-ads` `crm/domain/hashed_identity.py`.

---

## Aclaraciones resueltas (Assumptions — el dueño puede revocarlas)

### A-1 · Escritura (`[NEEDS CLARIFICATION: escritura]`)

**v1 = LECTURA + webhook entrante.** Las operaciones que modifican el origen **no se derivan al catálogo** (`RouteMap … EXCLUDE`): no existen, no se pueden invocar por error ni por inyección. Escribir es una capacidad **P3** que, cuando llegue, será una herramienta declarada una a una por el dueño, `RiskLevel.HIGH`, **nunca `auto_executable`**, con tarjeta por llamada y diff antes/después en la bitácora. El enlace CRM→anuncios **jamás** escribe en el origen (FR-022), ni en P3.
**Por qué así**: FR-028 lo exige y `MANAGED_REMOTE` ya lo garantiza; además evita que un spec con 300 endpoints publique 200 mutaciones el primer día.

### A-2 · Derecho al olvido (`[NEEDS CLARIFICATION: derecho al olvido]`)

**No hay PII bruta que purgar, por construcción.** El identificador se hashea en el borde (A-1 de privacidad, NFR-002): lo derivado guarda `customer_hash`, nunca el email. La propagación se implementa como una operación explícita **«olvidar cliente»**: se aporta el identificador (o su hash), se borra **toda fila con ese hash** en `customers`, `revenue_events` e `identity_mappings` de ambos lados, y se **anota la supresión** en la bitácora solo-anexable como `CustomerForgotten{customer_hash, requested_at, rows_deleted}` — sin el identificador. Los agregados ya calculados **no se reescriben**: se recalculan al ciclo siguiente y la divergencia se declara en el informe (FR-020: nunca reescribir historia).
**Por qué así**: satisface el borrado real sin abrir un camino de des-hasheo, y la anotación de la supresión es la prueba de cumplimiento que el borrado silencioso no deja.

### A-3 · Degradado y gasto (`[NEEDS CLARIFICATION: degradado y gasto]`)

**Un conector de CRM `degradado` deja al negocio en el mismo estado que «medición rota».** Se reutiliza la puerta que ya existe: `is_measurement_broken(..., bridge_has_recent_events_24h=...)` — hoy **cableado a `True`** en `SqlMeasurementFreezeGate:69`. Se conecta al puente real: sin hechos recientes del conector en 24 h, `is_frozen → True`, `MEASUREMENT_FROZEN` a nivel de **cuenta** ⇒ **BUY congelado**, acciones **defensivas** (`pause`/`lower` por señal dura) intactas, banner en el cuadro de mando y aviso por Telegram.
**Por qué así**: no inventa un estado nuevo — usa el que el motor ya sabe interpretar, y el parámetro estaba puesto esperando esta fuente. Congelar sólo la subida (y no toda actuación) es la asimetría correcta: con medición rota, dejar de gastar es seguro; gastar más, no.
**Coste**: si el CRM cae un fin de semana, el negocio no sube presupuesto hasta que vuelva. Aceptado: es exactamente lo que el dueño pediría si se le preguntara a las 3 de la mañana.

---

## Assumptions adicionales (reversibles, documentadas)

1. Nombres de identificador en inglés, prosa e interfaz en castellano (NFR-005).
2. El slug del conector es `connector-<friendly-slug>`; renombrar cambia la etiqueta, **no** el slug (FR-004: la historia se conserva).
3. Ciclo de salud y de sincronización: **horario**, desfasado por conector para no coincidir.
4. Muestras de webhook: **30 días**, luego purga automática.
5. Una moneda por negocio; importes en unidades menores (`Money` del compañero).
6. Tope de ritmo por defecto: 60 req/min y 5 concurrentes por conector; 600 req/min global.
7. Sin operación de sólo lectura evidente, la skill **pregunta cuál es segura** en vez de elegir (FR-003).
