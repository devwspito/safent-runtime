# 032 — Investigación (Fase 0)

Fecha: 14-sep-2026. Entradas: `spec.md`, `plan.md` (v2), código real de
`lumen-control-enterprise`, `safent-ads` y `safent-runtime`.

---

## Decisión: dónde vive la capa de gobierno del arnés
- **Elegido**: **servidor MCP remoto** en Safent Cloud (`https://mcp.safent.app/mcp`),
  streamable HTTP + OAuth 2.1.
- **Por qué**: es lo único que cumple «el vínculo con el panel es la instalación» sin pedirle
  al usuario que copie nada; el OAuth aterriza en Safent Cloud y *ahí mismo* nace la
  instalación con su identificador, su organización y su puesto. Además elimina podman,
  imágenes y puertos del camino de instalación (SC-1, < 5 min en un Mac limpio).
- **Descartadas**:
  - *Agregador local por stdio* (un binario `safent-mcp` que el arnés lanza y que multiplexa
    los servidores internos): funciona sin nube, pero exige instalar algo, guardar una
    credencial en la máquina (choca con SC-4) y no vincula nada con el panel; el arnés tendría
    que emparejarse por código, que es justo lo que el dueño quiere quitar.
  - *Servidor MCP del propio companion expuesto a Internet*: el companion es de un solo
    propietario y su `/mcp` se autentica con un bearer estático compartido
    (`ApiSettings.mcp_token`); exponerlo es dar la misma llave a todo el mundo, sin política,
    sin auditoría por persona y sin revocación.
- **Riesgos**: un servidor OAuth propio es superficie de seguridad nueva → se revisa con
  `security-engineer` **antes** de exponerlo (ver `contracts/oauth.md` §8) y se somete a la
  batería de conformidad OAuth 2.1 antes del primer login real.
- **Enlaces**: `spec.md` Reglas 1 y 2; `plan.md` §1 Decisiones 1 y 3.

## Decisión: la instalación materializa una `instance`, no un principal nuevo
- **Elegido**: `installation` 1:1 con una fila `instance` de `kind='harness'`, con
  `instance_secret_hash = NULL`.
- **Por qué**: `ads_grant`, `remote_approval`, `delegation_message`, `published_policy`,
  `license.assigned_instance_id` y `ads_instance_user` están **todos** claveados por
  `instance_id`. Un principal paralelo obligaría a bifurcar cinco subsistemas ya endurecidos
  (firma Ed25519 de decisiones, concesiones de cuentas de anuncios, publicación de política,
  relé A2A). Con esta decisión, el gobierno del arnés hereda todo eso el día uno y
  «revocar la licencia corta el puesto» (SC-5, P3) sale gratis.
  El `instance_secret_hash = NULL` es la otra mitad: `_authenticate_instance*` de `api/app.py`
  no puede autenticar nunca una instalación de arnés, así que un token OAuth jamás alcanza
  `/v1/policy`, `/v1/metering`, `/v1/outbox` ni `/v1/mcp-token`.
- **Descartadas**:
  - *`Installation` como tabla aislada con su propio sistema de concesiones*: duplicación de
    `ads_grant` y de la cola de aprobaciones; dos verdades sobre «quién puede tocar esta
    cuenta de Google Ads».
  - *Reutilizar `service_account_token`*: es un bearer de máquina de ≤ 15 min ligado a
    (user, org) sin puesto, sin licencia y sin política; no modela una instalación revocable
    con nombre.
- **Riesgos**: cada instalación consume un asiento de licencia; una persona con Claude Code +
  Codex + la app propia gasta tres. Mitigación: la organización personal nace con
  `seat_limit=3`. Para empresa queda como pregunta abierta 1.
- **Enlaces**: `data-model.md` §3 Installation; `spec.md` P3 Aceptación.

## Decisión: códigos de emparejamiento sólo para lo que no tiene navegador
- **Elegido**: OAuth para el arnés; `safent pair <código>` **sobre la misma instancia** para la
  jaula local y los servidores sin navegador.
- **Por qué**: el código es una credencial que el usuario transcribe — es fricción y es
  superficie. Donde hay navegador, el consentimiento es mejor en todo: más seguro (PKCE, sin
  secreto que copiar), más informativo (se ve qué se autoriza) y revocable con nombre. Donde no
  hay navegador (un servidor, la jaula en modo desatendido), el código sigue siendo la
  respuesta correcta y **ya existe y está endurecido** (Crockford base32, 60 bits, TTL 10 min,
  un uso, bloqueo por intentos).
