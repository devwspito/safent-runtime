# Contract — `app-engine`: el envoltorio nativo ↔ el CLI embebido

**Fuente de verdad de la forma.** El envoltorio (`desktop/src-tauri`) es el único
consumidor; el CLI embebido (`safent`, con `ops/container/run-safent.sh`) es el
único proveedor. Ninguna otra pieza habla este protocolo.

## 1. Invocación

```
<bundle>/engine/safent <verb> [args] --porcelain
```

Entorno que el envoltorio **fija siempre**:

| Variable | Valor | Por qué |
|---|---|---|
| `SAFENT_PODMAN` | ruta absoluta al podman empaquetado | Nunca el del PATH del usuario |
| `SAFENT_NO_BROWSER` | `1` | La app enseña el producto en su ventana |
| `SAFENT_NO_SELF_UPDATE` | `1` | El CLI viaja dentro del paquete; lo actualiza la app |
| `SAFENT_IMAGE` | `ghcr.io/devwspito/safent@sha256:…` | Digest, jamás una etiqueta |
| `SAFENT_ADS_IMAGE` | `ghcr.io/devwspito/safent-ads@sha256:…` | Idem (raíz del fallo CLI-10) |
| `SAFENT_STATE_HOME` | `~/.safent` | Un solo árbol de estado |

`--porcelain` **no** altera el comportamiento: sólo cambia el canal de progreso.
Sin la bandera, el CLI escribe el mismo texto humano de hoy (el operador de
terminal no pierde nada).

## 2. Canales

- **stdout** — exclusivamente **NDJSON**: un objeto por línea, UTF-8, sin
  agrupar. Nada más se escribe aquí.
- **stderr** — texto humano libre, para el diagnóstico. El envoltorio **no lo
  interpreta**; lo guarda para `diagnostics`.
- **código de salida** — `0` éxito · `10..39` fallo de dominio con `code`
  reportado en un evento `failed` previo · `1` fallo no clasificado.

## 3. Eventos (NDJSON)

```ts
type StageId =
  | 'preflight' | 'runtime_staging' | 'machine' | 'pull_engine' | 'pull_companion'
  | 'container' | 'health' | 'companion_scaffold' | 'companion_up'
  | 'companion_reload' | 'backup' | 'restore' | 'cleanup'

type EngineEvent =
  | { t: 'stage';    id: StageId; label: string; total_bytes?: number }
  | { t: 'progress'; id: StageId; done: number; total?: number; unit: 'bytes' | 'layers' | 'steps' }
  | { t: 'done';     id: StageId; ms: number }
  | { t: 'failed';   id: StageId; code: FailureCode; detail: string; retryable: boolean }
  | { t: 'facts';    facts: HostFacts }
  | { t: 'ready';    endpoint_ref: 'stdout-secret' }   // ver §5
```

**Invariantes de forma**

1. Ningún evento contiene el vale de arranque, el puerto, una URL local ni una
   credencial. `detail` es texto libre **saneado**: el CLI enmascara cualquier
   coincidencia con el vale antes de emitir.
2. `progress` llega al menos cada **5 s** mientras la etapa está viva
   (NFR-001/NFR-002). Una etapa sin `progress` durante 5 s es «estancada» y el
   envoltorio lo declara como tal.
3. Cada `stage` cierra con exactamente un `done` **o** un `failed`.
4. `label` viene en español y en vocabulario del dueño; el envoltorio lo pinta tal cual.

`FailureCode` (cerrado, estable):
`unsupported_os` · `unsupported_arch` · `insufficient_disk` · `insufficient_memory`
· `runtime_hash_mismatch` · `machine_create_failed` · `machine_start_failed`
· `userns_blocked` · `helper_denied` · `registry_unreachable` · `digest_mismatch`
· `pull_interrupted` · `port_exhausted` · `container_start_failed`
· `daemon_unhealthy` · `companion_network_conflict` · `companion_migration_failed`
· `companion_unreachable` · `backup_failed` · `restore_failed` · `clock_skew`.

