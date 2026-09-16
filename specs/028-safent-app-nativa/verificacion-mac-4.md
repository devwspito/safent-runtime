# Verificación 4 — app nativa de macOS en el MacBook del dueño

**Qué**: cuarta prueba real del artefacto notarizado `Safent-macOS-arm64` del run
**34565289948** (`devwspito/agents-autonomy`, rama `safent-desktop-pipeline-028`,
`42ee4234`; el run figura en rojo por otro job), versión 0.9.0, con el motor
fijado al digest `sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3`
(= `ghcr.io/devwspito/safent:v0.9.0-rc2`) y el compañero `safent-ads` a
`sha256:54c4fee8535d750c027ae2976984feef9884c7f11f7702894f410e03161264b9`.
**Dónde**: MacBook Air del dueño (`macbook-air-2-1`), macOS 15.5 (24F74), arm64,
79 GiB libres. Por SSH, **sin sesión gráfica**.
**Cuándo**: 11-sep-2026, 07:26–07:52 (hora local del Mac). **Quién**: exploratory-tester.

## Veredicto

**NO se puede publicar.** Cuatro de los cinco arreglos del encargo se confirman
en vivo (DMG firmado y con ticket embebido, digest real del contenedor, seccomp
empaquetado + rutas canónicas, helpers **propios** moviendo la máquina), pero
**poblar los `cdhash` ha destapado un bloqueante nuevo**: en un Mac limpio la app
**no arranca**, muere a los **6,4 s** con `runtime_hash_mismatch` (MAC4-01). Es la
**misma** línea que el commit `70330e4` arregló en el empaquetador y que sigue sin
arreglar en el consumidor (`safent:1920`). Un carácter: `2>/dev/null` → `2>&1`.

Con ese único punto puenteado por un arnés de la prueba, el resto del recorrido
va bien salvo la **reapertura**, que **sigue destruyendo el motor sano** (MAC4-02)
—la causa ya no es la que decía MAC3-02, que sí está arreglada, sino `_run`.

| Comprobación del encargo | Resultado |
|---|---|
| (1) sha256 en los dos extremos | **PASA** |
| (1) `codesign --verify --strict` + `-dvv` del **DMG** (`Authority=Developer ID … Luis Correa`) | **PASA** (MAC3-01 arreglado) |
| (1) `spctl -a -t open --context context:primary-signature` del DMG | **PASA** (`accepted`) |
| (1) `stapler validate` del DMG | **PASA** (y `codesign -dvv` → `Notarization Ticket=stapled`) |
| (1) `stapler validate` del `.app` | **FALLA** (MAC4-06 — sigue sin grapar) |
| (1) `spctl -a -vv -t exec` del `.app` | **PASA** |
| (1) `cdhash` no nulo y **igual** al del fichero enviado | **PASA** (8/8 Mach-O) |
| (1) las entradas sha256 del manifiesto casan | **PASA** (17/17 sha256 y 17/17 modo) |
| (1) `containers.conf` en el paquete | **PASA** |
| (2) **Arranque en frío en un Mac limpio** | **FALLA — BLOQUEANTE (MAC4-01)** |
| (2) Etapas en orden, con latidos (con el arnés) | **PASA** (hueco máx. en `pull_engine` 4,04 s) |
| (2) `curl /healthz` desde el **host macOS** → 200 | **PASA** (200/200/200) |
| (2) La URL `?k=` sirve el HTML con el bearer inyectado | **PASA** (307 → 200, `window.__SAFENT_TOKEN__`) |
| (2) **gvproxy/vfkit en ejecución = los EMPAQUETADOS** | **PASA** (sha256 idénticos a los del bundle) |
| (3) Reapertura con motor sano: mismo id y mismo puerto | **FALLA** (MAC4-02) |
| (4) Reanudación tras `podman kill`, desatendida | **PASA** (`ready` en 6,93 s) |
| (5) `podman-machine-default` intacta, cero huérfanos, desmontaje | **PASA** |

## Reglas de la prueba y arnés prestado

Todo bajo `/tmp/safent-mac-test4/` con `SAFENT_STATE_HOME=/tmp/safent-mac-test4/state`
**escrito en su forma no canónica a propósito** (es el defecto MAC3-03 que había
que volver a probar) y `SAFENT_NAME=mactest4` → contenedor `mactest4`, volumen
`mactest4-data`, **máquina `mactest4-engine`** (nombre real verificado con
`machine list`, nunca supuesto). Nunca se ejecutó `tailscale`; nunca se tocó
`/opt/podman`, `~/.config/containers`, `~/.local/share/containers`, `~/.ssh`,
`/Applications` ni `podman-machine-default`. Sin `SAFENT_ENGINE_DIGEST` ni
`SAFENT_RUNTIME_DIR`.

Arneses **añadidos desde fuera** (el producto no los trae):

1. `XDG_CONFIG_HOME`, `XDG_DATA_HOME` y `TMPDIR` dentro del sandbox — sigue
   vigente MAC3-09, aunque el CLI ya fija `TMPDIR` él solo si viene vacío
   (`safent:201-205`).
