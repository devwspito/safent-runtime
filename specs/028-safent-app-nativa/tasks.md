# Tasks — 028 (app nativa) + 029 (Ads de un botón)

**24 tareas.** `[P]` = paralelizable con las de su mismo bloque (ficheros
disjuntos). `[US1]`/`[US2]`/`[US3]` = historia de usuario que entrega.
Cada tarea nombra su especialista y sus rutas exactas.

Repositorios / worktrees:

| Clave | Ruta |
|---|---|
| **RT-DESK** | `/home/luiscorrea-dev/Desktop/lumen-runtime-next/desktop` |
| **RT** | `/home/luiscorrea-dev/Desktop/lumen-runtime-next` (CLI, `src/hermes`, `frontend`) |
| **ADS** | `/home/luiscorrea-dev/Desktop/safent-ads` |
| **AA** | `/home/luiscorrea-dev/Desktop/agents-autonomy` |

Reparto: **RT-DESK 8 · RT 10 · ADS 2 · AA 1 · en vivo 1 · (T023 pipeline en AA)**.

Por repositorio: **RT-DESK 8** (T007–T014) · **RT 10** (T001–T006, T015–T018 y T019–T020 del frontend) · **ADS 2** (T021, T022) · **AA 1** (T023) · **en vivo 1** (T024).

---

## Bloque 0 — Bloqueantes heredados (matriz 025). Sin esto, 029 no puede decir `ready`

### T001 [US1] — Escenificar la clave SSO del compañero como ya se hace con el bearer · `security-engineer` + `backend-engineer`
**Repo RT.** Corrige **ADS-02**: `/etc/hermes/companions/ads-sso.key` llega
`-r-------- root root` y el daemon corre como `hermes` (uid 880) → `/ads/` responde
503 y `ready` es hoy inalcanzable.
- `ops/agents-os-edition/scripts/hermes-companion-sso-key` (nuevo, espejo exacto de
  `hermes-companion-bearer`: `/run/hermes/companions/<slug>.sso.key`, 0440 root:hermes)
- `ops/agents-os-edition/systemd/hermes-runtime.service` (`ExecStartPre=-+`)
- `src/hermes/shell_server/companions.py` (leer de la ruta escenificada, misma
  comprobación `is_companion_secret_file_trustworthy`)
- **Test**: `tests/unit/agents_os/test_companion_sso_staging.py` — permisos, propietario, ruta derivada del slug validado.

### T002 [P] [US1] — Arreglar los dos verbos de compañero que mienten o rompen · `backend-engineer`
**Repo RT.** Corrige **CLI-08** (`_companion_container_counts` sin `_companion_env`
→ siempre «0/0») y **CLI-10** (`rotate` mezcla `:latest` con `safent-ads:local` y
rompe alembic).
- `safent` (`_companion_container_counts`, `_companion_env`, `cmd_companion_rotate`,
  `cmd_companion_update`) — la imagen se toma **por digest** del manifiesto, nunca se re-deriva
- **Test**: `tests/unit/cli/test_safent_companion_verbs.py` con podman doblado.

### T003 [P] [US3] — Acotar `uninstall` a esta instalación y purgar de verdad · `backend-engineer`
**Repo RT.** Corrige **UPD-06** (borra agentes y CLI ajenos) y el matiz de **CLI-12**
(`--purge` deja los tres volúmenes del compañero, incluida la base de campañas, y la red).
- `safent` (`cmd_uninstall` con `--scope this-install`, `cmd_companion_remove`)
- **Test**: `tests/unit/cli/test_safent_uninstall_scope.py`.

---

## Bloque 1 — Contratos primero (todo lo demás depende)

### T004 [US1] — Modo `--porcelain` NDJSON + `facts --json` + `SAFENT_PODMAN` en el CLI · `backend-engineer`
**Repo RT.** Implementa `contracts/app-engine.md` §1-§4. El texto humano de hoy no cambia.
- `safent` (nuevos: `_emit()`, `facts`, `stage-runtime`, `ensure-machine`,
  `ensure-images`, `up`, `--secret-fd`; `RT` pasa a resolverse por `SAFENT_PODMAN` primero)