**Integridad del runtime empaquetado, por plataforma** (decisión del dueño,
11-sep-2026 — "el código más simple es el que funciona mejor"; `runtime_hash_mismatch`
cubre ambos casos): en **macOS**, la firma de código de Apple — `cmd_stage_runtime`
verifica el `.app` entero con un `codesign --verify --strict` superficial, nunca
un `sha256`/`cdhash` por fichero. En **Linux**, el manifiesto `sha256` de
`runtime-bundle.json`, sin cambios.

**Vocabulario de `HostFacts.os`** (MAC-01, verificacion-mac-1.md): el CLI
deriva `os` de su propio `uname -s` en minúsculas — **`"darwin"`** (macOS) o
**`"linux"`**, nunca un nombre de producto (`"macos"` no es, ni ha sido nunca,
un valor real emitido por `cmd_facts`). El envoltorio (`engine_adapter.rs`,
`map_os`) acepta exactamente esas dos cadenas; cualquier otra cosa mapea a
`HostOs::Unsupported` → `unsupported_os` (no retryable). Las dos partes deben
seguir esta única tabla si el vocabulario alguna vez crece:

| `uname -s` | `HostFacts.os` | `HostOs` |
|---|---|---|
| `Darwin` | `darwin` | `MacOs` |
| `Linux` | `linux` | `Linux` |
| cualquier otro | el valor crudo de `uname -s` | `Unsupported` |

**Vocabulario de `HostFacts.machines[]`** (MAC2-01, verificacion-mac-2.md): el
CLI deriva `provider`/`cpus`/`memoryBytes` de `podman machine list --format
json` — **nunca** `machine inspect`, que en el podman real (6.1.1) no expone
NINGÚN campo de proveedor/tamaño (`--format '{{.VMType}}'` sobre `inspect`
falla con «can't evaluate field VMType in type machine.InspectInfo»; sólo
`list` lo tiene). `rootful`/`running` siguen viniendo de `machine inspect`
(el único sitio que los tiene). No existe `osVersion`: podman no tiene ningún
concepto de «versión del SO» por máquina en ninguno de los dos comandos, así
que `MachineSpec` no lo compara — compararlo era comparar un valor que el CLI
jamás podía informar de verdad, y `is_satisfied_by` no coincidía **nunca**
(MAC2-01/MAC2-06: cada arranque trataba la máquina recién creada como a la
deriva permanente).

| Campo | Origen | Vocabulario |
|---|---|---|
| `provider` | `machine list --format json` → `.VMType` | `applehv` · `qemu` · `hyperv` · `wsl` · `libkrun` · cualquier otro string tal cual |
| `cpus` | `machine list --format json` → `.CPUs` | entero |
| `memoryBytes` | `machine list --format json` → `.Memory` (bytes, como string) | entero (bytes) |
| `rootful` | `machine inspect <name> --format '{{.Rootful}}'` | booleano |
| `running` | `machine inspect <name> --format '{{.State}}'` == `running` | booleano |
| `ours` | `$SAFENT_STATE_HOME/machine.json`'s `name` == esta máquina | booleano |

**Vocabulario de `HostFacts.engineContainer.imageDigest`** (MAC3-02,
verificacion-mac-3.md): el CLI lee `inspect -f '{{.ImageDigest}}'` — **nunca**
`{{.Image}}`, que en el podman real (6.1.1) devuelve el **ID local** de la
imagen (`365e584d7f5c…`), no un digest, confirmado en vivo contra un
contenedor real. `images_gap` (`reconcile.rs`) compara este campo contra
`desired.engine_image.digest` (siempre `sha256:…`); comparar un ID nunca
podía coincidir, así que un contenedor sano y con el digest correcto se
destruía y recreaba en cada arranque. Si `{{.ImageDigest}}` viene vacío (no
se espera en el flujo propio de esta app — todo contenedor que crea arranca
una imagen bajada por digest), se usa `{{.Image}}` como respaldo — un ID, que
por construcción nunca coincide con un digest deseado, así que el respaldo
sigue siendo seguro (pide recrear) en vez de fingir una coincidencia.