- **Descartadas**: *sólo códigos* (no vincula con el panel sin pasos manuales, rompe SC-1);
  *sólo OAuth* (deja fuera servidores y la jaula desatendida).
- **Riesgos**: dos caminos de alta ⇒ dos sitios donde equivocarse con el asiento. Mitigado por
  la invariante 1:1 instalación↔instancia: el emparejamiento de la jaula **no** crea instancia
  nueva, rellena la que ya existe.
- **Enlaces**: `spec.md` Regla 2; `plan.md` Decisión 5; `contracts/panel.md` §1.

## Decisión: el companion se aloja por organización, no por instalación
- **Elegido**: un despliegue de `safent-ads` por organización; la pasarela le habla con una
  credencial de servicio y le pasa el alcance por cabecera firmada.
- **Por qué**: el companion es estructuralmente de un solo propietario —
  `SqlOwnerBridgeRepository` mantiene como mucho una fila en `owners` y
  `StaticCallerScopeResolver` devuelve `allowed_business_ids=None`, que significa **todos los
  negocios**. Alojarlo por instalación multiplicaría por N el coste y la superficie sin ganar
  aislamiento (dos instalaciones de la misma empresa deben ver la misma cartera).
- **Descartadas**: *uno por instalación* (coste, y no resuelve el aislamiento entre empresas);
  *uno solo multi-organización* (es ADS-02, no está cerrado; hasta que lo esté sería un fallo
  de aislamiento, no una optimización).
- **Riesgos**: hasta cerrar ADS-02, Safent Cloud sirve a una organización por despliegue
  (Friendog primero). Mitigación inmediata, y la más importante de este documento: **invertir
  el valor por defecto de `CallerScope`** — sin cabecera de alcance válida, el alcance es
  `frozenset()` (vacío), nunca `None` (todos).
- **Enlaces**: `plan.md` Decisión 3 y §4 Riesgos; `contracts/mcp-gateway.md` §4.

## Decisión: se conservan los nombres de herramienta del companion
- **Elegido**: `tools/list` publica `list_campaigns`, `propose_campaign_draft`… sin prefijo.
  Sólo las tres de gobierno llevan `safent_`.
- **Por qué**: (1) `hermes.capabilities.tool_sensitivity._SAFENT_ADS_WRITE_TOOLS` clasifica el
  gasto por esos nombres exactos; renombrar bifurca la única fuente de verdad de qué es SPEND.
  (2) El `ToolRegistry` del companion **rechaza** nombres que no empiecen por verbo de lectura
  o de propuesta: `safent_propose_pause` sería ilegal aguas arriba. (3) Claude Code y Codex ya
  espacian por servidor (`mcp__safent__…`).
- **Descartadas**: *prefijar todo con `safent_`* (rompe lo anterior a cambio de una estética);
  *renombrar a verbos en castellano* (el modelo razona peor y rompe todo el corpus de pruebas).
- **Riesgos**: si algún día conviven dos companions bajo la misma pasarela habrá colisión de
  nombres. Mitigación prevista: prefijo por companion (`ads.`) sólo cuando llegue el segundo.
- **Enlaces**: `contracts/mcp-gateway.md` §2.

## Decisión: opacos en vez de JWT para los tokens del arnés
- **Elegido**: tokens opacos de 32 bytes, guardados como SHA-256, validados con una consulta
  por llamada. Acceso 1 h, refresco 30 días con rotación y detección de reuso.
- **Por qué**: la revocación desde el panel tiene que **cortar de verdad**, y con JWT eso obliga
  a una lista de revocación consultada en cada llamada — es decir, la misma consulta, con la
  complejidad añadida de firmar y rotar claves. Con opacos, revocar es un `UPDATE`. El patrón
  ya existe en el repo (`service_account_token`, `hash_secret`).
- **Descartadas**: *JWT firmado con la clave del tenant* (revocación diferida o lista negra
  igual de cara); *tokens de acceso de 5 minutos* (churn de refresco innecesario cuando la
  validación ya toca la base de datos).
- **Riesgos**: una consulta por llamada MCP. Mitigado por índice único sobre `token_hash`.
- **Enlaces**: `contracts/oauth.md` §5.3.

## Decisión: «pendiente de aprobación» se reanuda con una herramienta de consulta
- **Elegido**: la llamada devuelve `status: pending_approval` (no es error) y el modelo
  consulta `safent_approval_status(request_id)`; al ver `approved`, **la pasarela** ejecuta con
  los argumentos originales guardados cifrados, una sola vez (pestillo `acked` 0→1).