2. **`SAFENT_CODESIGN`** apuntando a un envoltorio que llama al `codesign` de
   verdad y sólo funde el informe de `-d` con stdout. Sin él **no hay pase 2, ni
   3, ni 4**: la app no arranca (MAC4-01). Es el arnés que convierte el
   bloqueante en algo medible, y su existencia **es** la prueba del defecto.

```sh
#!/bin/sh    # /private/tmp/safent-mac-test4/codesign-harness
case "$1" in -d*) exec /usr/bin/codesign "$@" 2>&1 ;; esac
exec /usr/bin/codesign "$@"
```

Estado del dueño **antes** de empezar (07:26): `podman-machine-default` libkrun
`running`, `krunkit`=20043, `gvproxy`=20042, `~/.safent` inexistente,
`…/machine/applehv` sólo con `cache`, 0 `podman run` huérfanos, 79 Gi libres.

---

## Paso 1 — Descarga, traslado y verificación del paquete

### 1.1 Descarga (DGX) — **PASA**

```
gh api repos/devwspito/agents-autonomy/actions/artifacts/10185951375/zip > f.zip
   -> 2 017 918 089 B   sha256 d385042671662a98a55d86cc0149d88dfa892e39135f17b27ad4fe29d8d5df9f
unzip -l:
   1 013 445 945  dmg/Safent_0.9.0_aarch64.dmg
   1 007 249 256  macos/Safent.app.tar.gz
             404  macos/Safent.app.tar.gz.sig
```

### 1.2 Traslado — **PASA**

```
scp dmg/Safent_0.9.0_aarch64.dmg luiscorrea@macbook-air-2-1:/tmp/safent-mac-test4/   (8,0 s)
sha256 DGX : c7df9d03a7eed362d42ad010ba59358e1ab262da4ca79d0055e886beb46a98bf
sha256 Mac : c7df9d03a7eed362d42ad010ba59358e1ab262da4ca79d0055e886beb46a98bf   IDÉNTICO
```

### 1.3 Firma, notarización y grapado — **DMG PASA (MAC3-01 arreglado)**

```
codesign --verify --strict --verbose=2 Safent_0.9.0_aarch64.dmg
  -> valid on disk · satisfies its Designated Requirement            rc=0
codesign -dvv Safent_0.9.0_aarch64.dmg
  -> Identifier=Safent_0.9.0_aarch64 · Format=disk image
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Authority=Developer ID Certification Authority · Authority=Apple Root CA
     Timestamp=11 Sep 2026 at 07:20:49
     Notarization Ticket=stapled          <- el ticket VIAJA DENTRO del fichero
     TeamIdentifier=JBMBA58A8X                                       rc=0
spctl -a -vv -t open --context context:primary-signature <dmg>
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)       rc=0
xcrun stapler validate <dmg>   -> The validate action worked!        rc=0
xattr -l <dmg>                 -> (vacío)

hdiutil attach -nobrowse -readonly -mountpoint …/mnt <dmg>   -> /dev/disk5s1
   contenido: Safent.app, Applications -> /Applications
ditto mnt/Safent.app …/Safent.app        (0,76 s)   -> 1,0 GB, 22 ficheros
hdiutil detach …/mnt                     -> "disk4" ejected;  mount | grep -c = 0

spctl -a -vv -t exec Safent.app
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)       rc=0
codesign --verify --strict --verbose=2 Safent.app          -> valid on disk   rc=0
codesign --verify --deep --strict --verbose=2 Safent.app                      rc=0
codesign -dvvv Safent.app
  -> Identifier=com.safent.desktop · CodeDirectory v=20500 flags=0x10000(runtime)
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Timestamp=11 Sep 2026 at 07:20:22 · TeamIdentifier=JBMBA58A8X · Runtime 14.5.0
xcrun stapler validate Safent.app
  -> Safent.app does not have a ticket stapled to it.               rc=65   <- MAC4-06
```

`stapler validate -v` del DMG sigue **descargando** un ticket para validar (es lo
que hace esa herramienta), pero esta vez eso ya no prueba nada en contra: el
propio `codesign -dvv` declara `Notarization Ticket=stapled`. La salvedad de
MAC3-01 queda cerrada.

### 1.4 `runtime-bundle.json`: 17 entradas, 8 `cdhash` reales — **PASA**

`Contents/Resources/runtime/` trae **18** ficheros (17 + `runtime-bundle.json`),
**planos** (sin `bin/`), con `containers.conf` (43 B) y `safent.json` (19 766 B)
nuevos respecto a la verificación 3.

```
podman_version : 6.1.1    entries: 17    cdhash no nulos: 8 (los 8 Mach-O)
engine_image   : ghcr.io/devwspito/safent       sha256:9bafbad6…dbf8a3   linux/arm64
companion_image: ghcr.io/devwspito/safent-ads   sha256:54c4fee8…1264b9   linux/arm64
cat containers.conf ->  [engine]
                        helper_binaries_dir = ["$BINDIR"]
```

