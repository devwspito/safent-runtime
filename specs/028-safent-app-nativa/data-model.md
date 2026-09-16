# Data Model — 028 (app nativa) + 029 (Ads de un botón)

Modelo de dominio en lenguaje ubicuo. Sin ORM, sin SQL, sin decoradores de
serialización. El dominio de esta entrega vive en el **envoltorio nativo**
(`desktop/src-tauri`) y en el **CLI anfitrión** (`safent`); el daemon sólo
aporta hechos observados y consume solicitudes.

## Ubiquitous language

| Término | Definición |
|---|---|
| **App** | Lo que el dueño descarga, instala, abre y actualiza. Tiene nombre, icono y **una** ventana. Es lo único que el dueño nombra. |
| **Ventana** | La única superficie del producto. No es un navegador: sin barra de direcciones, sin pestañas, sin «abrir fuera». |
| **Motor** (`Engine`) | El servicio local enjaulado que ejecuta el producto y custodia los datos. El dueño nunca lo arranca ni lo nombra. |
| **Base de ejecución** (`ContainerRuntime`) | El podman **empaquetado y fijado** que la app trae dentro. Deja de ser una dependencia del equipo. |
| **Máquina** (`EngineMachine`) | En macOS, la VM que aloja el motor. Se **adopta** una existente si sirve, o se crea la nuestra. Nunca se modifica ni se borra una ajena. |
| **Hechos del equipo** (`HostFacts`) | La foto observada del equipo en un instante: sistema, arquitectura, disco, memoria, runtime, máquina, contenedor, puerto, volumen, compañero, daemon. |
| **Estado deseado** (`DesiredState`) | Lo que tiene que ser cierto para que el producto esté listo, expresado en digests y nombres, nunca en pasos. |
| **Reconciliación** | Llevar los hechos al estado deseado aplicando acciones idempotentes, sin preguntar nada. |
| **Acción de reparación** (`RepairAction`) | Un paso idempotente, reanudable y declarable que acerca los hechos al estado deseado. |
| **Etapa** (`Stage`) | La fase con nombre que el dueño lee mientras se prepara o se actualiza. |
| **Vale de arranque** (`BootstrapTicket`) | La credencial de un arranque que autoriza la ventana ante el motor. Nace y muere dentro de la app; **nunca se persiste**. |
| **Conjunto de versiones** (`VersionSet`) | Envoltorio + motor + compañero. Por fuera, una sola versión de Safent. |
| **Plan de actualización** (`UpdatePlan`) | Lo que hay que sustituir y en qué orden, con su punto de no retorno declarado. |
| **Compañero** (`Companion`) | Capacidad adicional servida dentro de la misma ventana. Hoy, exactamente uno: `safent-ads`. |
| **Andamiaje del compañero** (`CompanionScaffold`) | Red fija + estado + los cuatro ficheros de sólo lectura. Existe **siempre**, esté o no instalado el compañero. |
| **Solicitud de instalación** (`InstallRequest`) | La marca que la UI deja para que el agente anfitrión la cumpla. Se consume una vez y caduca. |
| **Agente anfitrión** (`HostAgent`) | El proceso del host (launchd/systemd) — y, cuando la app está abierta, la propia app — único autorizado a crear contenedores hermanos. |
| **Diagnóstico** (`DiagnosticBundle`) | Lo exportable para soporte. Nace sin secretos por construcción. |

## Bounded contexts

- **Bootstrap** (`desktop/src-tauri`, dueño: envoltorio nativo) — responsabilidad:
  llevar el equipo de «recién instalado» a «producto listo» sin decisiones del
  usuario. Aggregates: `EngineLifecycle`, `RuntimeBundle`. Contrato hacia fuera:
  `contracts/app-engine.md` (NDJSON de etapas sobre el CLI embebido).
- **Actualización** (`desktop/src-tauri` + `shell_server`) — responsabilidad:
  saber si hay novedad de verdad y aplicarla entera o ninguna. Aggregates:
  `UpdatePlan`. Contrato: `contracts/update.md`.
