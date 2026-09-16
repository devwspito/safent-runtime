# Verificación del paquete Linux arm64 (run 34523410889)

Ejecutada el 10-sep-2026 en la DGX (Ubuntu 24.04, `aarch64`, kernel 6.17), sin
sesión gráfica, sobre el artefacto **firmado** `Safent-Linux-arm64` del run
`34523410889` de `devwspito/agents-autonomy`. Objetivo: demostrar que el
**binario empaquetado** más el **podman empaquetado** arrancan el motor sin
pasos manuales. Nada se instaló en el sistema; nada se subió.

## 0. Qué trae el artefacto

```sh
gh run download -R devwspito/agents-autonomy 34523410889 -n Safent-Linux-arm64 -D <scratch>
```

| Fichero | Bytes |
|---|---|
| `appimage/Safent_0.1.0_aarch64.AppImage` | 102 939 144 |
| `deb/Safent_0.1.0_arm64.deb` | 30 489 706 |
| `rpm/Safent-0.1.0-1.aarch64.rpm` | 30 491 812 |

```sh
dpkg-deb -x deb/Safent_0.1.0_arm64.deb <scratch>/debroot      # NO se instala
./appimage/Safent_0.1.0_aarch64.AppImage --appimage-extract
```

Ambos dejan el mismo árbol: `usr/bin/safent-desktop` (5 392 720 B) y
`usr/lib/Safent/runtime/` con **16 ficheros aplanados** (los 15 del lock +
`pasta`, que llega como copia y no como enlace). Coincide con la fórmula de
`desktop/RUNTIME-BUNDLE.md` (`resource_dir()/runtime/<nombre>`, sin triple).

### Comprobación 1 — sha256 contra `desktop/runtime-manifest.lock`

Contra `targets["aarch64-unknown-linux-gnu"].entries` (15 entradas, comparando
por nombre de fichero porque el glob de Tauri aplana `bin/`, `libexec/podman/`
y `etc/containers/`):

| Paquete | Resultado |
|---|---|
| `.deb` | **PASA** — 15/15 sha256 y tamaño exactos |
| AppImage | **FALLA** — 6/15 no casan |

Los seis del AppImage: `crun` (+33 040 B), `fuse-overlayfs` (+63 256),
`fusermount3` (+63 992), `passt` (+62 432), `conmon` (+60 088), `catatonit`
(+39 920). `readelf -d` sobre el `crun` del AppImage muestra
`RUNPATH: [$ORIGIN]`: el empaquetador (linuxdeploy/patchelf) **reescribe** los
ELF del runtime. `podman`, `netavark`, `aardvark-dns` y `rootlessport` salen
intactos. Consecuencia: el AppImage nace incumpliendo el invariante
`RuntimeBundle` («la app re-verifica por sha256 antes de ejecutar»,
`stage-runtime.sh` §cabecera); el día que esa verificación se active, el
AppImage falla `runtime_hash_mismatch` de fábrica.

### Comprobación 2 — el CLI `safent` embebido: **FALLA (bloqueante)**

No está. Ni en el `.deb`, ni en el AppImage, ni en el `.rpm`. Todo lo que casa
con `*safent*` en el `.deb` es `usr/bin/safent-desktop` y tres iconos.

`boot.rs:748-750` resuelve el CLI como `runtime_dir.join("safent")`, es decir
`/usr/lib/Safent/runtime/safent`, y `stage-runtime.sh` (único que escribe ese
árbol) no stagea el CLI en ningún momento. **La app empaquetada no puede
invocar ningún verbo del contrato `app-engine`**: cualquier arranque real
muere en el primer `facts --json` por «no such file or directory».

## 1. Selftest sin pantalla — pasada 0 (tal y como está documentado)

Entorno (`contracts/app-engine.md` §1 + `boot::desired_state_from_env`), estado
y nombres **propios de esta sesión**, `SAFENT_PODMAN_PATH` apuntando al podman
**empaquetado** (el envoltorio lo reexporta como `SAFENT_PODMAN` al hijo,
`engine_adapter.rs:94`):