| Fichero | sha256 vs manifiesto | modo | cdhash del manifiesto | vs `codesign -dvvv` | `--verify --strict` |
|---|---|---|---|---|---|
| provision.sh | OK | 0755 | null | n/a | n/a |
| **containers.conf** | OK | 0644 | null | n/a | n/a |
| caps.template.yaml | OK | 0644 | null | n/a | n/a |
| podman | OK | 0755 | ea3733d4…f2a20 | **IGUAL** | rc=0 |
| vfkit | OK | 0755 | b2817b0b…2c9e9 | **IGUAL** | rc=0 |
| libkrun.dylib | OK | 0644 | aed38420…b52f2 | **IGUAL** | rc=0 |
| libepoxy.0.dylib | OK | 0644 | c4b2c942…1694c | **IGUAL** | rc=0 |
| podman-machine.aarch64.applehv.raw.zst | OK | 0644 | null | n/a | n/a |
| krunkit | OK | 0755 | d039e2e7…55ba7 | **IGUAL** | rc=0 |
| KRUN_EFI.silent.fd | OK | 0644 | null | n/a | n/a |
| gvproxy | OK | 0755 | e946b4d0…33b10 | **IGUAL** | rc=0 |
| libMoltenVK.dylib | OK | 0644 | 70a4304c…5c1f4 | **IGUAL** | rc=0 |
| compose.yaml | OK | 0644 | null | n/a | n/a |
| **safent.json** | OK | 0644 | null | n/a | n/a |
| run-safent.sh | OK | 0755 | null | n/a | n/a |
| safent | OK | 0755 | null | n/a | n/a |
| libvirglrenderer.1.dylib | OK | 0644 | 92b22cda…70481 | **IGUAL** | rc=0 |

`17/17 sha256 · 17/17 modo · 8/8 cdhash · 0 discrepancias`. MAC3-05 arreglado.

---

## Paso 2 — Arranque en frío (`--selftest`, sin compañero)

Binario: `Safent.app/Contents/MacOS/safent-desktop --selftest`, NDJSON capturado
en vivo con marca de tiempo relativa (`perl -MTime::HiRes`). Sandbox comprobado
**antes**: `machine list` vacío, `state` vacío, `podman --version` = 6.1.1,
`machineconfigdir` dentro del sandbox, `numberofmachines: 0`.

### Pase 1 — tal y como sale de la caja (**FALLA — MAC4-01**)

```
   2.079 {"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
   2.103 {"kind":"progress","stage":"runtime_staging","done":1,"total":17,"unit":"steps"}
   2.116 {"kind":"progress","stage":"runtime_staging","done":2,"total":17,"unit":"steps"}
   2.129 {"kind":"progress","stage":"runtime_staging","done":3,"total":17,"unit":"steps"}
   6.290 {"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
   6.312 {"kind":"progress","stage":"runtime_staging","done":1,"total":17,"unit":"steps"}
   6.324 {"kind":"progress","stage":"runtime_staging","done":2,"total":17,"unit":"steps"}
   6.337 {"kind":"progress","stage":"runtime_staging","done":3,"total":17,"unit":"steps"}
   6.391 {"kind":"failed","code":"runtime_hash_mismatch","detail":"podman no coincide con el cdhash del manifiesto","retryable":false}
rc=1
```

Se para en la **entrada 4** del manifiesto, `podman` — la primera con `cdhash`.
Duración total hasta la muerte: **6,4 s**. `retryable:false`: la app no reintenta
nunca más. El fichero de stderr de la app queda **vacío**: el dueño no tiene nada
que leer. Ver MAC4-01 para la causa exacta y el arreglo de un carácter.

### Pase 2 — arranque en frío completo, con el arnés `SAFENT_CODESIGN` (**PASA**, rc=0)

Estado borrado y vuelto a crear antes del pase (`state` vacío, sin máquinas).