- **Compañeros** (`safent` CLI + `shell_server` + frontend) — responsabilidad:
  instalar, reparar y quitar el compañero sin terminal. Aggregates:
  `CompanionInstallation`, `InstallRequest`. Contrato: `contracts/install-request.md`.
- **Producto** (`src/hermes`, sin cambios de dominio aquí) — recibe hechos y
  publica salud; no conoce al envoltorio.
- **Onboarding de Ads** (`safent-ads`, contexto ajeno) — capa anticorrupción: el
  puente de sesión same-origin `/ads/` (026). Safent **no** modela credenciales de
  plataformas; sólo sabe si el compañero se declara `ready`.

## Aggregates / entities / value objects

### EngineLifecycle *(aggregate root del contexto Bootstrap)*

- **Invariantes**
  1. El vale de arranque **nunca** se persiste ni entra en ningún evento, registro
     o diagnóstico. Vive en memoria del envoltorio durante un arranque.
  2. La ventana sólo navega a un destino con vale válido; jamás a un destino sin
     vale y jamás a un origen externo.
  3. Toda transición que ejecuta trabajo es **reanudable**: el estado persistido
     basta para retomar sin repetir lo ya hecho ni dejar el equipo a medias.
  4. Ninguna transición pide una decisión al dueño. Las únicas interacciones
     posibles son las autorizaciones del propio sistema operativo.
  5. `degraded` sólo se alcanza tras **ausencia de progreso** demostrada (la misma
     acción falla dos veces con el mismo código), y siempre con una causa nombrada.
  6. Hay como mucho **una** instancia viva; una segunda apertura enfoca la primera.

- **Estado / ciclo de vida**

```
                    ┌──────────────────────────── retry ────────────────────────────┐
                    v                                                               │
  fresh ─► preflight ─► runtime_staging ─► engine_provisioning ─► engine_pulling ─► engine_starting ─► engine_ready
              │              │                    │                     │                  │              │
              │              │                    │                     │                  │              ├─► companion_provisioning ─► companion_ready
              │              │                    │                     │                  │              │            │
              │              │                    │                     │                  │              │            └───────────────► degraded(companion_*)
              │              │                    │                     │                  │              │
              │              │                    │                     │                  │              ├─► updating ─► engine_ready (versión nueva)
              │              │                    │                     │                  │              │      └────► degraded(update_*) con la versión anterior viva
              │              │                    │                     │                  │              │
              │              │                    │                     │                  │              └─► reconnecting ─► engine_ready
              └──────────────┴────────────────────┴─────────────────────┴──────────────────┴─► repairing ─► (reintenta la etapa) 
                                                                                                  └─► degraded(<causa>) ─► [Reintentar] ─► repairing
```

  - `preflight` → comprueba sistema servido, arquitectura, disco y memoria **antes
    de descargar nada** (FR-008). Un equipo no servido termina aquí, declarado.
  - `runtime_staging` → despliega el podman empaquetado a `~/.safent/runtime/<versión>/`,
    **verifica cada binario por sha256** contra el manifiesto embebido y resuelve
    `SAFENT_PODMAN`.
  - `engine_provisioning` → macOS: adopta una máquina apta o crea la nuestra a
    partir de la imagen empaquetada, **sin red**; Linux: inicializa el almacén
    rootless y, si el kernel lo exige, invoca **una vez** el ayudante privilegiado.
  - `engine_pulling` → trae runtime y compañero **por digest**, reanudando por capa.
  - `engine_starting` → crea el contenedor con la jaula canónica y espera salud.
  - `engine_ready` → la ventana navega al producto. Es el único estado en que se
    muestra producto.
  - `reconnecting` → **red de seguridad de FR-012**: una carga sin vale válido
    resuelve aquí, con una acción y **cero** peticiones en bucle.
  - `repairing` → el reconciliador está aplicando acciones; siempre reversible.
  - `degraded(reason)` → una pantalla, una causa en lenguaje del dueño, un
    «Reintentar», y el diagnóstico a un gesto. **Nunca una instrucción de terminal.**