- **Por qué**: mantiene el vínculo entre lo que el humano leyó (`params_redacted`,
  `action_digest`) y lo que se ejecuta. Y el modelo no se queda bloqueado: sigue conversando.
- **Descartadas**: *re-invocar la herramienta original tras aprobar* (TOCTOU: el modelo podría
  mandar otros argumentos); *esperar bloqueando en el servidor* (agota el tiempo del arnés y
  bloquea una conexión por aprobación); *usar el relé `delegation_message`* (es A2A entre
  empleados y se consume por *pull* autenticado con `instance_secret`, que una instalación de
  arnés no tiene).
- **Riesgos**: hay que guardar argumentos cifrados con TTL. Mitigado: AES-GCM con la clave del
  tenant, borrado al ejecutar o al expirar (30 min).
- **Enlaces**: `contracts/mcp-gateway.md` §5.

## Decisión: dos clases de auditoría
- **Elegido**: una fila en `mcp_call_log` por **cada** llamada (sin encadenar, retención 90
  días) y una fila en `audit_log` (encadenada, indefinida) sólo para escrituras, denegaciones y
  pendientes.
- **Por qué**: `audit_log` lleva `seq`/`prev_hash`/`entry_hash` por organización — la cadena
  serializa las escrituras. 120 lecturas por minuto por instalación la destruirían como
  instrumento y como rendimiento. SC-3 pide el rastro **de las acciones con dinero**: esas van
  encadenadas y firmadas.
- **Descartadas**: *todo a `audit_log`* (contención y ruido); *todo a `mcp_call_log`* (pierde
  la propiedad a prueba de manipulación justo donde importa).
- **Riesgos**: dos tablas que consultar en el panel. Mitigado: el detalle de instalación las
  une en una sola vista.
- **Enlaces**: `contracts/mcp-gateway.md` §9.

## Decisión: la pasarela vive en Enterprise, no en el runtime
- **Elegido**: `lumen-control-enterprise/src/safent_control/mcp_gateway/`.
- **Por qué**: la Constitución del runtime (Principio 0) prohíbe alojar en el shell-server
  lógica de gobierno, máquinas de estado o firma criptográfica sobre HTTP. La pasarela es
  exactamente eso: gobierno, estado y firma. En Enterprise —que es un plano de control en la
  nube, no el sistema operativo agéntico— es su sitio natural, junto a la política, la
  auditoría y la cola de aprobaciones que ya administra. **No hay violación constitucional que
  registrar en `plan.md → Complexity Tracking`.**
- **Enlaces**: `.specify/memory/constitution.md` Principio 0; `plan.md` §1 tabla de componentes.

---

## Verificado

**Claude Code** (documentación 14-sep-2026): `claude mcp add --transport http <nombre> <url>`;
`claude mcp login <nombre>` (OAuth con registro dinámico de cliente); `.mcp.json` con
`type: "http"`; `_meta["anthropic/requiresUserInteraction"]` fuerza confirmación siempre
(2.1.199+); listas blancas de MCP gestionadas por la organización
(`managedMcpServers`, `allowedMcpServers`); plugins (manifiesto, `.mcp.json`, `hooks/hooks.json`,
`bin/`, marketplaces, `claude plugin validate`); hooks `PreToolUse` con `permissionDecision`.

**Codex** — verificado contra el binario real de esta máquina, `codex-cli 0.154.0-alpha.6.1`:
- `codex mcp add <NOMBRE> (--url <URL> | -- <COMANDO>…)`; `--url` selecciona streamable HTTP.
- `--bearer-token-env-var <VAR>`, `--oauth-client-id <ID>`,
  `--oauth-client-registration <AUTO|CIMD|DCR>` — **el registro dinámico está soportado**.
- `codex mcp list | get | add | remove | login | logout`.
- Sandbox: `-s, --sandbox <read-only|workspace-write|danger-full-access>`.
- Aprobaciones: `-a, --ask-for-approval <POLÍTICA>` con `on-request` y `never` seguros; la lista
  a tratar como «`on-request, never, untrusted, on-failure` pendiente de confirmación final».
- `--dangerously-bypass-approvals-and-sandbox` existe: **el lanzador de Safent no lo escribe
  jamás**; se añade una prueba que falla si aparece en la configuración generada.