```
   2.114 {"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
   2.138 {"kind":"progress","stage":"runtime_staging","done":1,"total":17,"unit":"steps"}
   2.150 {"kind":"progress","stage":"runtime_staging","done":2,"total":17,"unit":"steps"}
   2.164 {"kind":"progress","stage":"runtime_staging","done":3,"total":17,"unit":"steps"}
   2.345 {"kind":"progress","stage":"runtime_staging","done":4,"total":17,"unit":"steps"}
   2.699 {"kind":"progress","stage":"runtime_staging","done":5,"total":17,"unit":"steps"}
   2.761 {"kind":"progress","stage":"runtime_staging","done":6,"total":17,"unit":"steps"}
   2.804 {"kind":"progress","stage":"runtime_staging","done":7,"total":17,"unit":"steps"}
   3.718 {"kind":"progress","stage":"runtime_staging","done":8,"total":17,"unit":"steps"}
   3.779 {"kind":"progress","stage":"runtime_staging","done":9,"total":17,"unit":"steps"}
   3.797 {"kind":"progress","stage":"runtime_staging","done":10,"total":17,"unit":"steps"}
   3.954 {"kind":"progress","stage":"runtime_staging","done":11,"total":17,"unit":"steps"}
   4.014 {"kind":"progress","stage":"runtime_staging","done":12,"total":17,"unit":"steps"}
   4.027 {"kind":"progress","stage":"runtime_staging","done":13,"total":17,"unit":"steps"}
   4.040 {"kind":"progress","stage":"runtime_staging","done":14,"total":17,"unit":"steps"}
   4.053 {"kind":"progress","stage":"runtime_staging","done":15,"total":17,"unit":"steps"}
   4.066 {"kind":"progress","stage":"runtime_staging","done":16,"total":17,"unit":"steps"}
   4.107 {"kind":"progress","stage":"runtime_staging","done":17,"total":17,"unit":"steps"}
   4.109 {"kind":"done","stage":"runtime_staging","ms":2000}
   6.169 {"kind":"stage","stage":"machine","label":"Preparando la maquina","total_bytes":null,"point_of_no_return":false}
  27.094 {"kind":"done","stage":"machine","ms":21000}
  27.456 {"kind":"stage","stage":"pull_engine","label":"Descargando Safent","total_bytes":null,"point_of_no_return":false}
  31.484 {"kind":"progress","stage":"pull_engine","done":1,"total":null,"unit":"steps"}
  35.495 {"kind":"progress","stage":"pull_engine","done":2,"total":null,"unit":"steps"}
  39.511 {"kind":"progress","stage":"pull_engine","done":3,"total":null,"unit":"steps"}
  43.536 {"kind":"progress","stage":"pull_engine","done":4,"total":null,"unit":"steps"}
  47.547 {"kind":"progress","stage":"pull_engine","done":5,"total":null,"unit":"steps"}
  51.560 {"kind":"progress","stage":"pull_engine","done":6,"total":null,"unit":"steps"}
  55.572 {"kind":"progress","stage":"pull_engine","done":7,"total":null,"unit":"steps"}
  59.593 {"kind":"progress","stage":"pull_engine","done":8,"total":null,"unit":"steps"}
  63.618 {"kind":"progress","stage":"pull_engine","done":9,"total":null,"unit":"steps"}
  67.657 {"kind":"progress","stage":"pull_engine","done":10,"total":null,"unit":"steps"}
  71.692 {"kind":"progress","stage":"pull_engine","done":11,"total":null,"unit":"steps"}
  75.722 {"kind":"progress","stage":"pull_engine","done":12,"total":null,"unit":"steps"}
  79.737 {"kind":"progress","stage":"pull_engine","done":13,"total":null,"unit":"steps"}
  83.760 {"kind":"progress","stage":"pull_engine","done":14,"total":null,"unit":"steps"}
  87.774 {"kind":"progress","stage":"pull_engine","done":15,"total":null,"unit":"steps"}
  91.785 {"kind":"progress","stage":"pull_engine","done":16,"total":null,"unit":"steps"}
  95.798 {"kind":"progress","stage":"pull_engine","done":17,"total":null,"unit":"steps"}
  99.842 {"kind":"done","stage":"pull_engine","ms":72000}
 100.283 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
 100.908 {"kind":"done","stage":"container","ms":1000}
 100.912 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
 101.039 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
 106.505 {"kind":"done","stage":"health","ms":6000}
 106.587 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3","companion_digest":null}
rc=0
```

**Duraciones por etapa (pase 2, arranque en frío)**

| Etapa | Entra | Cierra | Duración real | `ms` declarado | Avance |
|---|---|---|---|---|---|
| `runtime_staging` | 2,114 | 4,109 | **2,00 s** | 2000 | 17/17 |
| `machine` | 6,169 | 27,094 | **20,93 s** | 21000 | **0 eventos** (MAC4-05) |
| `pull_engine` | 27,456 | 99,842 | **72,39 s** | 72000 | 17 latidos, hueco máx. **4,04 s** |
| `container` | 100,283 | 100,908 | **0,63 s** | 1000 | una apertura, un cierre |
| `health` | 100,912 | 106,505 | **5,59 s** | 5000 | 1 |
| `ready` | — | 106,587 | — | — | digest rc2, `companion_digest:null` |
| **Total** | | | **106,6 s** | | |

Huecos mudos, de mayor a menor: **20,93 s** (dentro de `machine`), 5,47 s (dentro
de `health`), 4,04 s y 4,04 s (latidos de `pull_engine`). Cada `stage` se abre una
vez y cierra una vez. **`pull_engine` cumple el encargo: ninguna ventana muda > 15 s.**

### La comprobación decisiva — `/healthz` **desde el host macOS** (**PASA**)

```
$RT machine list -> mactest4-engine  applehv  true            (nombre real, verificado)
$RT ps           -> e67de8ed3d8c  mactest4  Up 24 seconds  127.0.0.1:35013->7517/tcp
                    ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3
curl -sS -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:35013/healthz
  -> 200 / 200 / 200
curl -sS http://127.0.0.1:35013/healthz
  -> {"status":"ok","service":"hermes-shell-server","version":"0.9.0","ts":"2026-09-11T05:46:12.782183+00:00"}
lsof -nP -iTCP:35013 -> gvproxy 31673 … 127.0.0.1:35013 (LISTEN)   (sin CLOSE_WAIT)
```

### La comprobación de «Mac limpio» — los helpers son los **EMPAQUETADOS** (**PASA**)

```
ps -o pid=,comm= -p 31673 -p 31676
  31673 /private/tmp/safent-mac-test4/Safent.app/Contents/Resources/runtime/gvproxy
  31676 /private/tmp/safent-mac-test4/Safent.app/Contents/Resources/runtime/vfkit
shasum -a 256 de los binarios EN EJECUCIÓN:
  gvproxy 32633cf03349f5de…  == empaquetado 32633cf03349f5de…  != /opt/podman 36c0ca43b5552db4…
  vfkit   6b641707ea627a66…  == empaquetado 6b641707ea627a66…  != /opt/podman 0489f7caef8f91f4…
ps -o args= -p 31673 | cut -c1-200
  …/runtime/gvproxy -mtu 1500 -ssh-port 56774 -listen-vfkit unixgram:///private/tmp/safent-mac-test4/tmp/podman/mactest4-engine-gvproxy.sock …
```

