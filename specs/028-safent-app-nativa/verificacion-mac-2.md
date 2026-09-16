# Verificación 2 — app nativa de macOS en el MacBook del dueño

**Qué**: segunda prueba real del artefacto notarizado `Safent-macOS-arm64` del run
**34549128789** (`devwspito/agents-autonomy`, job «Build (macOS-arm64)» en verde;
el run figura en rojo por «Build (Linux-x64)», ajeno a esto), versión 0.9.0,
construido desde `safent-runtime@feat/safent-next` en `c7fea04`, notarizado y
grapado (envío `34961932-3e9d-4250-9ddc-68edf81ceae9`).
**Dónde**: MacBook Air del dueño (`macbook-air-2-1`), macOS 15.5 (24F74), arm64,
24 GB de RAM, 83 GB libres. Por SSH, **sin sesión gráfica**.
**Cuándo**: 11-sep-2026, 03:20–03:45 (hora local del Mac). **Quién**: exploratory-tester.
**Alcance**: recorrido headless (`--selftest`) del binario empaquetado hasta
`ready`, reanudación tras matar el contenedor, y comprobación de que el producto
responde de verdad. Sin navegador, sin ventana, sin GUI.

## Veredicto

**FALLA en el primer arranque; PASA en caliente.** Los seis hallazgos de la
verificación 1 (MAC-01…MAC-06) están **arreglados y verificados en vivo**: el
paquete se stagea entero (15/15), la máquina se crea con nombre propio, con la
imagen empaquetada, con `--provider applehv` y **sin tocar la del dueño**, y el
digest del motor sale del propio paquete. Pero el recorrido del dueño —doble clic
con el equipo limpio— **sigue sin llegar al producto**, por tres defectos nuevos
encadenados, y cuando sí llega a `ready`, **el vale apunta a un puerto que el
host no puede abrir**.

| Bloque | Resultado |
|---|---|
| Descarga del artefacto e integridad del DMG | **PASA** |
| Grapado del DMG (`stapler validate`) | **PASA** |
| Firma (`codesign --verify --strict`) y notarización (`spctl -a -vv -t exec`) | **PASA** |
| Grapado del `.app` extraído | **FALLA** (MAC2-12, menor) |
| 15 entradas del runtime contra `runtime-bundle.json` | **PASA** (15/15 sha256) |
| Vía `cdhash` de MAC-02 | **NO EJERCITADA** (MAC2-08: los 15 `cdhash` son `null`) |
| Digest del motor y del compañero fijados en el paquete | **PASA** |
| `runtime_staging` desde el `.app` firmado | **PASA** (1,0 s, 15/15) |
| `machine` (nombre propio, `--image` empaquetada, applehv, sin red) | **PASA** (20 s) |
| `pull_engine` (etapa `ensure-images`) | **NUNCA SE EJECUTA** (MAC2-01) |
| Primer arranque completo hasta `ready` | **FALLA** (MAC2-02, reproducido 2/2) |
| Arranque con la imagen ya descargada | **PASA** (`ready` en 6,9 s) |
| Reanudación tras `podman kill` | **PASA** (`ready` en 6,8 s, sin intervención) |
| El producto responde en el puerto publicado | **FALLA** (MAC2-05, bloqueante) |
| Reapertura con el motor ya vivo | **FALLA** (MAC2-06: lo destruye y lo recrea) |
| Convivencia con el podman del dueño | **PASA en el nombre**, **FALLA en el emplazamiento** (MAC2-14) |
| Desmontaje completo | **PASA** |

## Reglas de la prueba y arnés prestado

Todo ocurrió bajo `/tmp/safent-mac-test2/` con
`SAFENT_STATE_HOME=/tmp/safent-mac-test2/state` (luego `/private/tmp/...`, ver
MAC2-07) y `SAFENT_NAME=mactest2` → contenedor `mactest2`, volumen
`mactest2-data`, **máquina `mactest2-engine`** (`MACHINE_NAME="${NAME}-engine"`,
`safent:47`).

Igual que en la verificación 1, se añadió **desde fuera** un arnés que el producto
**no tiene**: `XDG_CONFIG_HOME`, `XDG_DATA_HOME` y `TMPDIR` apuntando al
sandbox. Sin él, la máquina propia se crearía **dentro del árbol de podman del
dueño** y sus sockets convivirían con los suyos en el mismo `$TMPDIR/podman`
(MAC2-14). Comprobado antes de empezar que el arnés aísla de verdad:

```
$RT machine list                       -> (vacío)
$RT machine info                       -> MachineConfigDir /tmp/safent-mac-test2/xdg/config/containers/podman/machine/libkrun
                                          MachineImageDir  /tmp/safent-mac-test2/xdg/data/containers/podman/machine/libkrun
```

Nunca se ejecutó `tailscale`. Nunca se tocó `/opt/podman`, `~/.config/containers`,
`~/.local/share/containers`, `~/.ssh`, `/Applications` ni `podman-machine-default`.
No se abrió ninguna app gráfica.

---

## Paso 1 — Descarga, traslado y verificación del paquete

### 1.1 Descarga (DGX) — **PASA**

`gh run download` sigue rechazando este artefacto (MAC-09 de la verificación 1),
así que se bajó por API, como manda el encargo:

```
gh api repos/devwspito/agents-autonomy/actions/artifacts/10180322412/zip > Safent-macOS-arm64.zip
   -> 2 013 124 927 B   sha256 ef70be5b1403ce0c9447d546fac10880b363d4d69655f32c70ec8b0a8ec97533
unzip -l:
   1 007 234 321  macos/Safent.app.tar.gz          <- vía del actualizador
   1 006 928 239  dmg/Safent_0.9.0_aarch64.dmg     <- vía de instalación
             404  macos/Safent.app.tar.gz.sig
```

El artefacto ha **doblado de tamaño** respecto al de la verificación 1 porque
ahora lleva también el `.tar.gz` del actualizador. El DMG por sí solo
(1 006 928 239 B) sigue dentro de la horquilla del lock y muy por debajo del tope
de 2 GiB de GitHub Releases; **el artefacto de CI (2,01 GB) no**, si alguna vez se
publicase entero como un único fichero.

### 1.2 Traslado — **PASA**

```
scp Safent_0.9.0_aarch64.dmg luiscorrea@macbook-air-2-1:/tmp/safent-mac-test2/   (7,8 s)
sha256 en los dos extremos: b6aa36c793179d93eaeb05651dd8861a24b85609ce31b2e2054d8933ac1de45e   IDÉNTICO
```

### 1.3 Grapado, firma y notarización — **PASA** (con una salvedad)

```
xcrun stapler validate Safent_0.9.0_aarch64.dmg
  -> The validate action worked!                                  rc=0
spctl -a -vv -t open --context context:primary-signature <dmg>
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)     rc=0
codesign --verify --strict --verbose=2 <dmg>
  -> valid on disk · satisfies its Designated Requirement          rc=0
xattr -l <dmg>                                                     -> (vacío, sin cuarentena)

hdiutil attach -nobrowse -readonly -mountpoint /tmp/safent-mac-test2/mnt <dmg>
  -> /dev/disk4s1 montado; contenido: Safent.app, Applications -> /Applications, .VolumeIcon.icns
ditto mnt/Safent.app /tmp/safent-mac-test2/Safent.app              (1,0 GB, 1,0 s)
hdiutil detach /tmp/safent-mac-test2/mnt                           -> "disk4" ejected

spctl -a -vv -t exec Safent.app
  -> accepted · source=Notarized Developer ID                      rc=0
codesign --verify --strict --verbose=2 Safent.app
  -> valid on disk · satisfies its Designated Requirement          rc=0
codesign --verify --deep --strict --verbose=2 Safent.app           rc=0
codesign -dvvv Safent.app
  -> Identifier=com.safent.desktop · Format=app bundle with Mach-O thin (arm64)
     CodeDirectory v=20500 flags=0x10000(runtime) hashes=1045+3
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Timestamp=11 Sep 2026 at 03:07:35 · TeamIdentifier=JBMBA58A8X · Runtime Version=14.5.0

xcrun stapler validate Safent.app
  -> Safent.app does not have a ticket stapled to it.              rc=65     <- MAC2-12
```

### 1.4 `runtime-bundle.json` y las 15 entradas — **PASA (15/15 sha256)**

```
podman_version : 6.1.1
entries        : 15
engine_image   : ghcr.io/devwspito/safent    @ sha256:62d459e3…953549   platform linux/arm64
companion_image: ghcr.io/devwspito/safent-ads@ sha256:54c4fee8…1264b9   platform linux/arm64
```

Contrastado contra el registro (público): `v0.9.0-rc1` **resuelve exactamente** a
`sha256:62d459e3…953549` (manifiesto OCI de una sola plataforma, `arm64/linux`,
72 capas, 2 705 199 203 B comprimidos); el digest del compañero es **el hijo
`linux/arm64`** del índice de `safent-ads:v0.2.0` (`sha256:0d8bdd39…`), o sea,
fijado por plataforma y no por índice. Correcto.

Verificación fichero a fichero **dentro del paquete firmado**:

| Fichero | manifiesto (sha256) | real | cdhash manifiesto | `codesign --verify` | veredicto |
|---|---|---|---|---|---|
| provision.sh | eeef14235363da | = | null | n/a | OK |
| caps.template.yaml | 1de55a12095a22 | = | null | n/a | OK |
| podman | 5b22fd88370db1 | = | **null** | rc=0 (CDHash ea3733d4…) | OK |
| vfkit | c94e5e10555e52 | = | **null** | rc=0 (CDHash b2817b0b…) | OK |
| krunkit | ed02aa7af37031 | = | **null** | rc=0 (CDHash d039e2e7…) | OK |
| gvproxy | 5f9251768df2b5 | = | **null** | rc=0 (CDHash e946b4d0…) | OK |
| podman-machine.aarch64.applehv.raw.zst | b71b8a4e95a440 | = | null | n/a | OK |
| libkrun.dylib | 528bec4bfdb79b | = | **null** | rc=0 (CDHash aed38420…) | OK |
| libepoxy.0.dylib | 0b1c005f4cc413 | = | **null** | rc=0 (CDHash c4b2c942…) | OK |
| libMoltenVK.dylib | d34e8062b674a9 | = | **null** | rc=0 (CDHash 70a4304c…) | OK |
| libvirglrenderer.1.dylib | 6e6a37ff505d4b | = | **null** | rc=0 (CDHash 92b22cda…) | OK |
| compose.yaml | 502fd5789a8786 | = | null | n/a | OK |
| run-safent.sh | b2a2b45624ac67 | = | null | n/a | OK |
| safent | 071ff10a3bd999 | = | null | n/a | OK |
| KRUN_EFI.silent.fd | 9ba725c245f634 | = | null | n/a | OK |