- Sobrescrituras estilo `-c 'sandbox_permissions=["disk-full-read-access"]'`.
- `codex app-server` (experimental) con `daemon {bootstrap,start,restart,enable-remote-control,
  disable-remote-control}`, `proxy` (stdio contra el socket de control) y `generate-ts`:
  **empotrar una sesión de Codex sin ventana lanzada por Safent es factible** por el protocolo
  de app-server (habilita P3.11 sin inventar nada).
- La documentación pública de configuración se movió (`docs/config.md` es hoy un esbozo):
  **no se cita**; la fuente de verdad es `codex <subcomando> --help` de la versión instalada.

**MCP**: transporte streamable HTTP; autorización según la especificación MCP (OAuth 2.1, PKCE,
RFC 8414, RFC 9728, RFC 7591).

**Código propio verificado leyendo la fuente**: `/mcp` ya ocupado por `FastApiMCP` en
`api/app.py:201`; `Mount("/mcp")` no casa el path pelado (lo documenta
`safent_ads/mcp/presentation/http.py`); `StaticCallerScopeResolver` devuelve alcance total;
`ads_grant` y `remote_approval` claveados por `instance_id`; `audit_log` con cadena
`seq/prev_hash/entry_hash`; `inline_local_refs` en `hermes/runtime/mcp_tool_specs.py`;
`_SAFENT_ADS_WRITE_TOOLS` en `hermes/capabilities/tool_sensitivity.py`.

## NO verificable con la documentación — plan de verificación de la primera hora

| # | Lo que no sabemos | Cómo se comprueba (≤ 60 min, sin escribir producto) | Qué cambia si sale mal |
| --- | --- | --- | --- |
| V1 | Si Claude Code acepta un servidor OAuth con registro dinámico **ajeno a Anthropic** y qué metadatos exige exactamente | Levantar un AS de juguete (FastAPI, 120 líneas) que sirva los dos `.well-known`, `/oauth/register`, `/authorize`, `/token`; `claude mcp add --transport http` + `claude mcp login`; capturar todas las peticiones | Si exige algo más (p. ej. `resource` obligatorio, o `registration_client_uri`), se añade a `contracts/oauth.md` antes de escribir el módulo |
| V2 | Si Codex `--oauth-client-registration DCR` habla el mismo RFC 7591 y con qué `redirect_uri` | Mismo AS de juguete; `codex mcp add safent --url … --oauth-client-registration DCR` + `codex mcp login safent`; comparar con V1 | Si difieren en `redirect_uris`, la validación de §3 se relaja **sólo** para el patrón observado, nunca con comodines |
| V3 | Valores exactos aceptados por `-a/--ask-for-approval` y por `approval_policy` en el fichero | `codex -a xxx --help` y probar cada valor contra una sesión de dos segundos; leer el error de validación | Fija la tabla de la tarea de Fase 2; hasta entonces el lanzador sólo escribe `on-request` |
| V4 | Si `tools.<tool>.approval_mode = "approve"` existe con ese nombre exacto en 0.154 y si el arnés lo respeta para un servidor remoto | Escribir el bloque a mano, `codex mcp get safent`, y lanzar una herramienta marcada; observar si pide confirmación | Si no existe, la confirmación en Codex depende sólo de `--ask-for-approval`; hay que decírselo al dueño porque degrada SC-3 en Codex |
| V5 | Si Codex honra el proxy de salida para el transporte MCP | Servidor MCP de juguete detrás de un proxy con lista blanca; observar si la conexión sale por él | Afecta a la tarea 2.5 de Fase 2, no a la Fase 1 |
| V6 | Vida y renovación real del token que guarda cada arnés (¿refresca solo?, ¿a los cuántos 401?) | Emitir un acceso de 60 s en el AS de juguete y ver el comportamiento del cliente ante `401` | Ajusta `expires_in` en `contracts/oauth.md` §5.3 |
| V7 | Si `_meta["anthropic/requiresUserInteraction"]` se respeta en un servidor **remoto** (no sólo stdio) | Publicar una herramienta trivial con la marca desde el servidor de juguete y llamarla | Si no se respeta en remoto, toda escritura pasa obligatoriamente por aprobación de panel (`decision='panel'` forzado) |
| V8 | Si el companion admite una cabecera extra sin romper su middleware de bearer | Llamada de prueba a `/mcp` del companion de Friendog con `X-Safent-Caller-Scope` | Si la rechaza, el alcance viaja en un argumento de la herramienta, no en cabecera (peor, pero viable) |

Regla: **ninguna tarea de implementación que dependa de V1–V4 y V7 empieza antes de que su
verificación esté hecha y anotada aquí.**