**MAC3-07 arreglado en lo que importa.** El `containers.conf` empaquetado
(`helper_binaries_dir = ["$BINDIR"]`) gana antes de que el PATH entre en juego, y
además el socket de gvproxy ya lleva el **nombre de la máquina**, así que no pisa
el del dueño. (El guardia «si se cuela uno ajeno, falla a gritos» **no** funciona:
ver MAC4-03.)

### Seccomp y estado canónico (**PASA — MAC3-03 arreglado**)

```
$RT inspect mactest4 --format '{{.HostConfig.SecurityOpt}}'
  -> [label=disable seccomp=/private/tmp/safent-mac-test4/Safent.app/Contents/Resources/runtime/safent.json]
```

Con `SAFENT_STATE_HOME=/tmp/…` **escrito sin canonizar**, el perfil ya no viaja
como ruta del estado: sale del propio paquete (tier 0), en forma canónica
(`/private/tmp/…`), que es lo que el podman **de dentro de la VM** puede abrir.

### `facts` (**PASA — MAC3-02 arreglado en el CLI**)

```
"machines": [{"name":"mactest4-engine","provider":"applehv","cpus":4,
              "memoryBytes":8589934592,"rootful":true,"running":true,"ours":true}]
"engineContainer": {"exists":true,"running":true,
                    "imageDigest":"sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3"}
"localEngineImageDigest": "sha256:9bafbad6…dbf8a3"   "publishedPort": 35013   "daemonHealth": "healthy"
```

`engineContainer.imageDigest` ya es un **digest de verdad** e **igual** al deseado:
`images_gap` queda satisfecho. Y aun así la reapertura recrea — ver MAC4-02.

### La URL `?k=` que abriría la app (**PASA**)

Vale obtenido por el descriptor 3, como manda `app-engine.md` §5
(`safent up --secret-fd 3 --porcelain 3>ticket`):

```
fd 3 -> http://127.0.0.1:35401/?k=<VALE>          (una sola línea, nunca en stdout)
curl -sS  'http://127.0.0.1:35401/?k=<VALE>'  -> http=307  redirect=…/app/?k=<VALE>
curl -sSL 'http://127.0.0.1:35401/?k=<VALE>'  -> http=200  bytes=1555
  HTML: <script>window.__SAFENT_TOKEN__="1f8dfc83…(64 hex)";</script>   <- bearer inyectado
  el vale NO aparece literal en el HTML (se canjea, no se refleja): grep -c = 0
curl 'http://127.0.0.1:35401/app/'  (sin vale) -> http=200 bytes=1488, SIN token
NDJSON del verbo `up` directo (forma del contrato §3, `t`/`id`):
  {"t":"stage","id":"container","label":"Creando el contenedor"}
  {"t":"done","id":"container","ms":8000}
  {"t":"stage","id":"health","label":"Esperando a que Safent este listo"}
  {"t":"progress","id":"health","done":0,"total":48,"unit":"steps"}
  {"t":"done","id":"health","ms":5000}
  {"t":"ready","endpoint_ref":"stdout-secret"}
```

---

## Paso 3 — Reapertura con el motor sano (**FALLA — MAC4-02**)

```
ANTES:   id=e67de8ed3d8c  puerto=127.0.0.1:35013->7517/tcp  estado=Up About a minute
   0.842 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
   6.904 {"kind":"done","stage":"container","ms":6000}
   6.913 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
   7.042 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
  12.635 {"kind":"done","stage":"health","ms":5000}
  12.739 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6…dbf8a3","companion_digest":null}
rc=0
DESPUES: id=c023396cdc6b  puerto=127.0.0.1:36017->7517/tcp  estado=Up 6 seconds
```

**Otro contenedor, otro puerto, 12,7 s.** `quickstart.md` §4.2 pide ≤ 10 s y «no se
repite la preparación». Repetido una tercera vez con el `up` del CLI:
`c023396cdc6b`/36017 → `8164cb7e2019`/35401. Los datos sobreviven (volumen
`mactest4-data`); el trabajo en curso del motor, no.

## Paso 4 — Reanudación tras matar el contenedor (**PASA**)

```
$RT kill mactest4  -> mactest4        (Exited (137) 2 seconds ago)
   0.587 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
   1.160 {"kind":"done","stage":"container","ms":1000}
   1.169 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
   1.290 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
   6.843 {"kind":"done","stage":"health","ms":5000}
   6.928 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6…dbf8a3","companion_digest":null}
rc=0
DESPUES: id=73146d0c8b1d  puerto=127.0.0.1:40721->7517/tcp  estado=Up 6 seconds
curl /healthz desde el host tras reanudar -> 200
```

Se recupera **sola**, en **6,93 s**, sin preguntar nada, y el puerto nuevo
**responde desde macOS**.