**15/15 casan** — incluidos los 8 Mach-O que en la verificación 1 fallaban todos.
El pipeline ahora graba el sha256 **después** de firmar: MAC-02 está arreglado
**de hecho**. Pero lo está por la vía del sha256, no por la vía documentada:
los 15 `cdhash` viajan a `null`, así que la rama `codesign --verify --strict` +
comparación de cdhash de `cmd_stage_runtime` (`safent:1777-1791`) **no se ejecuta
nunca** en este paquete (MAC2-08).

`Contents/Resources/runtime/` trae 17 ficheros (los 15 del manifiesto +
`runtime-bundle.json` + nada más): **sigue sin `containers.conf`** (MAC2-13,
era MAC-07).

---

## Paso 2 — Selftest headless

Binario: `/tmp/safent-mac-test2/Safent.app/Contents/MacOS/safent-desktop --selftest`
(sin compañero). **Sin `SAFENT_ENGINE_DIGEST` ni `SAFENT_RUNTIME_DIR`**: se exige
que el paquete se baste solo (MAC-03/MAC-04). Arrancado con `nohup`, NDJSON crudo
a fichero y copia con marca de tiempo.

### Pase 1 — equipo limpio, tal y como lo viviría el dueño (**FALLA**)

```
{"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}   +2,1 s
{"kind":"progress","stage":"runtime_staging","done":1,"total":15,"unit":"steps"}     +2,1 s
{"kind":"progress","stage":"runtime_staging","done":2,"total":15,"unit":"steps"}     +2,1 s
{"kind":"progress","stage":"runtime_staging","done":3,"total":15,"unit":"steps"}     +2,2 s
{"kind":"progress","stage":"runtime_staging","done":4,"total":15,"unit":"steps"}     +2,2 s
{"kind":"progress","stage":"runtime_staging","done":5,"total":15,"unit":"steps"}     +2,3 s
{"kind":"progress","stage":"runtime_staging","done":6,"total":15,"unit":"steps"}     +2,3 s
{"kind":"progress","stage":"runtime_staging","done":7,"total":15,"unit":"steps"}     +3,4 s
{"kind":"progress","stage":"runtime_staging","done":8,"total":15,"unit":"steps"}     +3,4 s
{"kind":"progress","stage":"runtime_staging","done":9,"total":15,"unit":"steps"}     +3,4 s
{"kind":"progress","stage":"runtime_staging","done":10,"total":15,"unit":"steps"}    +3,5 s
{"kind":"progress","stage":"runtime_staging","done":11,"total":15,"unit":"steps"}    +3,5 s
{"kind":"progress","stage":"runtime_staging","done":12,"total":15,"unit":"steps"}    +3,5 s
{"kind":"progress","stage":"runtime_staging","done":13,"total":15,"unit":"steps"}    +3,5 s
{"kind":"progress","stage":"runtime_staging","done":14,"total":15,"unit":"steps"}    +3,5 s
{"kind":"progress","stage":"runtime_staging","done":15,"total":15,"unit":"steps"}    +3,5 s
{"kind":"done","stage":"runtime_staging","ms":1000}                                  +3,5 s
{"kind":"stage","stage":"machine","label":"Preparando la maquina","total_bytes":null,"point_of_no_return":false}   +5,6 s
{"kind":"done","stage":"machine","ms":20000}                                         +25,9 s
{"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}  +26,2 s
{"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}  +43,7 s
{"kind":"failed","code":"daemon_unhealthy","detail":"sin respuesta durante 15s","retryable":true}                  +58,7 s
```

Duración total 58,7 s. **No hay `ready`.** Etapas: `runtime_staging` 1,0 s ·
`machine` 20,0 s · `container` matada dos veces a los 15 s de silencio.
`pull_engine` **no aparece**.

Al terminar el proceso quedaron **vivos dos hijos huérfanos**:

```
pgrep -fl "Resources/runtime/podman"
25564 .../runtime/podman run --rm --entrypoint cat ghcr.io/devwspito/safent@sha256:62d459e3… /usr/share/hermes/seccomp/safent.json
25758 .../runtime/podman run --rm --entrypoint cat ghcr.io/devwspito/safent@sha256:62d459e3… /usr/share/hermes/seccomp/safent.json
```

…que **siguieron descargando los 2,7 GB** y los terminaron tres minutos después,
ya con el arranque dado por fallido (`podman images` → la imagen de 8,17 GB
presente, sin que la app lo supiera).

### Pase 2 — con la imagen ya descargada (**FALLA por otra causa**)