- **Atributos** (tipos del dominio, no primitivos)
  `phase: EnginePhase` · `stage: Stage` · `progress: StageProgress` ·
  `attempt: AttemptCount` · `runtime: RuntimeBundleRef` · `machine: MachineRef|None` ·
  `engine: ImageRef(digest)` · `companion: ImageRef(digest)|None` ·
  `endpoint: LoopbackEndpoint` (puerto elegido, **nunca** expuesto a la UI) ·
  `ticket: BootstrapTicket` (**sólo en memoria**) · `lastFailure: FailureCause|None`

- **Eventos de dominio**
  `StageEntered`, `StageProgressed`, `StageCompleted`, `EngineReady`,
  `EngineDegraded`, `RepairApplied`, `NoProgressDetected`, `WindowNavigated`.

### RuntimeBundle *(value object)*

- **Invariantes**: cada binario lleva su `sha256` en el manifiesto embebido en el
  paquete; un binario que no verifica **no se ejecuta jamás**. El manifiesto no se
  descarga: viaja firmado dentro del instalador.
- **Atributos**: `podmanVersion: SemVer` · `entries: [BinaryEntry(path, sha256, mode)]` ·
  `machineImage: MachineImageRef(disktype, arch, sha256)|None` (ausente en Linux).

### HostFacts *(value object — la foto observada)*

`os` · `arch` · `freeDiskBytes` · `totalMemoryBytes` · `runtimeStaged: bool` ·
`runtimeHashOk: bool` · `machines: [MachineFact(name, provider, rootful, running, ours)]` ·
`engineContainer: ContainerFact(exists, running, imageDigest)|None` ·
`localEngineImageDigest: Digest|None` · `localCompanionImageDigest: Digest|None` ·
`publishedPort: Port|None` · `dataVolume: bool` · `companionScaffold: bool` ·
`companionContainers: (running, total)` · `companionHealth: CompanionHealth` ·
`daemonHealth: DaemonHealth` · `appVersion: SemVer|None` · `userNsAllowed: bool` ·
`helperInstalled: bool`.

**Ambiguity resolved in the app-desk-integration pass**: this list originally
omitted `localEngineImageDigest`/`localCompanionImageDigest`, and listed
`appVersion` as always present. Neither survived contact with the real CLI:
`reconcile.rs`'s `images_gap`/`companion_gap` need "is the desired digest
present locally" **independent of** whether a container already runs it
(`engineContainer.imageDigest` alone conflates the two) — without a separate
signal, `images_gap` would return `PullEngine` on every single observation,
forever, even against an already-converged engine. `cmd_facts` (`safent`)
now reports both via `podman image exists <digest-pinned ref>` — `null` for a
non-digest-pinned image or one not yet pulled. `appVersion` is genuinely
`null` on the wire whenever the engine has never run (the ordinary
fresh-install observation), not only ever a string.

### RepairAction *(value object, cerrado)*

`StageRuntime` · `AdoptMachine(name)` · `CreateMachine` · `StartMachine(name)` ·
`InstallPrivilegedHelper` · `PullEngine(digest)` · `PullCompanion(digest)` ·
`ChoosePort` · `CreateContainer` · `StartContainer` · `EnsureCompanionScaffold` ·
`ComposeCompanionUp(digest)` · `ReloadCompanionPresence` · `RecreateEngine` ·
`FocusExistingWindow`.

**Invariante del conjunto**: toda acción es idempotente, declara su etapa y su
coste estimado, y **ninguna borra datos del dueño ni modifica una máquina ajena**.

### CompanionInstallation *(aggregate root del contexto Compañeros)*

- **Invariantes**
  1. El destino del compañero lo fija el instalador local (red fija, IP, puerto,
     huella de CA). **Nunca** una URL escrita por el dueño en el camino normal.
  2. La imagen se aplica **por digest**; nunca se re-deriva una etiqueta en un
     verbo disparado desde la UI (raíz del fallo CLI-10).
  3. `ready` sólo se declara con el puente `/ads/` respondiendo de verdad; una
     salud fabricada está prohibida.
  4. Instalar dos veces converge al mismo estado: ni red, ni datos, ni
     credenciales duplicados.
  5. El andamiaje existe siempre; el servicio, sólo si está instalado.
- **Estado**: `not_installed → installing → ready` · `ready ⇄ unreachable` ·
  `ready → unauthorized|no_accounts` (faltan credenciales o cuentas) ·
  `ready|unreachable → removing → not_installed`.