> **Primer intento de este paso, invalidado por la propia prueba.** Antes de matar
> el contenedor se había lanzado `safent up` a mano **sin `--no-companion`** para
> obtener el vale; eso montó el andamiaje del compañero. El pase siguiente murió a
> los 3,067 s con
> `{"kind":"failed","code":"daemon_unhealthy","detail":"[x] …/state/companions/ads/image not found — refusing to guess the companion's image.…","retryable":true}`.
> El envoltorio **nunca** provoca eso (pasa `--no-companion` cuando no hay
> compañero, `engine_adapter.rs:216`), así que **no cuenta como fallo del paso 4**
> — pero el camino de código es del producto y deja una mina: ver MAC4-04. Tras
> borrar el andamiaje y la red, el paso se repitió limpio (el de arriba).

## Paso 5 — Convivencia con el podman del dueño (**PASA**)

| Medida | Antes (07:26) | Después (07:52) |
|---|---|---|
| `podman-machine-default` | libkrun · running | libkrun · **running** |
| `krunkit` (PID) | 20043 | **20043** |
| `gvproxy` del dueño (PID) | 20042 | **20042** |
| `~/.config/containers` (mtime) | 2026-07-16T11:02:28 | **igual** |
| `~/.local/share/containers` | 2026-07-16T11:01:56 | **igual** |
| `…/containers/podman/machine` | 2026-07-16T11:02:28 | **igual** |
| `/opt/podman` | 2026-07-08T22:08:46 | **igual** |
| `~/.safent` | no existe | **no existe** |
| `…/machine/applehv` | sólo `cache` | **sólo `cache`** |
| `/var/folders/…/T/podman/` | sólo ficheros de `podman-machine-default` | **igual** |
| `podman run` huérfanos | 0 | **0** |

---

## Hallazgos

### MAC4-01 · BLOQUEANTE (release) · en un Mac limpio la app **no arranca**: `runtime_hash_mismatch`

- **Evidencia**: pase 1, línea exacta —
  `{"kind":"failed","code":"runtime_hash_mismatch","detail":"podman no coincide con el cdhash del manifiesto","retryable":false}`
  a los **6,391 s**, con rc=1 y **stderr vacío**. Reproducible al 100 % (dos
  intentos internos del propio envoltorio, ambos con el mismo fallo).
- **Causa, en una línea**: `safent:1920` —
  `real_cdhash="$("$codesign_bin" -dvvv "$src" 2>/dev/null | sed -n 's/^CDHash=\(.*\)$/\1/p' | head -1 || true)"`.
  `codesign -d` **informa por stderr** (convención de Apple), así que `2>/dev/null`
  se lleva por delante la línea `CDHash=` → `real_cdhash` queda **vacío** → nunca
  puede ser igual al del manifiesto → `_die_porcelain runtime_hash_mismatch`
  (`safent:1921`), **no reintentable**.
- **Comprobado en vivo, en el propio Mac**:

  ```
  codesign -dvvv <bundle>/podman 2>/dev/null | sed -n 's/^CDHash=\(.*\)$/\1/p' | head -1
    -> (vacío)
  codesign -dvvv <bundle>/podman 2>&1      | sed -n 's/^CDHash=\(.*\)$/\1/p' | head -1
    -> ea3733d42d13d5ecde9044eb9ebc52bf981f2a20
  jq -r '.entries[]|select(.path=="podman")|.cdhash' runtime-bundle.json
    -> ea3733d42d13d5ecde9044eb9ebc52bf981f2a20      <- coinciden; sólo falla el CANAL
  ```

- **Por qué aparece justo ahora**: es **exactamente** el defecto que el commit
  `70330e4` («codesign -d reports on stderr, so every cdhash shipped null»)
  arregló en el **productor** (`stage-runtime.sh`). Nadie lo arregló en el
  **consumidor** (`safent`). Mientras los `cdhash` viajaban a `null` (MAC3-05), la
  rama no se ejecutaba nunca; poblarlos la ha encendido por primera vez.
  Verificación 3 ya lo avisaba: «La rama `codesign`+cdhash de `cmd_stage_runtime`
  sigue sin ejercitarse».
- **Arreglo**: `2>/dev/null` → `2>&1` en `safent:1920`, el mismo cambio literal de
  `70330e4`. Y el test que acompañe al arreglo debe usar un `codesign` falso que
  escriba en **stderr**, porque el que existía escribía en stdout y por eso no lo
  cazó ninguna de las dos veces.
- **Lo que ve el dueño**: la app se cierra sola a los seis segundos con
  «podman no coincide con el cdhash del manifiesto», sin salida ni diagnóstico, y
  **no reintenta**. Recuerda a un paquete corrupto cuando el paquete es correcto.

### MAC4-02 · MAYOR · reabrir la app **sigue destruyendo** el motor sano (causa nueva, la tercera)

- **Evidencia**: paso 3 — `e67de8ed3d8c`/35013 (Up About a minute) →
  `c023396cdc6b`/36017 (Up 6 seconds), 12,7 s, con el motor sano.
- **Lo que SÍ está arreglado**: `facts` ya emite el digest real
  (`imageDigest: sha256:9bafbad6…`), igual al deseado, así que `images_gap`
  (`reconcile.rs:155-169`) devuelve `None` y `container_gap` también. **MAC3-02
  está cerrado**: la causa que describía ya no existe.