```
{"kind":"stage","stage":"container",…,"point_of_no_return":true}    +0,5 s
{"kind":"stage","stage":"container",…,"point_of_no_return":true}    +3,7 s
{"kind":"failed","code":"daemon_unhealthy","detail":"Error: opening seccomp profile failed: open /tmp/safent-mac-test2/state/safent-seccomp.json: no such file or directory","retryable":true}   +4,3 s
```
`rc=1`. El fichero **sí existe en el host** (19 766 B, escrito por
`_ensure_seccomp` un segundo antes): quien no lo ve es el podman **de dentro de la
VM**, porque `/tmp` del host no está montado ahí. Ver MAC2-07. Se repitió el pase
con la misma carpeta escrita en su forma canónica
(`SAFENT_STATE_HOME=/private/tmp/safent-mac-test2/state`, que sí cae bajo el
`virtio-fs` de `/private`).

### Pase 3 — todo caliente (**PASA**)

```
{"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}      +0,5 s
{"kind":"done","stage":"container","ms":1000}                                                                          +1,3 s
{"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}  +1,4 s
{"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}                                                +1,5 s
{"kind":"done","stage":"health","ms":5000}                                                                             +6,8 s
{"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:62d459e30c36132a0892cfc09e37f3d7e1e964b3a53bf8e96f4160a998953549","companion_digest":null}   +6,9 s
```
`rc=0`. **`ready` alcanzado en 6,9 s**, con el digest exacto que el paquete fija y
`companion_digest: null` (modo sin compañero, correcto).

### Pase 4 — el arranque en frío, reproducido a propósito (**FALLA, determinista**)

Se borraron la imagen (`podman rmi`), el contenedor y el perfil seccomp cacheado,
dejando la máquina viva:

```
{"kind":"stage","stage":"container",…,"point_of_no_return":true}     +0,3 s
{"kind":"stage","stage":"container",…,"point_of_no_return":true}     +17,8 s
{"kind":"failed","code":"daemon_unhealthy","detail":"sin respuesta durante 15s","retryable":true}   +32,9 s
```
`rc=1`, 33 s, otra vez **dos `podman run` huérfanos** vivos al salir, y otra vez
**sin etapa `pull_engine`**. Es el mismo fallo del pase 1: reproducible 2/2.

### Medición aparte — ¿sobreviviría `pull_engine` si llegara a ejecutarse?

Se invocó a mano el verbo que el envoltorio nunca llega a pedir, cronometrando
**cada** línea de stdout y de stderr:

```
safent ensure-images --porcelain
stdout:  +   0,0 s  {"t":"stage","id":"pull_engine","label":"Descargando Safent"}
         +  84,5 s  {"t":"progress","id":"pull_engine","done":1,"total":1,"unit":"layers"}
         +  84,5 s  {"t":"done","id":"pull_engine","ms":84000}
stderr:  77 líneas; mayor hueco sin UNA sola línea: 60,9 s
         (+23,5 s … +84,4 s: "Copying blob …" → "Copying config …")
rc=0, 84 s en total
```

Con `stall_timeout = 15 s` (`engine_adapter.rs:67`), ese hueco de 60,9 s mata la
etapa a los ~23 s. **La descarga del motor es incompatible con el vigía del
envoltorio por diseño**, se alcance por `ensure-images` o por `up`.

---

## Paso 3 — Reanudación tras matar el contenedor (**PASA**)

```
$RT kill mactest2                     -> mactest2         (Exited (137))
./run_selftest.sh 4                   -> sin tocar nada más
{"kind":"stage","stage":"container",…}          +0,4 s
{"kind":"done","stage":"container","ms":0}      +1,2 s
{"kind":"stage","stage":"health",…}             +1,2 s
{"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}   +1,3 s
{"kind":"done","stage":"health","ms":6000}      +6,7 s
{"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:62d459e3…953549","companion_digest":null}   +6,8 s
rc=0
```
Se recupera **sola**, en 6,8 s, sin preguntar nada. Este era el paso (3) del
encargo: **PASA**.

---

## Paso 4 — ¿Responde el producto? (**FALLA — MAC2-05**)

Con `ready` recién emitido y el contenedor arriba:

```
$RT ps            -> mactest2  Up 24 seconds  127.0.0.1:40643->7517/tcp
$RT exec mactest2 systemctl is-active hermes-runtime    -> active
$RT exec mactest2 python3 -c 'import hermes;print(hermes.__version__)'  -> 0.9.0

# desde el HOST (que es donde vive la ventana de la app):
curl -s -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:40643/     -> 000   (3 intentos)
lsof -nP -iTCP:40643 -> gvproxy 25436 … 127.0.0.1:40643 (LISTEN)
                        gvproxy 25436 … 127.0.0.1:40643->127.0.0.1:52534 (CLOSE_WAIT)

# desde DENTRO de la VM:
curl http://127.0.0.1:40643/       -> 307
curl http://192.168.127.2:40643/   -> 000
```

Contraprueba con una sonda idéntica publicando en todas las interfaces
(`-p 0.0.0.0::7517`, contenedor `probe0`, misma imagen y mismas banderas):
**también 000 desde el host** y **307 sólo por el loopback de la VM**. O sea, no
es la dirección de publicación: el camino `gvproxy → eth0 de la VM → DNAT a
10.88.0.9:7517` no completa (la respuesta vuelve sin enmascarar). Reglas
observadas dentro de la VM:

```
chain nv_2f259bab_10_88_0_0_nm16_dnat {
  ip saddr 10.88.0.0/16 tcp dport 40643 jump NETAVARK-HOSTPORT-SETMARK
  ip saddr 127.0.0.1    tcp dport 40643 jump NETAVARK-HOSTPORT-SETMARK
  tcp dport 40643 ct mark set meta mark | 0x00001000 dnat ip to 10.88.0.9:7517
}
```
(sólo el tráfico de loopback y del bridge recibe la marca que lo enmascara).

**Efecto**: `up` emite `ready` y escribe en el fd 3
`http://127.0.0.1:<puerto>/?k=<vale>`; el envoltorio navega a esa URL y **no hay
nadie al otro lado**. El dueño vería la ventana en blanco / «reconectando» con el
motor sano y el selftest diciendo que todo fue bien. **El selftest da por bueno
un arranque sin comprobar jamás que el vale sirve.**

---

## Paso 5 — Reapertura con el motor ya vivo (**FALLA — MAC2-06**)

```
pase 6 (motor parado)   -> ready +7,2 s;  contenedor 61c7f68c58b1  puerto 42441
pase 7 (motor VIVO y sano, sin tocar nada) ->
{"kind":"stage","stage":"container",…,"point_of_no_return":true}   +0,9 s
{"kind":"done","stage":"container","ms":6000}                      +7,5 s
{"kind":"stage","stage":"health",…}                                +7,5 s
{"kind":"done","stage":"health","ms":6000}                         +12,9 s
{"kind":"ready",…}                                                 +13,0 s
contenedor 090f5713fae5   puerto 43617      <- OTRO contenedor, OTRO puerto
```
Abrir la app con el motor ya funcionando **lo destruye (`podman rm -f`) y lo
levanta de cero**, 13 s, puerto nuevo. `quickstart.md` §4.2 pide ≤10 s y «no se
repite la preparación». Los datos sobreviven (volumen `mactest2-data`), el trabajo
en curso no.

---

## Datos observados de la máquina

```
$RT machine inspect mactest2-engine
  mactest2-engine   rootful=true  state=running  cpus=4  mem=8192 (MiB)  disk=60 (GiB)  provider applehv
cat state/machine.json   -> {"name":"mactest2-engine","adopted":false}
```
Nombre propio, `adopted:false` escrito por el CLI, imagen empaquetada
(`--image …podman-machine.aarch64.applehv.raw.zst`), `--provider applehv` → la
arranca **vfkit**, no krunkit. **La máquina del dueño (`podman-machine-default`,
libkrun) siguió arrancada e intacta todo el rato.** MAC-05 y MAC-06: arreglados.

Nota: `MachineSpec` de `boot.rs:852-868` sigue pidiendo 6 GiB y el CLI fija 8192
MiB a pelo; el envoltorio no decide el tamaño (sigue siendo letra muerta, y es
además la raíz de MAC2-01).

---

## Hallazgos

### MAC2-01 · BLOQUEANTE · la máquina propia se lee siempre «a la deriva», y eso se come la etapa de descarga

- **Evidencia**: `facts --json` real de este Mac (ejecutado a mano con el entorno
  exacto del envoltorio):
  `"machines":[{"name":"mactest2-engine","provider":"podman","rootful":true,"running":true,"ours":true}]`
  — sin `cpus`, sin `memoryBytes`, sin `osVersion`, y con `provider` literal
  `"podman"`.
- **Causa, en tres líneas concretas**:
  1. `safent:1652` (`_machines_json`) escribe `"provider":"podman"` **fijo** para
     cualquier máquina y no emite ni `cpus`, ni `memory_bytes`, ni `os_version`.
  2. `engine_adapter.rs:926-933` (`map_machine_provider`) mapea `"podman"` a
     `MachineProvider::Other("podman")`; `engine_adapter.rs:754-772` declara los
     tres campos ausentes como `#[serde(default)]` → `cpus=0`, `memory=0`,
     `os_version=""`.
  3. `domain.rs:162-168` (`is_satisfied_by`) exige `provider == AppleHv`,
     `cpus >= 4`, `memory >= 6 GiB` y `os_version == "6.1"` → **jamás se cumple**.
- **Consecuencia 1 (la grave)**: `reconcile.rs:112-123` (`machine_gap`) no
  encuentra máquina que sirva, cae a `find(|m| m.ours)` y, como la nuestra está
  viva, devuelve **`RecreateEngine`** («drift»), que `cli_invocation_for`
  (`engine_adapter.rs:389`) traduce a **`up`**. Es decir: mientras la máquina
  exista y esté arrancada, `images_gap` (`reconcile.rs:155`) **nunca se evalúa** y
  el verbo `ensure-images` **es inalcanzable**. Por eso no hay una sola etapa
  `pull_engine` en toda la prueba, ni en frío ni en caliente.
- **Consecuencia 2**: cada arranque es un `RecreateEngine` → MAC2-06.
- **Arreglo**: que `_machines_json` emita el proveedor real
  (`machine inspect --format '{{.VMType}}'` → `applehv`) y los tres campos que el
  contrato compara, **o** que `MachineSpec` deje de comparar lo que el CLI no
  publica. El vocabulario de `machines[]` hay que fijarlo en `data-model.md` con
  la misma tabla explícita que se hizo para `os` tras MAC-01; es exactamente el
  mismo tipo de desajuste, una capa más abajo.