## 4. Verbos

| Verbo | Qué hace | Idempotente | Etapas que emite |
|---|---|---|---|
| `facts --json` | Observa el equipo y emite **un** `facts`. No modifica nada. | sí (puro) | — |
| `stage-runtime` | Despliega y **verifica por sha256** el podman empaquetado. | sí | `runtime_staging` |
| `ensure-machine` | macOS: adopta una máquina apta o crea la nuestra desde la imagen empaquetada, **sin red**. Linux: prepara el almacén rootless. | sí | `machine` |
| `ensure-images` | `pull` por digest con reintentos y reanudación por capa. | sí | `pull_engine`, `pull_companion` |
| `up` | Elige puerto libre, crea el contenedor con la jaula canónica y espera salud. | sí | `container`, `health`, `ready` |
| `companion install\|repair\|remove [--purge]` | Ciclo de vida del compañero, imagen **por digest**. | sí | `companion_*` |
| `update --to <VersionSet>` | Copia previa, sustitución, migración y reversión. | sí | `backup`, `pull_*`, `container`, `health`, `restore?` |
| `uninstall --scope this-install` | Retira **sólo** lo que esta app instaló. | sí | `cleanup` |
| `diagnostics --out <path>` | Empaqueta eventos + stderr + `facts`. **Sin secretos.** | sí | — |

**`--scope this-install`** es obligatorio y corrige el hallazgo UPD-06 de la matriz
025: el `uninstall` de hoy borra agentes y binarios de otras instalaciones.

## 5. Entrega del vale de arranque

`up` termina emitiendo `{ t: 'ready', endpoint_ref: 'stdout-secret' }` y, **acto
seguido**, escribe **una única línea** en un descriptor dedicado (`--secret-fd N`,
por defecto 3) con la forma `http://127.0.0.1:<puerto>/?k=<vale>`.

- Nunca en stdout, nunca en argv, nunca en el entorno, nunca en un fichero.
- El envoltorio la lee, navega y **descarta** la cadena. No la persiste ni la
  vuelve a pedir salvo en un nuevo arranque del motor.
- Si el descriptor se cierra sin línea, el envoltorio entra en `reconnecting`
  (FR-012) — nunca en un bucle de peticiones.

## 6. Cancelación

`SIGINT` al proceso hijo: el CLI aborta la etapa viva, emite
`{t:'failed', code:…, retryable:true}` y sale. **Ninguna etapa deja el equipo a
medias**: el estado siempre es reanudable (los digests ya bajados se conservan; una
máquina a medio crear se marca y el reconciliador la retoma o la descarta).
Después del `stage` declarado como punto de no retorno (`applying_engine` en una
actualización), la cancelación se rechaza y el envoltorio deshabilita el gesto —
lo declara antes, nunca después (NFR-003).

## 7. Superficie que el envoltorio expone al producto (IPC de Tauri)

La página remota (`http://127.0.0.1:*`) **no** recibe permisos de núcleo. Sólo
sobreviven, sobre el origen local, los dos ya existentes del portapapeles del host
(`allow-read-host-clipboard`, `allow-write-host-clipboard`) y se añade **uno**:

```ts
/** Estado que el producto pinta sin poder provocarlo. Sólo lectura. */
declare function safentAppStatus(): Promise<{
  app_version: string
  update: { available: boolean; to?: { app?: string; engine?: string; companion?: string } }
  engine_phase: 'engine_ready' | 'updating' | 'degraded' | 'reconnecting'
}>
```