- **La causa nueva, en dos piezas**:
  1. `boot.rs:282-297` — al converger sin haber acuñado vale, el bucle **vuelve a
     invocar `up` una vez** para conseguirlo («`up` is documented idempotent … and
     is the only place a ticket is minted»). El NDJSON lo confirma: empieza
     directo en `container`, sin `machine` ni `pull_engine`.
  2. `safent:434-439` (`_run`) — `up` **no es idempotente**: hace
     `"$RT" rm -f "$NAME"` **incondicionalmente** y vuelve a crear el contenedor
     con `-p 127.0.0.1::7517` (puerto efímero nuevo).
- **Arreglo**: que `cmd_up`, cuando el contenedor existe, corre, lleva el digest
  deseado y responde `200` en `/healthz`, **no llame a `_run`** y se limite a
  releer el vale del contenedor vivo (ya lo hace:
  `"$RT" exec "$NAME" cat /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap`).
  Alternativa peor: que el envoltorio pida un verbo `ticket` aparte. Y el test de
  regresión tiene que mirar **id y puerto**, no sólo que se llegue a `ready`.

### MAC4-03 · MENOR · el guardia «si se cuela un helper ajeno, falla a gritos» es código muerto

- **Evidencia**, reproduciendo el parse exacto de `_foreign_engine_helper`
  (`safent:285-299`) con los cuatro procesos vivos en el Mac:

  ```
  comm-según-awk=[/opt/podman/bin/]   ¿casa */gvproxy|*/vfkit? NO -> continue
  comm-según-awk=[/private/tmp/saf]   ¿casa */gvproxy|*/vfkit? NO -> continue
  ```

- **Causa**: `ps -axo pid=,comm=,args=` **recorta `comm` a 16 caracteres**
  (`/opt/podman/bin/` y `/private/tmp/saf` miden exactamente 16). El `case` del
  guardia nunca casa, así que siempre devuelve vacío y **jamás** puede detectar un
  helper ajeno. Con `ps -o pid=,comm= -p <pid>` (sin `args`) sí sale la ruta
  entera — que es como se midió en la verificación 3 y como se ha medido aquí.
- **Segundo defecto en la misma función**: compara contra
  `"$_podman_bundle_dir/bin/<nombre>"`, pero en el `.app` real el runtime es
  **plano** (`Contents/Resources/runtime/gvproxy`, comprobado con `ls`), no
  `runtime/bin/`. Si el recorte se arreglara sin arreglar esto, el guardia
  marcaría como **ajeno** al helper **propio** y bloquearía todos los arranques.
- **Impacto hoy**: bajo — la defensa de verdad (`containers.conf`) funciona y está
  medida. Pero la afirmación «un helper ajeno hace que falle a gritos» **no se
  sostiene**.

### MAC4-04 · MENOR (mina) · un andamiaje de compañero sin marcador `image` deja `facts` muerto

- **Evidencia**: tras un `safent up` de terminal (sin `--no-companion`),
  `safent facts --json` sale con **rc=1**, **cero bytes en stdout** y en stderr
  `[x] …/companions/ads/image not found — refusing to guess the companion's image.`
  El envoltorio lo traduce a `daemon_unhealthy` y la app no arranca.
- **Causa**: `_persisted_ads_image` (`safent:1106-1114`) hace `exit 1` crudo — sin
  evento `failed`, rompiendo el contrato de `--porcelain` (§2: todo lo que salga
  por stdout debe ser NDJSON; aquí no sale nada) — y lo llama el camino de
  observación cuando la red/el andamiaje del compañero existen pero el marcador no.
- **Alcance no cerrado**: en esta prueba lo provocó la prueba misma. El camino del
  envoltorio con compañero (`--selftest=companion`, 029) invoca `up` **sin**
  `--no-companion`, así que hay que comprobar si ese carril deja el mismo estado
  entre el primer y el segundo arranque. Pendiente para la verificación del 029.

### MAC4-05 · MENOR · la etapa `machine` pasa **20,93 s** sin un solo `progress` (MAC3-06 sin cambio)

- **Evidencia**: pase 2 — `stage machine` en 6,169 y `done` en 27,094, nada en
  medio. Es el mayor hueco de todo el arranque.
- **Choque**: `app-engine.md` §3 invariante 2 pide `progress` al menos cada 5 s.
  Sobrevive al vigía de 15 s porque `last_activity` también se refresca con
  stderr (`engine_adapter.rs:545-551`), no porque narre nada. Mismo arreglo que ya
  se hizo para `pull_engine` (`_pull_with_heartbeat`), pendiente en
  `cmd_ensure_machine`.

### MAC4-06 · MENOR · el `.app` sigue sin ticket grapado (MAC3-04 sin cambio)

- **Evidencia**: `xcrun stapler validate Safent.app` → rc=65, «does not have a
  ticket stapled to it», con el DMG que lo contiene **sí** grapado.
- Importa para la vía del actualizador (`macos/Safent.app.tar.gz`, publicado por
  este mismo artefacto), no para el primer arranque.

### MAC4-07 · MENOR · una etapa abierta dos veces sin cerrarse (MAC3-08 sin cambio)