### MAC2-02 · BLOQUEANTE · el primer arranque muere en `container` a los 15 s

- **Evidencia**: `{"kind":"failed","code":"daemon_unhealthy","detail":"sin respuesta durante 15s","retryable":true}`
  tras dos etapas `container` seguidas; pases 1 y 4, 2/2, con rc=1.
- **Causa**: al saltarse `pull_engine` (MAC2-01), los 2,7 GB se descargan
  **implícitamente dentro de `up`**: `_ensure_seccomp` (`safent:253`) hace
  `podman run --rm --entrypoint cat <imagen> /usr/share/hermes/seccomp/safent.json`,
  que tira de la imagen entera. Entre `_stage container` y `_stage_done`
  (`safent:1896-1898`) el CLI **no emite ni un `progress`**, así que el vigía de
  15 s del envoltorio (`engine_adapter.rs:67`, aplicado en `run_porcelain`
  líneas 548-551) mata al hijo. `EngineLifecycle` da un segundo intento y degrada.
- **Efecto en el dueño**: doble clic, ~33 s de «Creando el contenedor», y una
  pantalla de fallo con «Reintentar» que volverá a fallar igual, para siempre, en
  un equipo limpio. **El producto no arranca la primera vez.**
- **Agravante**: la etapa lleva `point_of_no_return: true`, así que durante esos
  2,7 GB de descarga **no se puede cancelar**.

### MAC2-03 · BLOQUEANTE · la descarga del motor no cabe en el vigía de 15 s

- **Evidencia**: medición del apartado anterior — `pull_engine` tarda **84 s**,
  emite **un solo** `progress` (al final) y pasa **60,9 s sin una línea** en
  ningún canal.
- **Choque**: `app-engine.md` §3.2 exige `progress` **al menos cada 5 s** mientras
  la etapa viva. `cmd_ensure_images` (`safent:1878-1882`) hace `"$RT" pull … >&2`
  y emite el progreso **después**.
- **Efecto**: aunque se arregle MAC2-01, la etapa de descarga morirá igual. Hay
  que narrar el pull de verdad (podman tiene `--quiet=false` con eventos por capa,
  o se le puede seguir el progreso por `podman events`/parseo de stderr) o subir
  el `stall_timeout` para esta etapa concreta — pero lo primero es lo que pide el
  contrato.

### MAC2-04 · BLOQUEANTE · el envoltorio mata al hijo y deja vivos a los nietos

- **Evidencia**: tras el `failed` del pase 1 y del pase 4, `pgrep` devuelve **dos**
  `podman run --rm --entrypoint cat …` vivos, descargando; terminaron minutos
  después, ya sin nadie escuchando.
- **Causa**: `kill_and_timeout` (`engine_adapter.rs:519` y `:550`) hace
  `child.kill()` sobre el proceso directo (`/bin/sh safent`), no sobre su grupo.
- **Efecto**: cada reintento lanza **otra** descarga de 2,7 GB en paralelo contra
  el mismo almacén; el equipo del dueño sigue consumiendo red y disco después de
  que la app se haya dado por vencida, y un `uninstall` inmediato competiría con
  ellos. Arreglo: `setsid`/grupo de proceso y `killpg`.

### MAC2-05 · BLOQUEANTE · el vale apunta a un puerto que el host no puede abrir

- **Evidencia**: apartado «Paso 4». Motor `active` dentro, `307` desde el loopback
  de la VM, **`000` desde el host** en las dos publicaciones probadas
  (`127.0.0.1::7517` y `0.0.0.0::7517`), con `gvproxy` escuchando en el host y la
  conexión muriendo en `CLOSE_WAIT`.
- **Efecto**: `ready` es **mentira útil**: el envoltorio navegaría a
  `http://127.0.0.1:<puerto>/?k=…` y no cargaría nada. Es el fallo que más se
  parece a «la app se abre en blanco» desde el punto de vista del dueño.
- **Segundo defecto en el mismo sitio**: el selftest —la única puerta headless que
  tiene el producto para verificarse— **no comprueba el vale**: `selftest.rs:150`
  se conforma con `LoopOutcome::Ready` y devuelve 0. Un humo que declara éxito sin
  tocar el puerto no puede cazar esto; debería hacer una petición al vale y exigir
  una respuesta HTTP antes de salir con 0.

### MAC2-06 · MAYOR · reabrir la app destruye y recrea el motor

- **Evidencia**: pases 6 y 7 — contenedor `61c7f68c58b1`/puerto 42441 →
  `090f5713fae5`/puerto 43617, 13,0 s, con el motor sano y sin que nadie lo pida.
- **Causa**: la deriva permanente de MAC2-01 → `RecreateEngine` → `up` → `_run`
  hace `"$RT" rm -f "$NAME"` (`safent:347`) antes de volver a crear.
- **Choque**: `quickstart.md` §4.2 («≤10 s y no se repite la preparación») y §5
  (auto-sanación sin destruir lo que ya funciona). Además el puerto cambia en cada
  arranque, lo que invalida cualquier `publishedPort` recordado.

