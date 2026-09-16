# 026 — Plan: Ads como cuadro de mando propio dentro de Safent

**Entrada**: `spec.md` (469d574) · **Depende de**: `specs/024-ads-companion-preinstalled/` (red fija
`10.201.0.10:8443`, CA + bearer aprovisionados por `provision.sh`, `companions.json`, `_SEEDED_MCP_SLUGS`).
**Repos**: `lumen-runtime-next` (Safent) y `safent-ads` (el companion). Prosa española, identificadores ingleses.

## 1. Contexto técnico

| Eje | Estado hoy | Consecuencia para 026 |
|---|---|---|
| Sesión de Safent | *bearer* estable en el navegador (`shell-webui-session`), sin cookies; `GET` sin gate, mutaciones con `Bearer` | La autoridad raíz es ese bearer; el puente debe colgar de él |
| Sesión del companion | cookie `ads_session` `HttpOnly; Secure; SameSite=Strict; Path=/api` + TOTP `X-Reauth-Token` + CSRF doble envío `ads_csrf` | Ninguna de esas cookies viaja entre orígenes distintos |
| Panel del companion | CSP `frame-ancestors 'none'` + `X-Frame-Options: DENY` | **El iframe de `AdsView` está silenciosamente bloqueado hoy** |
| TLS del companion | CA privada, hoja `ads.safent.internal` | El navegador **no** confía en `https://127.0.0.1:8443`: un iframe cross-origin nunca cargaría |
| Lecturas de ads | `PortfolioView`, `Badges`, `SignalView`, `Freshness`, `Caps/Pacing`, `spend_14d`, `economics`, `audit` | El cockpit **compone**, no calcula |

## 2. Constitution Check — pre-diseño

| Principio | Veredicto |
|---|---|
| 0 · SO, no backend | ⚠️ El puente necesita HTTP en el shell-server. Mitigado en §4: el shell-server es **transporte con allow-list**; la autoridad (firma Ed25519, salud del companion) vive en el daemon por D-Bus |
| 0.2 · nada de firma criptográfica en el shell-server | ✅ La firma la emite `org.hermes.Runtime1.mint_companion_owner_assertion`, con autoría por `sender_uid` |
| II · HITL inquebrantable | ✅ El tablero no abre escritura nueva (FR-013); reusa propuestas/ejecución con su fricción |
| IV · fail-closed | ✅ Prefijo, cabeceras, cookies, rutas y estados: allow-list; cualquier anomalía → denegar |
| INV-4 de 024 (el bearer no sale del daemon) | ✅ El puente **no lee el bearer**: usa una credencial distinta |
| DDD / no duplicación | ✅ Cockpit = read model compuesto de los existentes; cero SQL nuevo donde ya lo hay |

## 3. Decisión 1 — SSO (FR-002): proxy inverso mismo-origen + aserción firmada por el daemon

**Elegida (a) con el mecanismo criptográfico de (b)**: el shell-server sirve `/ads/*` como proxy inverso
al companion, y la sesión del propietario se obtiene con una aserción Ed25519 que **firma el daemon** y
canjea `POST /api/v1/auth/exchange` en el companion. Contrato completo en `contracts/sso.md`.

**Por qué no (b) puro (iframe cross-origin)**: tres muros independientes, cada uno letal por sí solo —
(1) el navegador rechaza la CA privada de `https://127.0.0.1:8443`, (2) `frame-ancestors 'none'` +
`X-Frame-Options: DENY` bloquean el marco, (3) `ads_session`/`ads_csrf` son `SameSite=Strict`: en un
marco de tercero no se envían ni con cookie particionada (CHIPS exige `Partitioned` + `Secure` y rompe
el doble envío CSRF). Además obligaría a que el navegador custodiara la cookie de una autoridad que no
es la suya. **Mismo-origen elimina los tres muros de raíz**, no los rodea.