- **Evidencia**: pase 1 — `stage runtime_staging` en 2,079 y otra vez en 6,290, sin
  `done` ni `failed` entre medias; el primer intento **no emite `failed` en
  absoluto**. Choca con `app-engine.md` §3 invariante 3. En el camino feliz
  (pase 2) no ocurre: una apertura, un cierre, para las cinco etapas.

### MAC4-08 · MENOR · MAC3-09 sigue: el producto no aísla su podman del del dueño

- El CLI ya se fija `TMPDIR` propio si viene vacío (`safent:201-205`) y el socket
  de gvproxy ya lleva el nombre de la máquina, así que la colisión concreta de
  `gvproxy.pid` está mitigada. Pero `XDG_CONFIG_HOME`/`XDG_DATA_HOME` los sigue
  poniendo **la prueba**: sin ellos, la máquina propia nacería dentro de
  `~/.config/containers` y `~/.local/share/containers` del dueño. No reproducido a
  propósito: las reglas de la prueba lo prohíben.

---

## Lo que no se pudo simular

- **Todo lo gráfico**: ventana, etapas pintadas, «Cancelar», barra de menús,
  instancia única, `Cmd+L`, Anuncios. No hay pantalla por SSH.
- **El doble clic con cuarentena**: se midió el veredicto de `spctl`/`stapler`, no
  el diálogo de Gatekeeper sobre un DMG descargado por Safari.
- **El compañero (029)**: `--selftest` sin compañero, por encargo. Su digest sí
  quedó verificado en el manifiesto, y MAC4-04 deja una pregunta abierta para ese
  carril.
- **El recorrido con `~/.safent`**: prohibido escribir en el home del dueño.
- **Un helper ajeno de verdad**: habría exigido tocar `/opt/podman` o el PATH del
  dueño; MAC4-03 se demostró reproduciendo el parse del guardia sobre los procesos
  reales, no forzando el caso.
- **La actualización de un botón (US2 de 028)**: requiere una versión nueva publicada.

## Desmontaje

```
Nombres reales verificados ANTES de borrar:
  contenedor  73146d0c8b1d  mactest4  ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3
  volumen     mactest4-data
  imagen      ghcr.io/devwspito/safent  365e584d7f5c  8.17 GB
  red         safent-companions            (creada por el `up` de terminal de la prueba)
  maquina     mactest4-engine  applehv  running

$RT rm -f mactest4                 -> mactest4
$RT volume rm -f mactest4-data     -> mactest4-data
$RT network rm -f safent-companions-> safent-companions
$RT rmi -f ghcr.io/devwspito/safent-> rc=0
$RT machine stop mactest4-engine   -> Machine "mactest4-engine" stopped successfully
$RT machine rm -f mactest4-engine  -> (sin máquinas en el sandbox)
rm -rf /private/tmp/safent-mac-test4  (3,0 GB)   -> No such file or directory
mount | grep -c safent-mac-test4        -> 0     hdiutil info | grep -c -> 0
pgrep -fl "safent-mac-test4|mactest4"   -> (vacío)
pgrep -fl "podman run"                  -> 0
podman machine list (dueño)             -> podman-machine-default  libkrun  running
krunkit=20043  gvproxy=20042            (los mismos PID que al empezar)
~/.safent                               -> sigue sin existir
~/.local/share/containers/podman/machine/applehv -> sólo 'cache'
df -h /System/Volumes/Data              -> 75 Gi libres (79 Gi al empezar; el sandbox
                                           está borrado y no queda nada nuestro —
                                           /private/tmp tiene ficheros >100 MB de OTRO
                                           proyecto, ajenos a esta prueba)
```

Todo lo creado por la prueba —máquina, contenedor, volumen, imagen, red, sandbox,
DMG montado— está retirado con el podman **empaquetado**, verificando los nombres
antes de cada borrado. La máquina del dueño quedó **arrancada e intacta**.

## Siguiente acción recomendada

1. **MAC4-01** — `2>/dev/null` → `2>&1` en `safent:1920`, y un test cuyo `codesign`
   falso escriba en **stderr**. Es lo único que impide arrancar. `debug-engineer`.
2. **MAC4-02** — que `up` reutilice un contenedor sano con el digest correcto en
   vez de `rm -f` incondicional; test de regresión sobre **id y puerto**.
   `backend-engineer`.
3. **MAC4-03** — arreglar el guardia de helpers (leer `comm` sin recortar, o casar
   contra `args`) **y** la ruta esperada (runtime plano, no `bin/`), o quitarlo:
   hoy es código muerto que aparenta protección.
4. **MAC4-04** — `_persisted_ads_image` no puede `exit 1` en crudo dentro de
   `--porcelain`; y comprobar el carril del compañero (029) contra esta mina.
5. **MAC4-06 / MAC4-05 / MAC4-07** — grapar el `.app`, latidos en `ensure-machine`,
   un solo cierre por etapa.
6. **MAC4-08** — aislar `XDG_CONFIG_HOME`/`XDG_DATA_HOME` desde el propio producto.

Cuando estén 1 y 2, esta misma prueba debería repetirse **entera y sin arneses**:
todo lo demás del encargo ya está verificado en vivo.