```sh
export SAFENT_NAME=pkg-selftest-1
export SAFENT_DATA_VOLUME=pkg-selftest-1-data
export SAFENT_STATE_HOME=<scratch>/state
export SAFENT_COMPANION_STATE=<scratch>/state/companions/ads
export SAFENT_RUNTIME_DIR=<scratch>/debroot/usr/lib/Safent/runtime
export SAFENT_PODMAN_PATH=$SAFENT_RUNTIME_DIR/podman
export SAFENT_CLI_PATH=<repo>/safent                    # DESVIACIÓN, ver §0.2
export SAFENT_ENGINE_IMAGE_REPO=localhost/safent-runtime
export SAFENT_ENGINE_DIGEST=sha256:b09654e3f32407b21972bc736a95483a58824e46bce9f7270161679d768f3408
unset DISPLAY
timeout 90 <scratch>/debroot/usr/bin/safent-desktop --selftest
```

**FALLA (bloqueante): bucle infinito en `runtime_staging`.** 798 líneas NDJSON
en 90 s — 399 ciclos idénticos, ni un `failed`, ni un `ready`, ni una espera
entre vueltas; lo matamos nosotros (`EXIT=124`):

```
{"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
{"kind":"done","stage":"runtime_staging","ms":0}
   … x399 …
```

Cadena exacta, leída en el código y confirmada en vivo:

1. `cmd_stage_runtime` (`safent:1387-1396`) es un **no-op** cuando no hay
   `runtime-bundle.json` junto al CLI — y ese manifiesto **no existe** ni en el
   paquete ni en el repo; por tanto nunca escribe `.verified`.
2. `cmd_facts` (`safent:1359-1362`) deriva `runtimeStaged`/`runtimeHashOk` de
   `$SAFENT_STATE_HOME/runtime/*/.verified` → siempre `false`.
3. `reconcile::reconcile` (`reconcile.rs:74-75`) devuelve otra vez
   `StageRuntime`.
4. `BootService::run` (`boot.rs:148`) es un `loop` **sin tope de vueltas y sin
   detección de «reparación aplicada que no cambia los hechos»**: el guardián
   `NoProgressDetected` (`boot.rs:331`) solo salta desde `handle_failure`, es
   decir **solo ante fallos**. Un `Ok(Progressed)` estéril gira para siempre,
   a ~4,4 vueltas/s y dos procesos por vuelta.

Para el dueño esto es la ventana clavada en «Preparando la base de ejecución»
sin error, sin final y sin botón: es el peor final posible de los tres
(bloqueo silencioso), y hoy es el comportamiento **por defecto** del paquete.

## 2. Pasada 1 — desbloqueado el marcador, podman EMPAQUETADO

Desviación declarada: `mkdir -p $SAFENT_STATE_HOME/runtime/6.1.1 && : >
…/.verified` a mano, para poder ver qué hay **detrás** del bucle.

**FALLA.** 2 s, `EXIT=1`, transcripción completa:

```
{"kind":"stage","stage":"pull_engine","label":"Descargando Safent","total_bytes":null,"point_of_no_return":false}
{"kind":"stage","stage":"pull_engine","label":"Descargando Safent","total_bytes":null,"point_of_no_return":false}
{"kind":"failed","code":"registry_unreachable","detail":"No se pudo descargar la imagen","retryable":true}
```

Causa real, reproducida directamente con el binario del paquete:

```sh
$ <runtime>/podman image exists localhost/safent-runtime:latest
Error: failed to open 2048 locks in /libpod_rootless_lock_1000: numerical result out of range   # rc=125
```

**El podman empaquetado no ejecuta ni un solo verbo en este equipo.** No es
userns: `kernel.unprivileged_userns_clone=1`,
`kernel.apparmor_restrict_unprivileged_userns=1`,
`/etc/subuid`+`/etc/subgid` con 100000:65536 para el usuario, y el podman del
sistema corre rootless sin problema. Es `ERANGE` de `open_lock_shm`:

- `/dev/shm/libpod_rootless_lock_1000` ya existe, **98 880 B**, creado el 9-sep
  por el podman del sistema (4.9.3, glibc).
- El podman empaquetado es **estático musl** (`file`: `statically linked`;
  `strings | grep -ci musl` = 0 por estar *stripped*, pero el origen es
  `mgoltzsche/podman-static`), y en musl `pthread_mutex_t` ocupa 40 B frente a
  los 48 B de glibc → para 2048 cerraduras calcula un segmento de otro tamaño
  y rechaza el existente.