**Prohibido** exponer a la página: instalar, actualizar, desinstalar, navegar,
abrir en el navegador, leer ficheros o ejecutar procesos. Esas acciones se
disparan por el camino de la marca (`install-request.md`), que tiene vocabulario
cerrado y las cumple el agente anfitrión.

## 8. Canal wrapper → webview (loader local, `desktop/src`)

**Distinto de §3.** El protocolo de §1-§6 es CLI↔envoltorio (`safent`
--porcelain → `desktop/src-tauri`). Esta sección documenta el segundo salto,
envoltorio→ventana, que §1 ya avisaba que nadie más habla — hasta ahora no
estaba escrito, y la línea UI y la línea del núcleo habían asumido formas
distintas (ver `desktop/UI-STATES.md`). Esta es la forma real que
`desktop/src-tauri/src/boot.rs` emite y que `desktop/src/lifecycle.ts` consume.

Dos canales Tauri, ambos sólo hacia la ventana **local** (`index.html`, nunca
el origen remoto del producto):

- **`safent://engine-event`** — un evento por cada `DomainEvent` relevante,
  discriminante **`kind`** (no `t`), campo de etapa **`stage`** (no `id`):

```ts
type EngineEvent =
  | { kind: 'stage'; stage: StageId; label: string; total_bytes: number | null; point_of_no_return: boolean }
  | { kind: 'progress'; stage: StageId; done: number; total: number | null; unit: 'bytes' | 'layers' | 'steps' }
  | { kind: 'done'; stage: StageId; ms: number }
  | { kind: 'failed'; code: FailureCode; detail: string; retryable: boolean }
  | { kind: 'ready'; app_version: string; engine_digest: string; companion_digest: string | null }
```

  - `point_of_no_return` viaja **en cada `stage`** (`boot.rs::bootstrap_point_of_no_return`)
    — el cliente ya no adivina qué etapas son irreversibles; NFR-003 ("lo
    declara antes, nunca después") se cumple con este campo, no con una lista
    hardcodeada en el cliente.
  - `ready` lleva el `VersionSet` aplicado (`app_version`/`engine_digest`/
    `companion_digest`), **nunca** `endpoint_ref` ni el vale: el vale vive y
    muere dentro de Rust (`boot.rs::navigate_to_ticket` llama
    `window.navigate` directamente) — la ventana no necesita saberlo para
    pintar la transición a «Listo».
  - `failed` **no** lleva `stage`: `FailureCause` (data-model.md) es
    deliberadamente agnóstica de etapa (una violación de `preflight` no tiene
    ninguna etapa activa todavía). El cliente deriva la etapa que muestra en
    «Detalles» de la última `stage` que vio activa; puede ser indefinida.
  - `code` es el vocabulario cerrado de §3 (21 valores) **más dos extensiones
    del envoltorio**, sintetizadas fuera del CLI y nunca presentes en su
    NDJSON: `cancelled_by_owner` (SIGINT/cancelación honrada antes del punto
    de no retorno) y `cli_porcelain_unsupported` (el binario embebido no
    habla `--porcelain`/`facts --json` todavía, o violó una invariante del
    protocolo). Ambas son parte del vocabulario **cerrado** de este canal
    igualmente — un `FailureCode` fuera de las 23 sigue siendo un fallo de
    contrato, nunca una razón para que la pantalla se caiga.
  - No existe un `kind: 'facts'` en este canal — `facts` es un resultado de
    observación puramente interno al envoltorio (`reconcile`); nunca cruza a
    la ventana.

- **`safent://reconnecting`** — `{ reason: 'token_missing' | 'engine_restarted' }`,
  sin discriminante propio (el nombre del canal ya lo es). FR-012: sustituye
  a cualquier navegación cuando `up` no entrega vale, o cuando la bandeja
  pide «Reiniciar el motor» — cero peticiones en bucle.

Comandos Tauri que la ventana local invoca (capabilities/default.json):
`cancel_bootstrap`, `retry_bootstrap` — exactamente esos nombres, sin prefijo.
