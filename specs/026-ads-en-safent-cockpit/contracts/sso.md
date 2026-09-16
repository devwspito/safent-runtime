# Contrato — Puente de sesión Safent → safent-ads (FR-002)

Fuente de verdad de la forma. Toda implementación y todo cliente se valida contra este documento.
Tres actores: **daemon** (autoridad, firma), **shell-server** (transporte, proxy), **companion** (verifica).

```
navegador ──(1) POST /api/v1/ads/bridge/session ─────────────► shell-server        [Bearer webui]
          ◄── Set-Cookie: ads_bridge (HttpOnly, SameSite=Strict, Path=/ads)
          ──(2) GET  /ads/  (iframe, mismo origen) ──────────► shell-server        [Cookie ads_bridge]
                                     shell-server ──(3) D-Bus mint_companion_owner_assertion ──► daemon
                                     shell-server ──(4) POST /api/v1/auth/exchange ────────────► companion
                                     shell-server ◄── Set-Cookie: ads_session  (se queda en la jarra)
          ──(5) XHR /ads/api/v1/... ─────────────────────────► shell-server ──► companion
```

## 1. `POST /api/v1/ads/bridge/session` — Safent (shell-server)

Autenticación: `Authorization: Bearer <webui>` (middleware existente). Sin cuerpo.

`200` → `{"status": "ready"|"unavailable", "reason": <AdsAvailability|null>}` +
`Set-Cookie: ads_bridge=<hex64>; HttpOnly; SameSite=Strict; Path=/ads; Max-Age=2592000`.
Valor = subclave HKDF `ads-bridge-cookie` de `master.key` (estable entre reinicios, comparación en
tiempo constante). `401` sin bearer válido. `DELETE` sobre la misma ruta borra la cookie, cierra la
sesión aguas arriba (`POST /api/v1/auth/logout`) y vacía la entrada de la jarra.

## 2. `/ads/{path}` — proxy inverso (shell-server)

Puerta: `ads_bridge` válida, si no `401` (default-deny; `GET` incluido — el resto del shell-server solo
exige credencial en mutaciones, aquí **no**). Destino: `https://ads.safent.internal:8443/{path}`,
TLS verificado contra `ca_path` de `companions.json`, SNI/`Host` fijados por el cargador, sin seguir
redirecciones a otro host.