- `ops/container/run-safent.sh` (mismo `SAFENT_PODMAN`)
- **Test**: `tests/unit/cli/test_safent_porcelain.py` — un evento por línea, cierre
  `done|failed` por etapa, **cero** apariciones del vale en stdout/stderr.

### T005 [P] [US2] — `runtime-manifest.json` firmado y ampliación de `/system/update` · `backend-engineer`
**Repo RT.** Implementa `contracts/update.md` §2-§3 en el lado del daemon.
- `src/hermes/shell_server/system_update.py` (añade `engine_digest`,
  `companion_digest`, `pieces`; **conserva** los tres campos de hoy)
- **Test**: `tests/unit/agents_os/test_system_update_manifest.py` — manifiesto sin
  firma válida ⇒ `update_available: false` (fail-closed).

### T006 [P] [US1] — Endpoints de la marca con vocabulario cerrado · `backend-engineer` + `security-engineer`
**Repo RT.** Implementa `contracts/install-request.md` §3. Sin lógica: valida el
enum y escribe/lee el fichero (condición del Principio 0).
- `src/hermes/shell_server/system_update.py` → extraer a
  `src/hermes/shell_server/install_requests.py`; `POST/GET /api/v1/system/requests`;
  alias de compatibilidad para `/system/update` y `/system/uninstall`; TTL por verbo
- **Test**: `tests/unit/agents_os/test_install_requests.py` — verbo/slug desconocido ⇒ 400
  sin escribir; segunda petición del mismo verbo ⇒ 409 con la viva; caducidad por verbo.

---

## Bloque 2 — El envoltorio nativo (RT-DESK)

### T007 [US1] — Dominio: `HostFacts`, `RepairAction`, `EngineLifecycle` · `backend-engineer`
- `desktop/src-tauri/src/domain/{mod,facts,lifecycle,actions,failure}.rs`
- Sin `Command`, sin red, sin ficheros. Tipos de `data-model.md`.
- **Test**: `desktop/src-tauri/src/domain/tests.rs` — transiciones legales e ilegales.

### T008 [US1] — El reconciliador auto-sanador (función pura) · `software-architect` + `backend-engineer`
- `desktop/src-tauri/src/domain/reconcile.rs` — `reconcile(HostFacts, DesiredState) -> Vec<RepairAction>`
- **Test** (`reconcile_tests.rs`), un caso por regla del principio rector:
  máquina rootless preexistente · máquina de otro tamaño · máquina ajena en uso ·
  puerto ocupado · contenedor a medias · compañero a medias · descarga cortada ·
  imagen envejecida · `state.json` ausente · `state.json` corrupto ·
  espacio insuficiente · userns bloqueado · **ausencia de progreso** (misma acción,
  mismo código, dos veces ⇒ `degraded`, nunca bucle).

### T009 [P] [US1] — Puertos y adaptador sobre el CLI embebido · `backend-engineer`
- `desktop/src-tauri/src/app/ports.rs` (`EngineDriver`, `UpdateManifestSource`,
  `PrivilegeHelper`, `StateStore`, `ProgressSink`)
- `desktop/src-tauri/src/infra/embedded_cli.rs` (consume NDJSON, lee el vale por
  `--secret-fd 3` y lo **descarta** tras navegar)
- `desktop/src-tauri/src/infra/state_store.rs` (`~/.safent/app/state.json`, 0600)
- **Test**: doble de CLI (script que emite NDJSON grabado) — **sin contenedores**.

### T010 [US1] — Empaquetado del runtime: podman fijado + imagen de máquina · `devops-engineer` + `backend-engineer`
- `desktop/src-tauri/tauri.conf.json` (`bundle.resources`), `desktop/runtime/manifest.json`
  (sha256 por binario), `desktop/scripts/fetch-runtime.sh` (descarga y verifica en el build)
- Presupuesto medido: macOS arm64 ≈ **1,02–1,10 GB** (12 MB app + ~80 MB podman +
  **932 MB** imagen de máquina) frente al límite de **2 GiB**; Linux ≈ **45–110 MB**
- **Test**: `desktop/src-tauri/src/infra/bundle_tests.rs` — un binario con sha256 que
  no casa **no se ejecuta jamás**.