### MAC2-07 · MAYOR · el perfil seccomp viaja como ruta del host a un podman remoto

- **Evidencia**: `{"kind":"failed","code":"daemon_unhealthy","detail":"Error: opening seccomp profile failed: open /tmp/safent-mac-test2/state/safent-seccomp.json: no such file or directory"}`
  con el fichero **existiendo** en el host (19 766 B).
- **Causa**: `_run` (`safent:364`) pasa `--security-opt seccomp="$SECCOMP"`, con
  `SECCOMP="$SAFENT_STATE_HOME/safent-seccomp.json"` (`safent:69-70`). En macOS el
  que abre ese fichero es el podman **de dentro de la VM**, que sólo ve
  `/Users`, `/private` y `/var/folders` (los `virtio-fs` que monta `machine init`).
- **Efecto**: funciona con el `~/.safent` por defecto **por casualidad** (cae bajo
  `/Users`). Cualquier `SAFENT_STATE_HOME` fuera de esos tres árboles —un disco
  externo, `/opt`, `/tmp` sin canonizar— rompe el arranque con un error crudo de
  podman travestido de `daemon_unhealthy`. Arreglo: copiar el perfil **dentro** de
  la máquina (o del volumen) y referenciarlo con una ruta de la VM, o declarar y
  validar la restricción antes de llegar a `podman run`.
- **Nota de la prueba**: esto obligó a reescribir `SAFENT_STATE_HOME` como
  `/private/tmp/safent-mac-test2/state` — **la misma carpeta**, en su forma
  canónica, siempre dentro del sandbox exigido.

### MAC2-08 · MENOR · la vía `cdhash` es código muerto en este paquete

- **Evidencia**: los 15 `cdhash` de `runtime-bundle.json` son `null`; los 8 Mach-O
  pasan `codesign --verify --strict` (rc=0) y tienen CDHash real, pero nadie lo
  compara. La verificación real la hace el sha256, que casa 15/15 porque el
  pipeline lo graba tras firmar.
- **Riesgo**: `RUNTIME-BUNDLE.md` describe `--refresh-bundle-json` como el paso que
  rellena los `cdhash` **después** de firmar; ese paso o no se ejecutó o no
  escribió nada. Mientras el sha256 se capture post-firma no pasa nada; el día que
  vuelva a capturarse antes, MAC-02 renace y esta red de seguridad seguirá sin
  existir.

### MAC2-09 · MENOR · una etapa abierta dos veces sin cerrarse

- **Evidencia**: pases 1 y 4 — dos `{"kind":"stage","stage":"container"}` seguidos,
  sin `done` ni `failed` entre medias.
- **Choque**: `app-engine.md` §3 invariante 3 («cada `stage` cierra con exactamente
  un `done` o un `failed`»). La ventana que pinte esto ve la etapa reiniciarse sin
  explicación; un cronómetro por etapa mide mal.

### MAC2-10 · MENOR · un timeout del envoltorio no es `daemon_unhealthy`

- `"sin respuesta durante 15s"` se emite con `code:"daemon_unhealthy"` y
  `retryable:true`. No hay ningún demonio implicado: el envoltorio mató al CLI.
  El vocabulario cerrado de `app-engine.md` §3 no tiene un código para «mi hijo se
  quedó mudo»; hoy se disfraza del que menos se le parece, y el `retryable:true`
  invita a un reintento que fallará idéntico (lo hace: dos veces, siempre).

### MAC2-11 · MENOR · no existe la etapa «comprobar el equipo»

- El primer evento de todo arranque es `runtime_staging`. `preflight` está en el
  vocabulario (`app-engine.md` §3) y `quickstart.md` §2 lo pide nombrado
  («comprobar el equipo → preparar la base de ejecución → …»), pero nadie lo
  emite: los 2,1 s iniciales son pantalla muda.

### MAC2-12 · MENOR · el `.app` no lleva ticket grapado

- `xcrun stapler validate Safent.app` → rc=65, «does not have a ticket stapled to
  it». Sólo el DMG está grapado. `spctl` lo acepta aquí porque el ticket ya estaba
  en la caché local tras validar el DMG. Importa para la **vía del actualizador**
  (`macos/Safent.app.tar.gz`, que este mismo artefacto publica): el `.app` que
  instale el updater no llevará ticket y Gatekeeper necesitará red para validarlo.

### MAC2-13 · MENOR · sigue sin `containers.conf` en el bundle de macOS

- Los 17 ficheros de `Contents/Resources/runtime/` listados; ninguno es
  `containers.conf`, así que `safent:145` no fija `CONTAINERS_CONF` y el podman
  empaquetado cae en la configuración del sistema. Era MAC-07; sigue igual.

### MAC2-14 · MENOR (convivencia) · el producto sigue sin aislar su podman del del dueño

- MAC-05 se arregló **en el nombre** (`MACHINE_NAME`, `machine.json`,
  `adopted:false` — verificado), pero **no en el emplazamiento**: sin el arnés de
  la prueba, `machine init` crea la máquina dentro de
  `~/.config/containers/podman/machine/applehv` y
  `~/.local/share/containers/podman/machine/applehv` del dueño, y monta su
  `~/.config/containers` dentro de la VM (`--device virtio-fs,sharedDir=…/.config/containers`,
  visto en el `argv` de vfkit).