**Por qué no el bearer del companion como credencial de canje**: convertiría un secreto que hoy autoriza
un catálogo acotado de herramientas MCP en un "conviértete en el propietario". Separación de deberes: par
Ed25519 propio, aprovisionado junto al bearer con el patrón ya probado de `gen_keys`; el verificador solo
guarda la pública. `/mcp` **no** se proxea: el puente humano nunca es una segunda puerta MCP.

**Reglas duras del puente** (todas verificables): cookie `ads_bridge` (HttpOnly, SameSite=Strict, Path=/ads)
emitida por `POST /api/v1/ads/bridge/session` bajo el bearer existente → el proxy default-deny sin ella ·
la cookie `ads_session` **nunca llega al navegador** (jarra en proceso) · del navegador solo se reenvía
`ads_csrf` (el resto de cookies y `Authorization` se eliminan) · rutas permitidas por prefijo, con
`/api/v1/auth/login` y `/api/v1/auth/totp` **denegadas** (FR-002: jamás un formulario embebido) ·
`X-Reauth-Token` se reenvía intacto: el TOTP dentro del iframe funciona sin tocar `require_reauth`.

**Aprovisionado del propietario**: el canje crea el propietario si no existe y lo ata al `sub` de Safent
(TOFU, registrado en el log de decisiones); atado a otro `sub` → `403 OWNER_BOUND_ELSEWHERE`. Sustituye a
`seed_owner` en instalación companion. **Paridad Telegram**: el canal sigue siendo el gemelo (FR-012); una
propuesta resuelta en cualquiera de las dos superficies cierra la otra porque ambas escriben en `proposals`.

## 4. Decisión 2 — Cockpit read model

`GET /api/v1/cockpit?business_id&window` devuelve **una** instantánea: cabecera de cartera + filas de
cotización + franja de cambios. Contrato en `contracts/cockpit-read-model.md`. Tres decisiones de fondo:

1. **`Measure<T>` como envoltorio obligatorio de toda celda numérica** (`status` ∈ available ·
   no_data · insufficient_volume · immature_window · learning · not_controllable · no_customer_source ·
   stale). FR-009/SC-006 dejan de ser disciplina y pasan a ser tipo: no existe la forma "número sin estado".
2. **Sin SQL nuevo donde ya lo hay**: `SqlCockpitReadModel` compone `get_portfolio`, `get_badges`,
   `list_signals`, `caps_and_pacing`, `economics` y el log de decisiones. Solo hay consulta nueva donde no
   existe fuente (franja de cambios).
3. **Se calcula sin filtrar y se filtra en el cliente**: una instantánea por `(business_id, window)`,
   memoizada dentro de la ventana de frescura, con `ETag`. Ordenar/filtrar (FR-006) no revalida nada, el
   estado vive en la URL y SC-003 (tablero = cartera) es estructural: misma consulta, mismo instante.

## 5. Diseño por capas (entrada a `plan.md` consolidado)

**`safent-ads`** · *Domain*: intacto — `Signal`, `Proposal`, `EmergencyBrake`, `UnitEconomicsProfile`,
`LagCurve` ya poseen sus invariantes; 026 **no añade dominio**. *Application*: `CockpitReadPort` (Protocol
propio, ISP — no engorda `PanelReadPort`), DTOs `CockpitView`/`TickerRow`/`Measure`/`ChangeEntry`;
`ExchangeOwnerAssertion` (caso de uso en `iam/application`). *Infrastructure*: `SqlCockpitReadModel`,
`Ed25519AssertionVerifier`, `SqlAssertionReplayGuard`. *Presentation*: `panel/presentation/rest.py`
(`/cockpit`), `iam/presentation/router.py` (`/auth/exchange`), `composition/api.py` (modo empotrado:
`frame-ancestors 'self'`, `X-Frame-Options: SAMEORIGIN`, prefijo `X-Forwarded-Prefix` validado contra
`{"", "/ads"}`), `panel/src/routes/CockpitPage.tsx` como hogar de Ads.