### T011 [US1] — Servicio de arranque: observar → planificar → aplicar → reobservar · `backend-engineer`
- `desktop/src-tauri/src/app/bootstrap_service.rs`
- `desktop/src-tauri/src/infra/privilege_helper.rs` (Linux: perfil AppArmor `userns`,
  capacidades de `newuidmap`/`newgidmap`, rangos `subuid`/`subgid`; **una** vez, declarado)
- **Test**: convergencia desde cada estado inicial de T008, con el CLI doblado.

### T012 [US1] — Pantalla de preparación: etapas, cancelar, **una** pantalla de fallo · `frontend-engineer` + `content-designer`
- `desktop/ui/index.html` (reescribe la animación de hoy)
- Etapas con nombre y bytes reales · «Cancelar» hasta el punto de no retorno,
  declarado antes · fallo ⇒ causa en lenguaje del dueño + **un** «Reintentar» +
  «Exportar diagnóstico». **Cero** instrucciones de terminal. Teclado y contraste AA.

### T013 [US1] [US3] — Política de ventana: sin navegador, instancia única, bandeja · `frontend-engineer`
- `desktop/src-tauri/src/app/window_policy.rs`, `desktop/src-tauri/tauri.conf.json`,
  `desktop/src-tauri/capabilities/remote-ui.json`
- Sólo navega al destino con vale · **deniega** cualquier navegación a otro origen ·
  sin menú contextual de navegador · segunda apertura **enfoca** la existente ·
  elemento de barra de menús/bandeja con «Abrir», «Reiniciar el motor», «Salir» ·
  cerrar la ventana **no** para el motor (FR-030)
- **Test**: `window_policy_tests.rs` + `desktop/dev-headless-selftest.sh` ampliado.

### T014 [US2] — Orquestador de actualización + actualizador de Tauri · `backend-engineer`
- `desktop/src-tauri/src/app/update_service.rs`, `desktop/src-tauri/src/infra/tauri_updater.rs`
- `desktop/src-tauri/Cargo.toml` + `tauri.conf.json` (`plugins.updater.pubkey`)
- Implementa `contracts/update.md` §4: plan → aplacar la cola (espera acotada ≤ 10 min,
  re-encolar, **sin preguntar**) → descargar → verificar → copia → motor → compañero →
  envoltorio → relanzar → comprobar. Reversión desde cualquier paso posterior a la copia
- **Test**: reversión en cada paso ⇒ la versión anterior queda viva.

---

## Bloque 3 — Anfitrión y compañero (RT)

### T015 [US1] — Andamiaje del compañero **siempre** presente (029 CL-002) · `backend-engineer`
- `ops/container/companions/ads/provision.sh` (modo andamiaje: red + estado + los
  cuatro ficheros, aunque el servicio no esté), `ops/container/run-safent.sh`, `safent` (`_provision_companion`)
- Efecto: instalar Anuncios **no recrea Safent**
- **Test**: `tests/unit/cli/test_companion_scaffold.py` + arranque con andamiaje vacío
  ⇒ `companions.py` degrada a «sin compañero» sin error.

### T016 [US1] — Verbos `companion install|repair` y consumo de la marca en el agente · `backend-engineer`
- `safent` (`cmd_companion_install`, `cmd_companion_repair`, `cmd_agent` con la rama
  nueva, reclamación mutuamente excluyente con la app, TTL por verbo, consumo único)
- **Test**: `tests/unit/cli/test_agent_install_request.py` — marca caducada ⇒ borrada;
  dos lectores ⇒ uno solo actúa; fallo ⇒ vuelve a `pending`, no bucle.

### T017 [P] [US1] — Verbo del daemon que **relee** la presencia del compañero · `backend-engineer`
- `src/hermes/runtime/dbus/` (verbo `ReloadCompanionPresence`), `src/hermes/shell_server/companions.py`
- Resiembra el MCP y la regla de egress sin recrear el contenedor
- **Test**: `tests/unit/agents_os/test_companion_reload.py`.