| Regla | Valor |
|---|---|
| Prefijos permitidos | `/`, `/assets/*`, `/api/v1/*` (menos los denegados), `/favicon.ico` |
| Prefijos **denegados** | `/mcp*` (403 `MCP_NOT_BRIDGED`), `/api/v1/auth/login`, `/api/v1/auth/totp` (403 `LOGIN_NOT_BRIDGED`) |
| Cabeceras cliente→upstream | allow-list: `accept`, `accept-language`, `content-type`, `content-length`, `if-none-match`, `x-csrf-token`, `x-reauth-token` |
| Cabeceras añadidas | `Cookie: ads_session=<jarra>; ads_csrf=<del navegador>`, `X-Forwarded-Prefix: /ads` |
| Cabeceras eliminadas | `authorization`, `cookie` original completo, `x-forwarded-for`, `forwarded` |
| Cookies upstream→navegador | solo `ads_csrf`, reescrita `Path=/ads`, `Secure` retirado si el borde es `http://127.0.0.1` (origen confiable W3C). `ads_session` **se retiene en la jarra, nunca se reenvía** |
| Métodos | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`; cuerpo en streaming, tope 8 MiB |
| Timeouts | 10 s conexión, 30 s respuesta; 504 `COMPANION_TIMEOUT` |

**Jarra de sesión** (`AdsSessionJar`, en proceso, un único propietario): `{cookie, expires_at}`. Vacía o
`401` aguas arriba → un canje (§3–§4) y **un** reintento; si el canje falla, se propaga su error. Se vacía
al reiniciar el shell-server, al `DELETE` de §1 y al cambiar la huella de la CA.

## 3. D-Bus `org.hermes.Runtime1.mint_companion_owner_assertion(slug) → JSON`

Autoría por `sender_uid` (solo el uid del shell-server). Lee la clave privada de
`/etc/hermes/companions/ads-sso.key` (montaje read-only, mismas comprobaciones de propietario/permisos que
`companions.py`). Devuelve `{"assertion": "<b64url(payload)>.<b64url(sig)>", "expires_at": <ISO8601>}`.
Nunca devuelve la clave. Límite: 30 aserciones/minuto; superado, `RATE_LIMITED`.

**Payload** (JSON compacto, UTF-8, claves ordenadas — la firma cubre los bytes exactos que se transmiten):

| Campo | Tipo | Regla |
|---|---|---|
| `v` | int | `1` |
| `iss` | str | `"safent-runtime"` |
| `aud` | str | `"safent-ads"` |
| `slug` | str | `"safent-ads"` (debe existir en `companions.json`) |
| `sub` | str | identidad del propietario en Safent: `sha256(master.key ‖ "ads-sso-subject")`, estable por instalación, no invertible |
| `jti` | str | UUIDv4, un solo uso |
| `iat` / `exp` | int | epoch; `exp − iat = 60 s` |
| `purpose` | str | `"cockpit_session"` |
| `surface` | str | `"safent_cockpit"` (llega al log de decisiones) |

Firma: Ed25519 sobre los bytes del payload (sin envoltorio JWT: un algoritmo, sin `alg` negociable).

## 4. `POST /api/v1/auth/exchange` — companion

Solo existe con `ADS_COMPANION_MODE=true` (fuera, `404`). Exento de CSRF (no lleva cookie de sesión),
sometido al presupuesto de tasa de `/api/v1/auth`. Cuerpo `{"assertion": "<...>"}`.

Verificación, en orden, **fail-closed** en cada paso: formato → firma Ed25519 con `ADS_SSO_PUBLIC_KEY` →
`v`/`iss`/`aud`/`slug`/`purpose` literales → `iat ≤ now + 5 s` y `now < exp` → `jti` insertado en
`sso_assertions_seen` (`UNIQUE(jti)`, purga > 24 h) → resolución del propietario.

**Resolución del propietario** (sustituye a `seed_owner` en modo companion):
sin propietario → se crea y se ata a `sub` · propietario sin atar → se ata (TOFU) y se registra
`owner_session_bridged` en el log de decisiones · atado al mismo `sub` → sigue · atado a otro → `403`.

`200` → `Set-Cookie: ads_session=<token>; HttpOnly; Secure; SameSite=Strict; Path=/api; Max-Age=<TTL absoluto>`
(idéntico a `_set_session_cookie`, sin ruta nueva) y cuerpo
`{"owner_id", "business_id", "expires_at", "totp_enrolled": bool, "surface": "safent_cockpit"}`.

| Código | `error.code` | Cuándo |
|---|---|---|
| 400 | `ASSERTION_MALFORMED` | formato/base64/JSON inválidos |
| 401 | `ASSERTION_INVALID` | firma o campos literales que no cuadran |
| 401 | `ASSERTION_EXPIRED` | fuera de la ventana de 60 s |
| 409 | `ASSERTION_REPLAYED` | `jti` ya visto |
| 403 | `OWNER_BOUND_ELSEWHERE` | el propietario está atado a otro `sub` |
| 404 | — | fuera de modo companion |
| 429 | `RATE_LIMITED` | presupuesto de `/api/v1/auth` agotado |

## 5. Modo empotrado (companion)

Con `X-Forwarded-Prefix` validado contra `{"", "/ads"}` (cualquier otro valor → `""`, default-deny):
`root_path` del ASGI = el prefijo · `index.html` se sirve con `<base href="{prefijo}/">` y
`window.__ADS_BASE_PATH__` / `__ADS_EMBEDDED__` inyectados (el panel se construye una sola vez y sirve
igual en directo que empotrado) · CSP `frame-ancestors 'self'` y `X-Frame-Options: SAMEORIGIN`
**solo en modo companion**; fuera siguen en `'none'`/`DENY` · el panel oculta "cerrar sesión" e "iniciar
sesión" cuando `__ADS_EMBEDDED__`.

## 6. Revocación

| Suceso | Efecto |
|---|---|
| `DELETE /api/v1/ads/bridge/session` | cookie de puente borrada + logout aguas arriba + jarra vacía |
| Reinicio del shell-server | jarra vacía; el siguiente `/ads/*` canjea de nuevo (sin ver login) |
| `safent companion rotate` | nuevo par Ed25519 → toda aserción anterior falla `ASSERTION_INVALID` |
| Sesión de ads caducada (TTL absoluto/idle) | `401` aguas arriba → un canje + un reintento, transparente |
| TOTP fallido/bloqueo 5-15 min | intacto: `require_reauth` no se toca |

## 7. Amenazas

| # | Amenaza | Control | Test |
|---|---|---|---|
| S-1 | Aserción falsificada | Ed25519, un algoritmo, verificador solo con la pública | `test_exchange_rejects_forged_signature` |
| S-2 | Repetición de una aserción capturada | `jti` único + `exp` 60 s + TLS + único par de red | `test_exchange_rejects_replayed_jti` |
| E-1 | El bearer MCP escala a sesión de propietario | credencial distinta; `/mcp` no proxeado; el canje no acepta bearer | `test_bridge_denies_mcp_prefix` |
| E-2 | Clickjacking | mismo origen + `frame-ancestors 'self'` + borde en loopback | `test_embedded_headers_are_self_not_none` |
| E-3 | CSRF desde una página hostil | doble envío conservado extremo a extremo + `SameSite=Strict` en ambas cookies | `test_bridge_forwards_csrf_pair_only` |
| I-1 | Cookies/bearer de Safent filtrados aguas arriba | allow-list de cabeceras; `authorization` eliminado | `test_bridge_strips_safent_credentials` |
| I-2 | `ads_session` expuesta al navegador | jarra en proceso; `Set-Cookie` retenido | `test_ads_session_never_reaches_browser` |
| T-1 | Inyección de prefijo por `X-Forwarded-Prefix` | allow-list `{"", "/ads"}`; peor caso, ruta del mismo origen | `test_prefix_allowlist_defaults_to_root` |
| T-2 | Formulario de acceso embebido (phishing dentro de Safent) | `/auth/login` y `/auth/totp` denegados en el puente | `test_bridge_denies_login_routes` |
| R-1 | Repudio de una acción hecha desde el tablero | `surface="safent_cockpit"` en el log de decisiones (FR-015) | `test_bridged_action_records_surface` |
| D-1 | Inundación de canjes | presupuesto `/api/v1/auth` + 30/min en el daemon | `test_mint_assertion_rate_limited` |
| E-4 | El agente (uid 886) alcanza el puente | aislamiento de netns respecto a `:17517` + `ads_bridge` obligatoria | `test_bridge_requires_cookie` |
