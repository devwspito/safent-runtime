# 026 — Tareas

Dos repos: **[RT]** `~/Desktop/lumen-runtime-next` · **[ADS]** `~/Desktop/safent-ads`.
`[P]` = paralelizable con la tarea anterior. Cada tarea trae sus pruebas; sin ellas no está hecha.
Contratos vinculantes: `contracts/sso.md` y `contracts/cockpit-read-model.md`.

## Fase 0 — Cimientos del puente (bloquean US1)

- [ ] **T001 [RT] · devops-engineer** — Par Ed25519 de SSO junto al bearer.
  `ops/container/companions/ads/provision.sh` (genera con el patrón ya existente de
  `python -m safent_ads.tools.gen_keys`, privada `0400` en `$STATE/sso/ads-sso.key`, pública a
  `ADS_SSO_PUBLIC_KEY` de `secrets/api.env`) · `ops/container/run-safent.sh` (4º bind read-only
  `/etc/hermes/companions/ads-sso.key`) · `safent` (`companion rotate` rota también el par).
  *Tests*: `tests/unit/ops/test_companion_provision.py` — idempotencia, permisos, rotación reemite ambas.

- [ ] **T002 [ADS] · backend-engineer + database-engineer [P]** — `POST /api/v1/auth/exchange`.
  `src/safent_ads/iam/application/exchange_owner_assertion.py` (caso de uso: verificar → antirrepetición →
  resolver/atar propietario TOFU) · `src/safent_ads/iam/infrastructure/ed25519_assertion_verifier.py` ·
  `sql_assertion_replay_repository.py` · migración `sso_assertions_seen (jti UNIQUE, seen_at)` con purga >24 h ·
  ruta en `iam/presentation/router.py` (solo `ADS_COMPANION_MODE`) · `ADS_SSO_PUBLIC_KEY` en
  `composition/settings.py`. *Tests*: firma falsa, `jti` repetido, expirada, `aud`/`slug` ajenos,
  `OWNER_BOUND_ELSEWHERE`, 404 fuera de modo companion, cookie idéntica a `_set_session_cookie`.

- [ ] **T003 [ADS] · backend-engineer** — Modo empotrado. `composition/api.py`: `frame-ancestors 'self'` +
  `X-Frame-Options: SAMEORIGIN` **solo** en modo companion; `X-Forwarded-Prefix` validado contra
  `{"", "/ads"}` → `root_path` · `composition/app.py`: `index.html` con `<base href>` +
  `window.__ADS_BASE_PATH__`/`__ADS_EMBEDDED__` · `panel/vite.config.ts` `base: "./"` ·
  `panel/src/api/client.ts` y el router leen el base en tiempo de ejecución.
  *Tests*: cabeceras por modo; prefijo hostil → `""`; el mismo build sirve en `/` y en `/ads/`.

- [ ] **T004 [RT] · backend-engineer** — Autoridad en el daemon.
  `src/hermes/agents_os/infrastructure/dbus_runtime_service.py`: `mint_companion_owner_assertion(slug)`
  (autoría por `sender_uid`, 30/min, payload de `sso.md §3`) y `get_companion_health(slug)` (`/mcp/health`
  con la CA y el bearer, ya es el único lector). Carga de la clave con las comprobaciones de
  `shell_server/companions.py`. *Tests*: `tests/unit/agents_os/test_companion_sso_assertion.py` — payload
  exacto, TTL 60 s, `jti` distinto, uid no autorizado, fichero con permisos flojos → denegar, la clave
  privada no aparece en ninguna respuesta ni log.

- [ ] **T005 [RT] · backend-engineer** — Proxy y jarra. **Nuevo** `src/hermes/shell_server/ads_bridge.py`
  (`/ads/{path:path}`, `POST|DELETE /api/v1/ads/bridge/session`, `AdsSessionJar`, allow-lists de ruta,
  método, cabecera y cookie, canje perezoso con **un** reintento) · registro en
  `src/hermes/shell_server/main.py` antes del montaje estático.
  *Tests*: `tests/unit/shell_server/test_ads_bridge.py` — toda la tabla de amenazas de `sso.md §7`
  (S-2, E-1, E-2, E-3, I-1, I-2, T-1, T-2, E-4) más 504 con el companion caído.

## Fase 1 — US1: abrir Ads y leer el mercado (P1)

- [ ] **T006 [ADS] · software-architect + backend-engineer [P]** — Formas de lectura.
  `Measure`/`MeasureStatus` en `src/safent_ads/shared/read_models/dto.py` · `cockpit_dto.py` y
  `CockpitReadPort` en `src/safent_ads/panel/application/` · serializadores en
  `shared/read_models/serialization.py`. *Tests*: propiedad — ningún `Measure` no-`available` serializa
  un número; redondeo de dinero como cadena decimal.