### T018 [P] [US1] [US3] — La tarjeta de Ads: acción «Instalar» única y URL replegada tras «avanzado» · `frontend-engineer` + `content-designer`
- `frontend/src/components/CompanionInstallAction.tsx` (nuevo — un solo flujo),
  `frontend/src/views/McpView.tsx` (la tarjeta de Ads pasa de campo de URL a
  «Instalar»; el campo `mcp-managed-ads-url` se repliega tras «avanzado»,
  desactivado por defecto y con su validación estricta intacta),
  `frontend/src/components/AdsNavItem.tsx`, `frontend/src/hooks/useAdsAvailability.ts`,
  `frontend/src/api/client.ts`, `frontend/src/lib/i18n.ts`
- Herramientas y el estado `not_installed` de la barra lateral disparan **el mismo**
  flujo · etapas reales · segunda pulsación **no** dispara otra instalación ·
  nunca un `ready` fabricado
- **Test**: `frontend/src/components/CompanionInstallAction.test.tsx` — por defecto,
  **0** campos de conexión antes de `ready`; el campo sólo aparece tras «avanzado».

### T019 [P] [US2] — El pie lee el manifiesto rico; el botón sólo cuando hay novedad · `frontend-engineer`
- `frontend/src/components/Layout.tsx` (`injectedLatestVersion` → `window.__safentUpdate`),
  `frontend/src/api/client.ts`, `frontend/src/api/types.ts`
- Plan vacío ⇒ **ningún** rastro de «Actualizar» en ninguna pantalla
- **Test**: `frontend/src/components/SystemUpdateFooter.test.tsx` — ambos sentidos de SC-006.

### T020 [P] [US1] — Carga sin vale ⇒ **una** pantalla de reconexión, cero ráfagas · `frontend-engineer`
- `frontend/src/lib/token.ts`, `frontend/src/api/client.ts`,
  `frontend/src/components/ReconnectScreen.tsx` (nuevo)
- Sin bearer ⇒ **no se dispara ninguna** llamada a `/api/v1/*`: cortocircuito antes
  del `fetch`, un estado, una acción, **cero** reintentos en bucle (FR-012, SC-012)
- **Test**: `frontend/src/lib/token.test.ts` — 0 peticiones emitidas sin bearer.

---

## Bloque 4 — Onboarding de Ads (ADS)

### T021 [US2 de 029] — Acceso Google Ads por proyecto Cloud, sin token retirado
**Corregida el 11-sep-2026. Repo ADS.** La instrucción anterior de añadir un
developer token era incorrecta. Se elimina de formulario, contrato y SDK;
el onboarding existente se reutiliza, no se duplica.
- `src/safent_ads/accounts/presentation/platform_apps_payloads.py`,
  `src/safent_ads/accounts/application/platform_apps_ports.py`,
  `src/safent_ads/broker/application/app_credentials_service.py`,
  `panel/src/api/schemas/platformApps.ts`, `panel/src/components/connections/ConnectProviderCard.tsx`
- Cliente OAuth del proyecto Cloud; gestora solo si aplica. Ningún token retirado.
- Un rechazo `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` debe explicar dónde
  gestionar el acceso en Google Cloud, sin prometer aprobación automática.
- **Test**: `tests/integration/accounts/test_platform_apps_router.py` +
  `tests/unit/accounts/presentation/test_platform_apps_payloads.py` — el campo
  retirado se rechaza y el SDK no lo envía; errores de acceso visibles sin secretos.

### T022 [P] [US2 de 029] — Estado de onboarding legible desde el puente · `backend-engineer`
**Repo ADS.** Qué falta para `ready`: credenciales del vendor por plataforma y
cuentas conectadas.
- `src/safent_ads/settings/presentation/rest.py` (o `accounts`): estado agregado que
  el puente `/ads/` expone para que Safent distinga `unauthorized` de `no_accounts`
- **Test**: `tests/integration/settings/test_onboarding_status.py`.

---

## Bloque 5 — Pipeline (AA)

### T023 [US1] [US2] — Publicar el paquete completo, con notarización como **gate duro** · `devops-engineer`
**Repo AA.** `/.github/workflows/safent-desktop.yml`:
- Entrada `ref` ya existe; añadir el paso **descargar y verificar por sha256 el
  runtime empaquetado** (podman fijado + imagen de máquina) antes de `tauri-action`