- **Y un choque concreto de ficheros**: el `gvproxy` de nuestra máquina se lanza
  con `-pid-file $TMPDIR/podman/gvproxy.pid` — **sin el nombre de la máquina**
  (el resto de sockets sí lo llevan). Con el `TMPDIR` real del dueño, ese fichero
  es **el mismo** que el de su `podman-machine-default` y se pisa. Por eso esta
  prueba fijó `TMPDIR` al sandbox.

### MAC2-15 · MENOR · `cmd_up` todavía pasa por el `_ensure_machine` viejo (podman del PATH)

- `cmd_up` → `_run` (`safent:343`) → `_ensure_machine` (`safent:225-242`), que
  **no** es el `cmd_ensure_machine` corregido: usa `podman` **del PATH**, no `$RT`
  (en este Mac, `/opt/podman/bin/podman`, versión **6.0.1**, el del dueño), y sigue
  cogiendo `machine list -q | head -1`, la primera máquina que haya. Con el arnés
  XDG no hizo daño; sin él, `cmd_up` ejecutaría el binario ajeno contra
  `podman-machine-default`. Es MAC-05 a medio arreglar: corregido en el verbo
  porcelana, intacto en el camino que ese verbo recorre.

---

## Lo que no se pudo simular

- **Todo lo gráfico**: ventana, etapas pintadas, «Cancelar», barra de menús,
  instancia única, `Cmd+L`, Anuncios. No hay pantalla por SSH.
- **El compañero (029)**: `--selftest` sin compañero, por encargo. El digest de
  `safent-ads` sí quedó verificado en el manifiesto (hijo arm64 de `v0.2.0`).
- **La actualización de un botón (US2 de 028)**: requiere una versión nueva
  publicada.
- **El recorrido del dueño con `~/.safent`**: las reglas de la prueba prohíben
  escribir en su home, así que MAC2-07 se observó en el sandbox y su ausencia en
  `~/.safent` es una deducción de las rutas montadas, no una medición.

## Desmontaje

```
$RT ps -a / volume ls / machine list      -> mactest2 · mactest2-data · mactest2-engine (nombres verificados ANTES de borrar)
$RT rm -f mactest2                        rc=0
$RT volume rm -f mactest2-data            rc=0
$RT machine stop mactest2-engine          -> Machine "mactest2-engine" stopped successfully
$RT machine rm -f mactest2-engine         -> (sin máquinas en el sandbox)
pgrep -fl safent-mac-test2                -> (vacío)
mount | grep -c safent-mac-test2          -> 0        (DMG desmontado en el paso 1)
rm -rf /tmp/safent-mac-test2              -> No such file or directory
ls -d ~/.safent                           -> No such file or directory   (no existía; sigue sin existir)
ls /var/folders/…/T/podman/               -> sólo ficheros de podman-machine-default (intactos, del 27-ago)
ls ~/.local/share/containers/podman/machine/applehv -> sólo 'cache' (16-jul); nada de mactest2
/opt/podman/bin/podman machine list       -> podman-machine-default  libkrun  Currently running
pgrep -fl krunkit|gvproxy                 -> los dos procesos del dueño, vivos e intactos
```
Todo lo creado por la prueba —máquina, contenedor, volumen, imágenes, sandbox,
DMG montado— está retirado con el podman **empaquetado**, verificando los nombres
antes de cada borrado. La máquina del dueño quedó **arrancada e intacta**.

## Siguiente acción recomendada

Por orden; los tres primeros son el mismo recorrido roto:

1. **MAC2-01** — que `machines[]` lleve proveedor real y los campos que
   `MachineSpec` compara (o que deje de compararlos). Sin esto, `ensure-images` es
   inalcanzable y cada arranque recrea el motor. `debug-engineer` + una línea en
   `data-model.md` fijando el vocabulario.
2. **MAC2-03 + MAC2-02** — narrar la descarga (progreso real por capa) y no dejar
   que `up` la haga de tapadillo dentro de una etapa sin retorno.
   `backend-engineer` en `safent`, `frontend-engineer`/`debug-engineer` en el
   vigía.
3. **MAC2-04** — matar el **grupo** de procesos, no sólo al hijo.
4. **MAC2-05** — publicar el puerto de forma que el host lo alcance (y, sea cual
   sea la vía, **que el selftest exija una respuesta HTTP del vale antes de
   devolver 0**). `software-architect` + `devops-engineer`: afecta a la imagen de
   máquina empaquetada.
5. **MAC2-06** — dejar de recrear un motor sano (cae solo al arreglar MAC2-01,
   pero merece su propia prueba de regresión).
6. **MAC2-07** — el perfil seccomp no puede ser una ruta del host en macOS.
7. **MAC2-09 / MAC2-10 / MAC2-11** — higiene del contrato NDJSON (una etapa, un
   cierre; un código honesto para el timeout; `preflight` visible).
8. **MAC2-12 / MAC2-13 / MAC2-14 / MAC2-15** — grapar el `.app`, empaquetar
   `containers.conf`, aislar `XDG_*`/`TMPDIR`, y terminar MAC-05 en
   `_ensure_machine`.