**Safent runtime** · *Daemon (autoridad)*: `mint_companion_owner_assertion(slug)` y
`get_companion_health(slug)` en `dbus_runtime_service`; única lectura de la clave privada y del bearer.
*Shell-server (transporte)*: `shell_server/ads_bridge.py` — router `/ads/{path}`, jarra de sesión,
allow-lists, `POST /api/v1/ads/bridge/session`. *Presentation*: `AdsView` (iframe mismo-origen + los
estados honestos), `Layout` (entrada incondicional), `useAdsAvailability`, i18n.

**Transversales**: authz en el borde del puente (bearer → cookie de puente → aserción) · CSRF de doble
envío conservado extremo a extremo · TOTP donde ya estaba · auditoría: `owner_session_bridged` y toda
acción con `surface=cockpit` (FR-015) · sin secretos ni PII de leads en el read model (NFR-003).

## 6. Constitution Check — post-diseño

✅ Principio 0: la firma y la salud viven en el daemon; el shell-server no tiene máquina de estados ni
lógica de negocio. ✅ II/IV: fricción y fail-closed intactos. ⚠️ Una violación declarada abajo.

### Complexity Tracking

1. **Proxy HTTP en el shell-server** (Principio 0.2). *Alternativa más simple rechazada*: que el navegador
   hable directo con `10.201.0.10:8443` — imposible: no es enrutable desde el host, la CA no es de confianza
   y el netns del agente lo prohíbe. *Segunda alternativa rechazada*: reescribir el cockpit como vista
   nativa de Safent contra D-Bus — duplicaría la UI del panel y el motor de señales (viola "no duplicación"
   y FR-016). Se acota: allow-list de rutas, cero firma, cero estado de negocio, jarra con TTL.

## 7. Assumptions — los dos `[NEEDS CLARIFICATION]` resueltos

1. **FR-017 (fuente de cliente y valor, rezago aceptable)**. Fuente = el CRM ya conectado del propietario:
   conversiones `business_conversion` de `crm.lead_attributions` (identidad hasheada, webhook
   `/api/v1/conversions/webhook`) para *cliente*, y `UnitEconomicsProfile.contribution_margin` de
   `economics` para *valor de cliente*. **Rezago aceptable = el que declara `LagCurve`**, no una constante:
   `maturity ≥ 0,60` para justificar `SUBIR` y `≥ 0,30` para `BAJAR`/`SALIR`; por debajo, `immature_window`.
   Un perfil `provisional` nunca sostiene una subida. **Hasta la spec 027** (modelo de cliente) el read
   model devuelve `customers`/`customer_value` con `status: no_customer_source` y el ROI se declara
   `basis: "revenue"` — nunca un cero ni una estimación.
2. **Seguridad (¿segundo factor fresco o basta la sesión?)**. **La fricción no baja por entrar sin
   fricción**: leer = sesión de Safent; actuar = exactamente la fricción del canal de mensajería (FR-011).
   Regla asimétrica: **frenar** cuesta sesión + una confirmación (FR-014: nunca se bloquea la acción
   segura); **soltar el freno**, aprobar una subida de gasto, subir un tope o activar autonomía exigen
   **TOTP fresco** (`X-Reauth-Token`, un solo uso, atado a `action_hash`), igual que hoy. El puente reenvía
   la cabecera; no se crea un segundo verificador.

## 8. Preguntas abiertas para el propietario

1. Al rotar (`safent companion rotate`), ¿se revocan también las sesiones de ads vivas, o solo se invalidan
   las aserciones futuras? (defecto propuesto: solo futuras).
2. ¿El propietario creado por TOFU debe poder iniciar sesión con contraseña en el panel directo, o queda
   como cuenta solo-puente + TOTP? (defecto propuesto: solo-puente + TOTP enrolable).