- El nombre del segmento es **fijo por UID** (`/libpod_rootless_lock_<uid>`), no
  configurable.

O sea: **el podman empaquetado no puede convivir con un podman de distro para
el mismo usuario**, que es exactamente el escenario de adopción que
`quickstart.md` declara canónico («tiene podman 5 en `/opt/podman/bin`… es el
caso de adopción real, no un equipo limpio»). En un equipo limpio no habría
colisión; en el equipo del dueño, sí.

Agravante: al dueño se le cuenta como `registry_unreachable` / «No se pudo
descargar la imagen» — diagnóstico **engañoso**, y el stderr real del CLI llega
al usuario con **0 bytes** (el envoltorio lo captura y no lo emite). Nadie
puede deducir de la pantalla que el problema es la cerradura de /dev/shm.

## 3. Pasada 2 — mismo binario empaquetado, podman del sistema (DESVIACIÓN)

Sustituido `SAFENT_PODMAN_PATH=/usr/bin/podman` (4.9.3) para poder medir el
resto de la cadena. Todo lo demás idéntico.

**PASA (con las dos desviaciones de §0.2 y §3).** 12 228 ms de reloj,
`EXIT=0`:

```
{"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
{"kind":"done","stage":"container","ms":1000}
{"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
{"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
{"kind":"progress","stage":"health","done":1,"total":48,"unit":"steps"}
{"kind":"done","stage":"health","ms":11000}
{"kind":"ready","app_version":"0.1.0","engine_digest":"sha256:b09654e3f32407b21972bc736a95483a58824e46bce9f7270161679d768f3408","companion_digest":null}
```

- **Digest del motor: el local.** `sha256:b09654e3f324…f3408` =
  `localhost/safent-runtime:latest`. No hubo etapa `pull_engine`: `cmd_facts`
  reportó `localEngineImageDigest` y `reconcile` saltó directo a crear el
  contenedor. Cero red.
- Duración por etapa: `container` 1 000 ms · `health` 11 000 ms. La resolución
  de `ms` es de **segundo entero** (el CLI mide en segundos), no de milisegundo.
- Puerto elegido solo: `127.0.0.1:40647->7517/tcp`. Nunca aparece en el NDJSON.

## 4. Pasada 3 — SIGKILL al contenedor y reintento

```sh
podman kill -s KILL pkg-selftest-1     # -> Exited (137)
<binario empaquetado> --selftest        # sin ningún paso manual
```

**PASA.** 7 366 ms, `EXIT=0`, vuelve a `ready` con el **mismo digest**:

```
{"kind":"stage","stage":"container",...,"point_of_no_return":true}
{"kind":"done","stage":"container","ms":1000}
{"kind":"stage","stage":"health",...}
{"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
{"kind":"done","stage":"health","ms":6000}
{"kind":"ready","app_version":"0.1.0","engine_digest":"sha256:b09654e3f324…f3408","companion_digest":null}
```

La auto-sanación de `quickstart.md` §5 («`podman stop safent` desde fuera y
abrir la app → lo levanta sola») se cumple, y con SIGKILL, que es más duro.

## 5. Resumen PASA/FALLA

| # | Comprobación | Veredicto |
|---|---|---|
| 1 | Descarga y contenido del artefacto (deb + AppImage + rpm) | PASA |
| 2 | sha256 del runtime del **.deb** contra `runtime-manifest.lock` | PASA (15/15) |
| 3 | sha256 del runtime del **AppImage** contra el lock | **FALLA** (6/15) |
| 4 | CLI `safent` embebido presente en el paquete | **FALLA** (no existe) |
| 5 | El binario empaquetado arranca sin `DISPLAY` y habla el canal NDJSON | PASA |
| 6 | Selftest tal cual, con el podman empaquetado | **FALLA** (bucle infinito) |
| 7 | El podman empaquetado ejecuta algo en este equipo | **FALLA** (ERANGE /dev/shm) |
| 8 | Selftest hasta `ready` con el digest local (con desviaciones) | PASA |
| 9 | SIGKILL → `ready` otra vez, sin pasos manuales | PASA |