- **Quitar** `continue-on-error: true` del paso de notarización y **quitar** el
  comentario «403 hasta que el titular acepte»: el acuerdo de Apple **ya está
  aceptado**. Notarizar + **grapar el DMG Y el `.app`** (dos operaciones de
  grapado independientes, la misma notarización — MAC2-12,
  verificacion-mac-2.md: grapar el DMG NO grapa lo que hay dentro; el `.app`
  suelto que este mismo pipeline publica como `macos/Safent.app.tar.gz` para
  el actualizador no tiene, si no, ninguna vía offline de validarse) pasa a
  ser condición de release; se reintenta ante fallo transitorio, no se degrada.
  **Orden exacto (MAC4-06, verificacion-mac-4.md — el orden actual sigue
  fallando: `stapler validate Safent.app` → rc=65 dentro de un DMG ya
  notarizado y grapado)**: `xcrun stapler staple` **escribe** el vale dentro
  del propio paquete, así que necesita un destino escribible — grapar el
  `.app` DESPUÉS de copiarlo a la imagen de disco final (de solo lectura
  para distribución) no tiene dónde escribir in situ. Grapar el `.app` en
  cuanto `notarytool submit --wait` confirma `Accepted`, **mientras sigue
  suelto en la carpeta de trabajo** — ANTES de construir el DMG y ANTES de
  comprimirlo a `Safent.app.tar.gz` — y construir AMBOS artefactos a partir
  de esa MISMA copia ya grapada; grapar el `.dmg` en sí es una segunda
  llamada independiente, después, sobre el fichero ya construido (ver
  `desktop/RUNTIME-BUNDLE.md` para el razonamiento completo).
- Pasar `TAURI_SIGNING_PRIVATE_KEY` y publicar `latest.json`
  (`includeUpdaterJson: true`) **y** `runtime-manifest.json` firmado
- Gate de publicación: si falta cualquiera de los cuatro artefactos, **no hay release**
- **Prueba**: un `workflow_dispatch` sobre `feat/safent-next` produce DMG **notarizado
  y grapado** (`stapler validate` PASA), .deb, .AppImage, `latest.json` y `runtime-manifest.json`.

---

## Bloque 6 — Escenario en vivo (obligatorio para cerrar)

### T024 [US1] [US2] [US3] — Recorrido completo en el MacBook Air del dueño · `qa-engineer` + el dueño
**No es simulable.** La DGX no tiene sesión gráfica ni build de Tauri (filas
DESK-01…DESK-04 de la matriz 025 son `NO-APLICA-DGX`).
- Equipo: macOS 15.5 Apple Silicon, 24 GB, **con podman 5 en `/opt/podman/bin` y una
  máquina libkrun previa** — es el caso de adopción real, no un equipo limpio
- Ejecutar `quickstart.md` entero, sección a sección, apuntando PASA/FALLA con evidencia
- **Condiciones de cierre**: 0 ventanas de navegador · 0 pasos de terminal ·
  0 direcciones locales visibles · 0 preguntas que no sean del sistema operativo ·
  0 avisos de origen desconocido · 0 relanzamientos manuales · la máquina libkrun
  preexistente **intacta o adoptada**, nunca modificada ni borrada
- Resultado → `specs/028-safent-app-nativa/resultados-macbook-air.md` (mismo formato que la matriz 025).

---

## Orden y dependencias

```
T001 T002 T003            (bloqueantes; en paralelo entre sí)
   └► T004 T005 T006      (contratos; T004 antes que todo el Bloque 2)
        └► T007 ► T008 ► T009 ► T010 ► T011 ► T012 ► T013 ► T014
        └► T015 ► T016 ► T017
                    └► T018 T019 T020         (frontend, en paralelo)
        └► T021 ► T022                        (ADS, en paralelo con RT)
                    └► T023                   (pipeline; necesita T010)
                          └► T024             (en vivo; necesita todo)
```

**Tests primero** en T004, T006, T008, T014, T020: son las piezas donde un fallo
silencioso rompe el principio rector, y sus tests son ejecutables sin contenedores
ni red (Principio V de la constitución).