- **Atributos**: `slug: CompanionSlug` · `image: ImageRef(digest)` ·
  `scaffold: CompanionScaffold` · `health: CompanionHealth` ·
  `retention: DataRetention(keep|purge)`.
- **Eventos**: `CompanionInstallRequested`, `CompanionStageProgressed`,
  `CompanionReady`, `CompanionInstallFailed(cause)`, `CompanionRemoved(retention)`.

### InstallRequest *(aggregate root — el disparador UI → host)*

- **Invariantes**
  1. Vocabulario **cerrado** de verbos (`install_companion`, `repair_companion`,
     `remove_companion`, `update_system`, `uninstall_system`). Ningún verbo lleva
     una orden, una ruta ni una URL: sólo un enum y, como mucho, un slug validado.
  2. Se consume **una sola vez** (borrado antes de ejecutar).
  3. **Caduca**: si nadie la recoge en su ventana de vida, expira y la UI vuelve a
     ofrecer la acción diciendo honestamente que nadie la atendió.
  4. Una solicitud viva impide crear otra del mismo verbo (idempotencia de FR-008
     de 029).
- **Estado**: `pending → claimed → applied` · `pending → expired` ·
  `claimed → failed(cause) → pending` (reintentable).
- **Atributos**: `verb: HostVerb` · `slug: CompanionSlug|None` ·
  `createdAt` · `expiresAt` · `attempt: AttemptCount` · `lastCause: FailureCause|None`.
- **Eventos**: `InstallRequested`, `InstallRequestClaimed`, `InstallRequestApplied`,
  `InstallRequestExpired`, `InstallRequestFailed`.

### UpdatePlan *(aggregate root del contexto Actualización)*

- **Invariantes**
  1. Sólo existe si **de verdad** hay una pieza más nueva. Un plan vacío no se
     construye, y sin plan **no hay botón** (FR-015).
  2. Se aplica entero o se revierte: envoltorio, motor y compañero avanzan juntos.
  3. Antes del punto de no retorno se hace copia; ante cualquier fallo posterior se
     restaura y la versión anterior queda **viva**.
  4. Verificación de procedencia e integridad **antes** de aplicar cada pieza.
  5. El recorrido acaba con la ventana abierta en el producto.
- **Estado**: `planned → downloading → verified → backing_up → applying_engine →
  applying_companion → applying_app → relaunching → done` ·
  cualquier paso `→ rolling_back → rolled_back(cause)`.
- **Atributos**: `from: VersionSet` · `to: VersionSet` ·
  `pieces: [UpdatePiece(kind, from, to, sizeBytes)]` ·
  `pointOfNoReturn: Stage` · `backup: BackupRef|None`.
- **Eventos**: `UpdateAvailable`, `UpdateStarted`, `PieceVerified`, `BackupTaken`,
  `PieceApplied`, `RelaunchScheduled`, `UpdateCompleted`, `UpdateRolledBack(cause)`.

### VersionSet *(value object)*

`app: SemVer` · `engine: ImageRef(digest)` · `companion: ImageRef(digest)|None`.
**Invariante**: la versión que la app muestra es la que **está corriendo**; una
discrepancia entre piezas se declara con su acción, nunca se maquilla (FR-021).

## Domain events

- **`StageEntered`** — emitido por `EngineLifecycle` al entrar en una etapa.
  Payload: `stage`, `humanLabel`, `estimatedBytes?`.
- **`StageProgressed`** — cada ≤ 5 s mientras haya trabajo (NFR-001/NFR-002).
  Payload: `stage`, `done`, `total`, `unit`.
- **`EngineReady`** — el producto es alcanzable. Payload: `versionSet`. **Sin vale.**
- **`EngineDegraded`** — tras `NoProgressDetected`. Payload: `cause`, `humanLabel`,
  `retryable: true`.
- **`InstallRequested` / `InstallRequestExpired`** — 029, ciclo de la marca.
  Payload: `verb`, `slug?`, `expiresAt`.