- [ ] **T007 [ADS] · backend-engineer** — `GET /api/v1/cockpit`.
  `panel/infrastructure/sql_cockpit_read_model.py` (compone `get_portfolio`, `get_badges`, `list_signals`,
  `caps_and_pacing`, `economics`; **cero SQL nuevo** salvo lo inexistente) · ruta en
  `panel/presentation/rest.py` con `ETag`, `304` y memoización por ventana de frescura · cableado en
  `composition/`. *Tests*: reglas de honestidad de `cockpit-read-model.md §4` una a una (aprendizaje,
  no controlable, obsoleto, madurez, perfil provisional, cuenta caída), orden por `money_at_stake`,
  paridad campo a campo con `GET /portfolio` (SC-003), y `no_customer_source` en clientes/valor.

- [ ] **T008 [ADS] · frontend-engineer** — `panel/src/routes/CockpitPage.tsx` como hogar de Ads:
  `routeConfig.ts` (Cuadro de mando primero, atajo `1`, Cartera pasa a detalle) · renderizador único de
  `Measure` · `hooks/useCockpitFilters.ts` (filtros y orden en la URL, compartible y restaurable) ·
  cabecera de cartera, ticker pausable, sello de frescura, 390 px sin desplazamiento horizontal, foco
  visible, nada solo por color. *Tests*: panel (Vitest + Testing Library) de los ocho estados de `Measure`,
  teclado completo, `prefers-reduced-motion`, instantánea a 390 px.

- [ ] **T009 [RT] · frontend-engineer** — Ads en la barra lateral, siempre.
  `frontend/src/components/Layout.tsx` (entrada incondicional, sin `useAdsPanelOrigin`) ·
  `frontend/src/hooks/useAdsAvailability.ts` (**nuevo**, lee `companion_status` + salud) ·
  `frontend/src/views/AdsView.tsx` (iframe **mismo origen** a `/ads/`, llamada previa a
  `POST /api/v1/ads/bridge/session`, y los estados honestos: sin instalar · arrancando · versión
  incompatible · canal sin emparejar · sin cuentas, cada uno con su acción) · claves `ads.*` en
  `frontend/src/lib/i18n.ts` (es/en). *Tests*: un caso por estado; ninguna pantalla vacía ni error genérico.

## Fase 2 — US2: actuar sobre la señal (P2)

- [ ] **T010 [ADS] · backend-engineer** — `RowAction` real: derivación de `kind`/`mode`/`friction`/
  `blocked_reason`/`target` desde `proposals` (clasificación + autorización), `execution` (`undo_policy`,
  ventana de gracia) y `rules` (freno, guardarraíles); `surface="safent_cockpit"` en el log de decisiones
  de toda acción del tablero (FR-015). *Tests*: matriz señal × guardarraíl × freno × frescura;
  irreversible ⇒ `typed_confirmation`+`fresh_totp`; frenar ⇒ `none`; soltar el freno ⇒ `fresh_totp`;
  propuesta ya resuelta en Telegram ⇒ `review_proposal` resuelto, sin doble efecto (FR-012).

- [ ] **T011 [ADS] · frontend-engineer** — Acciones en la fila: aprobación en línea con evidencia
  expandible, barra de deshacer con cuenta atrás, confirmación tecleada, `X-Reauth-Token` en línea
  (TOTP dentro del iframe), franja de freno persistente y freno en dos pulsaciones.
  *Tests*: panel — ≤3 pulsaciones de abrir a resolver una `SELL` (SC-001); freno activo ⇒ todo a propuesta;
  401 `REAUTH_REQUIRED` no expulsa de la vista.

## Fase 3 — US3: qué ha cambiado y hacia dónde bajar (P3)

- [ ] **T012 [ADS] · backend-engineer** — `get_changes_since`: `ChangeStrip` desde el log de decisiones,
  señales emitidas y entradas/salidas de cartera desde el último cierre.
  *Tests*: dos cierres sintéticos → los cinco `kind`; fuente incompleta ⇒ `is_partial`, nunca lista vacía.

- [ ] **T013 [ADS] · frontend-engineer [P]** — Franja de cambios en el cockpit y descenso a las vistas
  existentes (campaña, señales, propuestas, creatividades, reglas, registro, conexiones) **sin perder el
  filtro** y sin duplicar ninguna. *Tests*: ida y vuelta conserva el estado de la URL.

## Fase 4 — Verificación en vivo

- [ ] **T014 · qa-engineer** — Instalación limpia de Safent + companion (sin estado previo). Guion:
  abrir Ads desde la barra lateral → aterriza en el cuadro de mando · **cero segundos inicios de sesión**
  en 20 aperturas (SC-002) · las cifras cuadran con Cartera sobre la misma ventana (SC-003) · resolver una
  `SELL` en ≤3 pulsaciones y ≤30 s (SC-001) con su registro antes/después (SC-005) · 50 celdas sin dato
  muestran estado honesto (SC-006) · el companion apagado deja el tablero con su último dato sellado, no
  vacío · a 390 px se alcanzan aprobar, rechazar, deshacer y freno (SC-008).
  Se documenta en `specs/026-ads-en-safent-cockpit/quickstart.md`.