## 6. Otros hallazgos encontrados por el camino

- **Ayudantes fuera de la ruta de búsqueda de podman.** `conmon`, `netavark`,
  `aardvark-dns`, `rootlessport` y `catatonit` quedan aplanados junto a
  `podman` (`/usr/lib/Safent/runtime/`), que **no** es ninguna de las rutas
  que podman busca (`…/libexec/podman`), y ni el envoltorio
  (`engine_adapter.rs:88-101`) ni el CLI fijan `CONTAINERS_CONF` o
  `helper_binaries_dir`. En esta máquina lo tapa el
  `~/.config/containers/containers.conf` del usuario, que apunta a los del
  **sistema** — justo lo que `app-engine.md` §1 prohíbe («nunca el del PATH del
  usuario»). En un equipo limpio no hay nada que lo tape. No medido en vivo:
  el fallo §2 impide llegar ahí.
- **Colisión de volumen entre instancias.** `safent:35` fija
  `DATA_VOLUME="${SAFENT_DATA_VOLUME:-safent-data}"`, que **no** deriva de
  `NAME` (a diferencia de `run-safent.sh:110`, `VOLUME="${NAME}-data"`), y el
  envoltorio nunca exporta `SAFENT_DATA_VOLUME`. Cualquier instancia con
  `SAFENT_NAME` propio monta el volumen ajeno `safent-data` — que en esta
  máquina existe. Aquí se fijó a mano `pkg-selftest-1-data`.
- **«Sin compañero» no lo es en el CLI.** `--selftest` (sin `=companion`) deja
  `companion_image = None`, pero `EmbeddedCliDriver` mapea
  `StartContainer → up` **sin** `--no-companion`
  (`engine_adapter.rs:357-360`), y `_provision_companion` (`safent:220-242`)
  solo se salta con esa bandera; con `SAFENT_ADS_IMAGE` vacío,
  `run-safent.sh:148` llega a adoptar `safent-ads:local` por su cuenta. En esta
  máquina eso habría tocado la pila `safent-ads-*` de otra sesión, en la red
  compartida `safent-companions`. **Contenido a propósito** dejando
  `$SAFENT_STATE_HOME/companions/ads/bin` en modo `0500`, de modo que
  `_fetch_companion_file` falla y el CLI toma su propia degradación declarada
  (FR-3, «arranca sin compañero»). Verificado después: el contenedor de prueba
  salió con `nets=` vacío y solo el volumen propio.
- **`point_of_no_return: true` desde la primera etapa visible.** En esta ruta
  (`container`, `health`) no hay ni una etapa cancelable, así que el «Cancelar»
  de `quickstart.md` §2 no existe en ningún momento del arranque normal.

## 7. Desviaciones respecto al guion

1. `SAFENT_CLI_PATH` apuntando al `safent` del repo — obligado: el paquete no
   lleva CLI (§0.2).
2. `.verified` creado a mano en la pasada 1 — obligado para ver algo detrás del
   bucle infinito (§1).
3. `SAFENT_PODMAN_PATH=/usr/bin/podman` en las pasadas 2 y 3 — el podman
   empaquetado no arranca en este equipo (§2). Las pasadas 2 y 3 validan el
   **binario empaquetado**, no el **podman empaquetado**.
4. Nombres y estado propios (`pkg-selftest-1`, `pkg-selftest-1-data`,
   `SAFENT_STATE_HOME` en el scratch). El almacén de podman compartido se usó
   tal cual para no reescribir nada del equipo; no se tocó ningún contenedor,
   volumen ni red ajenos.

## 8. Desmontaje

```sh
podman rm -f pkg-selftest-1
podman volume rm pkg-selftest-1-data
rm -rf <scratch>/state <scratch>/store <scratch>/runroot
```

Sin residuos (`podman ps -a --filter name=pkg-selftest` → 0,
`podman volume ls | grep pkg-selftest` → 0). `safent-demo` y la pila
`safent-ads-*` de otras sesiones siguen en pie con su uptime original. Nada se
instaló en el sistema, nada se escribió en `~/.safent`, no se usó `tailscale`,
no se hizo `push`.