- **`CompanionReady`** — el puente `/ads/` respondió de verdad. Payload: `slug`.
- **`UpdateAvailable`** — hay plan no vacío. Payload: `from`, `to`, `pieces`.
- **`UpdateRolledBack`** — Payload: `cause`, `restoredVersionSet`.

**Regla transversal**: ningún evento lleva el vale de arranque, ni el puerto, ni
una URL local, ni una credencial. El diagnóstico se construye **sólo** a partir de
estos eventos, por lo que nace libre de secretos por construcción (FR-029, SC-009).

## Relationships

```
EngineLifecycle 1 ──── 1 RuntimeBundle          (la app trae exactamente uno)
EngineLifecycle 1 ──── 0..1 EngineMachine       (sólo macOS; adoptada o propia)
EngineLifecycle 1 ──── 1 VersionSet             (lo que corre ahora)
EngineLifecycle 1 ──── 0..1 UpdatePlan          (a lo sumo uno vivo)
EngineLifecycle 1 ──── 0..1 CompanionInstallation
CompanionInstallation 1 ── 1 CompanionScaffold  (el andamiaje existe siempre)
InstallRequest  * ──── 1 HostAgent              (una viva por verbo)
HostFacts ──(entrada)──► reconcile ──(salida)──► [RepairAction]
```

Dependencias **sólo hacia dentro**: envoltorio → CLI embebido → motor. El motor no
conoce al envoltorio; `safent-ads` no conoce a ninguno de los dos.

## Persisted state (lo único que sobrevive a un cierre)

`~/.safent/app/state.json` — 0600, propiedad del usuario, **sin secretos**:

| Campo | Tipo | Para qué |
|---|---|---|
| `schema_version` | entero | Migración del propio fichero |
| `phase` | enum de `EnginePhase` | Retomar donde se quedó |
| `stage` | enum de `Stage` | Etapa exacta dentro de la fase |
| `attempt` | entero | Detectar ausencia de progreso |
| `last_failure` | `{code, at}` | Comparar dos fallos idénticos seguidos |
| `runtime_bundle` | `{version, sha256}` | Saber si hay que re-desplegar el runtime |
| `machine` | `{name, adopted: bool}` | No recrear ni tocar una máquina ajena |
| `engine_digest` | digest | Estado deseado del motor |
| `companion_digest` | digest \| null | Estado deseado del compañero |
| `port` | entero | Reutilizar el puerto elegido; **nunca** se muestra |
| `helper_installed` | booleano | No volver a pedir la autorización del sistema |
| `updated_at` | marca de tiempo | Antigüedad del estado |

**Prohibido** en este fichero: el vale de arranque, cualquier credencial, cualquier
URL con `?k=`. `~/.safent/app/backups/<versionSet>/` guarda la copia previa a un
punto de no retorno; se poda dejando la última buena.

## Migration plan (traspaso a `database-engineer`)

Ninguna migración de base de datos: esta entrega no toca el esquema del daemon ni
el de safent-ads. Cambios de forma, en modo expandir → contraer:

1. **Expandir** — `state.json` nace con `schema_version: 1`. Una instalación sin
   fichero se trata como `fresh` y el reconciliador la converge (adopción de
   FR-028: contenedor y volumen existentes se **adoptan**, no se duplican).
2. **Expandir** — `companions.json` y sus tres ficheros hermanos pasan a existir
   **siempre** (andamiaje vacío incluido). `companions.py` ya degrada a «sin
   compañero» ante un fichero inválido: no requiere cambio de contrato.
3. **Expandir** — `GET /api/v1/system/update` añade `engine_digest`,
   `companion_digest` y `pieces` **sin quitar** `current_version` /
   `latest_version` / `update_available`. Los consumidores viejos siguen leyendo.
4. **Corrección (11-sep-2026, safent-ads)** — `SetGoogleAppCredentialsRequest` y
   `GoogleAppCredentialsInput` conservan solo el cliente OAuth y la gestora
   opcional. Google retiró los developer tokens el 9-sep-2026: no se añade,
   cifra ni devuelve ese campo, tampoco como compatibilidad opcional.
5. **Contraer (más adelante, no aquí)** — retirar `vendor.env` como camino
   documentado una vez la UI cubra las cinco credenciales.
