# Verificación 6 — app nativa de macOS en el MacBook del dueño

**Qué**: sexta prueba real del artefacto `Safent-macOS-arm64` (id `10192531943`) del
run **34582572593** de `devwspito/agents-autonomy` (rama `safent-desktop-pipeline-028`,
`6308a14b`; el run figura en rojo por otro job), versión 0.9.0, construido desde
`safent-runtime` `feat/safent-next` **`fd623a5`**, con el motor fijado a
`ghcr.io/devwspito/safent:v0.9.0-rc2` =
`sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3`.
**Dónde**: MacBook Air del dueño (`macbook-air-2-1`), macOS 15.5, arm64, 91 GiB
libres. Por SSH, **sin sesión gráfica**.
**Cuándo**: 11-sep-2026, 11:19–11:39 (hora local del Mac). **Quién**: exploratory-tester.

## Veredicto

**LISTO PARA PUBLICAR.** Los dos cambios del encargo se confirman en vivo y el
bloqueante de la verificación 5 está **cerrado**: el arranque en frío en un Mac
limpio llega a `ready` **de caja, sin un solo arnés sobre la máquina**, en
**138,845 s** y con `rc=0`. Ninguna comprobación del encargo falla.

| Comprobación del encargo | Resultado |
|---|---|
| (1) sha256 del DMG en los dos extremos | **PASA** (`f6834254…a6204`) |
| (1) DMG `codesign --verify --strict` | **PASA** (rc=0) |
| (1) DMG `spctl -a -t open --context context:primary-signature` | **PASA** (`accepted`) |
| (1) DMG `stapler validate` | **PASA** (rc=0) |
| (1) `.app` `spctl -a -vv -t exec` | **PASA** (`accepted · Notarized Developer ID`) |
| (1) `.app` `codesign --verify --strict` — **el mandamiento del que ahora depende la app** | **PASA** (**rc=0**) |
| (1) `runtime-bundle.json` **sin** campo `cdhash` | **PASA** (0 apariciones) |
| (2) Arranque en frío `--selftest`, etapas en orden, `ready` | **PASA** (138,845 s, rc=0) |
| (2) `runtime_staging` rápido y **sin hash por fichero** | **PASA** (**1,021 s**, 17/17) |
| (2) Ninguna ventana muda > 15 s | **PASA** (máx. **9,141 s**) |
| (2) `/healthz` 200 desde el **host macOS** | **PASA** (200/200/200) |
| (2) La URL `?k=` sirve la app con el bearer | **PASA** (307 → 200, `__SAFENT_TOKEN__`) |
| (2) `ps -o args=` = gvproxy/vfkit **EMPAQUETADOS** | **PASA** (ruta dentro del `.app`) |
| (3) Reapertura: mismo id, mismo puerto, `ready` en segundos | **PASA** (**1,809 s**) |
| (4) Máquina del dueño intacta, cero huérfanos, desmontaje completo | **PASA** |

Hallazgos abiertos: **ninguno bloqueante**. Quedan dos menores heredados
(MAC5-03 y MAC5-05) y una observación nueva (MAC6-01), detallados abajo.

## Reglas de la prueba y arnés

Todo bajo `/tmp/safent-mac-test6/` con `SAFENT_STATE_HOME=/tmp/safent-mac-test6/state`
y `SAFENT_NAME=mactest6` → contenedor `mactest6`, volumen `mactest6-data`,
**máquina `mactest6-engine`** (nombre real verificado con `machine list`, nunca
supuesto). Nunca se ejecutó `tailscale`; nunca se tocó `/opt/podman`,
`~/.config/containers`, `~/.local/share/containers`, `~/.ssh`, `/Applications`
ni `podman-machine-default`.

Arnés **añadido desde fuera** (uno solo, el producto no lo trae):
`XDG_CONFIG_HOME`, `XDG_DATA_HOME` y `TMPDIR` dentro del sandbox — **sigue
vigente MAC5-05/MAC4-08**. Sin él la máquina de la prueba nacería dentro del
`~/.local/share/containers` del dueño, que las reglas prohíben tocar.

**El arnés de la verificación 5 ya NO hizo falta**: la máquina **no** se creó de
antemano. `env | grep -c SAFENT_CODESIGN` → **0**; sin `SAFENT_ENGINE_DIGEST`
ni `SAFENT_RUNTIME_DIR`. El sandbox estaba vacío antes de empezar
(`ls -A /tmp/safent-mac-test6 | wc -l` → 0).

Estado del dueño **antes** (11:19): `podman-machine-default` libkrun `running`,
`krunkit`=20043, `gvproxy`=20042, `~/.safent` inexistente, `…/machine/applehv`
sólo con `cache`, 0 `podman run` huérfanos, 91 Gi libres.

## Los dos cambios, leídos en el código de `fd623a5`

```
git -C lumen-runtime-next log --oneline -3
  fd623a5 merge: app-desk-simplify — en macOS la integridad es el sello de Apple …
  fff6563 refactor(macos): drop the per-file cdhash mechanism …
  ecb88f3 fix(cli): machine heartbeat emitted an illegal progress unit (MAC5-01)
```

**`cmd_stage_runtime` (`safent:1944-1953`)** — resolución de ruta exacta:

```sh
bundle_dir="$(CDPATH='' cd -- "$(dirname "$0")" && pwd)"   # …/Safent.app/Contents/Resources/runtime
if [ "$OS" = Darwin ]; then
  app_bundle="${bundle_dir%%/Contents/*}"                  # …/Safent.app   (corta en el PRIMER /Contents/)
  case "$app_bundle" in *.app) ;; *) _die_porcelain runtime_hash_mismatch … ;; esac
  "${SAFENT_CODESIGN:-codesign}" --verify --strict "$app_bundle" >/dev/null 2>&1 \
    || _die_porcelain runtime_hash_mismatch "El paquete no supera codesign --verify --strict" false
fi
```

y el hash por fichero queda **apagado en Darwin** (`safent:1965-1967`):

```sh
if [ "$OS" != Darwin ]; then
  [ "$(_sha256 "$src")" = "$sha" ] || _die_porcelain runtime_hash_mismatch …
fi
```

**Latido de máquina (`safent:1299`)**: `_stage_progress "$_hb_elapsed" "" steps`
— la unidad ilegal `seconds` de MAC5-01 ya no está.

---

## Comprobación 1 — PAQUETE

### 1.1 Descarga y traslado — **PASA**

```
gh api repos/devwspito/agents-autonomy/actions/artifacts/10192531943/zip > f.zip
   -> 2 013 148 446 B   sha256 dd4fda3400c22680afeeab149865a9140bcbf53e989fed9b99d1b325f961d75a   (5 m 33 s)
unzip -l f.zip:
   1 007 253 709  macos/Safent.app.tar.gz
   1 006 944 367  dmg/Safent_0.9.0_aarch64.dmg
             404  macos/Safent.app.tar.gz.sig

scp dmg/Safent_0.9.0_aarch64.dmg luiscorrea@macbook-air-2-1:/tmp/safent-mac-test6/   (1 m 25 s)
sha256 DGX : f6834254dd16165839eee34f45a01e51f7382c6017fec9cd8edc1469194a6204
sha256 Mac : f6834254dd16165839eee34f45a01e51f7382c6017fec9cd8edc1469194a6204   IDÉNTICO
```

### 1.2 DMG: firma, Gatekeeper y grapado — **PASA**

```
xattr -l <dmg>                                                    -> (vacío)
codesign --verify --strict --verbose=2 <dmg>
  -> valid on disk · satisfies its Designated Requirement          rc=0
codesign -dvv <dmg>
  -> Identifier=Safent_0.9.0_aarch64 · Format=disk image
     CodeDirectory v=20200 size=308 flags=0x0(none) hashes=1+6
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Authority=Developer ID Certification Authority · Authority=Apple Root CA
     Timestamp=11 Sep 2026 at 11:12:31
     Notarization Ticket=stapled
     TeamIdentifier=JBMBA58A8X                                     rc=0
spctl -a -vv -t open --context context:primary-signature <dmg>
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)     rc=0
xcrun stapler validate <dmg>  -> The validate action worked!       rc=0
```

### 1.3 Montaje y extracción — **PASA**

```
hdiutil attach -nobrowse -readonly -mountpoint …/mnt <dmg>
  -> "verificado  CRC32 $3DC7E578" · /dev/disk4s1 -> /private/tmp/safent-mac-test6/mnt
  contenido: Safent.app, Applications -> /Applications
ditto mnt/Safent.app …/Safent.app        (0,64 s)
hdiutil detach …/mnt                     -> "disk4" ejected
mount | grep -c safent-mac-test6         -> 0
```

### 1.4 `.app`: **el mandamiento del que ahora depende la app** — **PASA**

```
codesign --verify --strict /tmp/safent-mac-test6/Safent.app
  -> (sin salida)                                                  rc=0     <- ESTE
codesign --verify --strict --verbose=2 /tmp/safent-mac-test6/Safent.app
  -> /tmp/safent-mac-test6/Safent.app: valid on disk
     /tmp/safent-mac-test6/Safent.app: satisfies its Designated Requirement  rc=0
spctl -a -vv -t exec /tmp/safent-mac-test6/Safent.app
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)     rc=0
codesign -dvvv /tmp/safent-mac-test6/Safent.app
  -> Identifier=com.safent.desktop · Format=app bundle with Mach-O thin (arm64)
     CodeDirectory v=20500 size=33662 flags=0x10000(runtime) hashes=1045+3
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Timestamp=11 Sep 2026 at 11:11:39 · TeamIdentifier=JBMBA58A8X · Runtime 14.5.0
     Sealed Resources version=2 rules=13 files=19                  rc=0
xcrun stapler validate /tmp/safent-mac-test6/Safent.app
  -> Safent.app does not have a ticket stapled to it.              rc=65
```

El `.app` **no está grapado** — **aceptado por encargo** (Tauri estándar: se grapa
el DMG, que es lo que viaja con cuarentena). Lo que importa está probado por
separado: `spctl -t exec` lo da **`accepted · Notarized Developer ID`** (el
registro de notarización existe) y `codesign --verify --strict` devuelve **rc=0**,
que es exactamente la llamada que `cmd_stage_runtime` ejecuta en cada arranque.
`Sealed Resources … files=19` confirma que el sello de Apple cubre los 18 ficheros
de `Contents/Resources/runtime` + el ejecutable: **una sola llamada cubre todo el
paquete**, que es la premisa del cambio.

### 1.5 `runtime-bundle.json` **sin `cdhash`** — **PASA**

```
ls -1 …/Contents/Resources/runtime | wc -l     -> 18   (17 entradas + el manifiesto)
wc -c < runtime-bundle.json                    -> 2976   (era 3654 con cdhash)
grep -c cdhash runtime-bundle.json             -> 0        rc=1   <- NINGUNA
grep -c '"path":' runtime-bundle.json          -> 17
podman_version                                 -> 6.1.1
sha256 de imágenes en el manifiesto:
  sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3   (motor, rc2)
  sha256:54c4fee8535d750c027ae2976984feef9884c7f11f7702894f410e03161264b9   (compañero)
```

El manifiesto conserva `path` + `sha256` + `mode` por entrada (lo que sigue usando
Linux) y **ha perdido el campo `cdhash` por completo**. Con ello desaparece de
raíz la causa de MAC4-01 y de MAC5-02 (el manifiesto caducado del canal de
actualización ya no puede desajustarse, porque no hay nada que casar en macOS).

---

## Comprobación 2 — ARRANQUE EN FRÍO (`--selftest`, sin compañero)

```
/tmp/safent-mac-test6/Safent.app/Contents/MacOS/safent-desktop --selftest
  con SAFENT_STATE_HOME=/tmp/safent-mac-test6/state  SAFENT_NAME=mactest6
  NDJSON con marca relativa (perl -MTime::HiRes), stderr a fichero aparte
  START 11:28:02   END 11:30:21
```

### Transcripción NDJSON íntegra — **rc=0**

```
   7.106 {"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
   7.538 {"kind":"progress","stage":"runtime_staging","done":1,"total":17,"unit":"steps"}
   7.549 {"kind":"progress","stage":"runtime_staging","done":2,"total":17,"unit":"steps"}
   7.560 {"kind":"progress","stage":"runtime_staging","done":3,"total":17,"unit":"steps"}
   7.690 {"kind":"progress","stage":"runtime_staging","done":4,"total":17,"unit":"steps"}
   7.715 {"kind":"progress","stage":"runtime_staging","done":5,"total":17,"unit":"steps"}
   7.727 {"kind":"progress","stage":"runtime_staging","done":6,"total":17,"unit":"steps"}
   7.744 {"kind":"progress","stage":"runtime_staging","done":7,"total":17,"unit":"steps"}
   8.018 {"kind":"progress","stage":"runtime_staging","done":8,"total":17,"unit":"steps"}
   8.035 {"kind":"progress","stage":"runtime_staging","done":9,"total":17,"unit":"steps"}
   8.047 {"kind":"progress","stage":"runtime_staging","done":10,"total":17,"unit":"steps"}
   8.060 {"kind":"progress","stage":"runtime_staging","done":11,"total":17,"unit":"steps"}
   8.072 {"kind":"progress","stage":"runtime_staging","done":12,"total":17,"unit":"steps"}
   8.083 {"kind":"progress","stage":"runtime_staging","done":13,"total":17,"unit":"steps"}
   8.093 {"kind":"progress","stage":"runtime_staging","done":14,"total":17,"unit":"steps"}
   8.104 {"kind":"progress","stage":"runtime_staging","done":15,"total":17,"unit":"steps"}
   8.115 {"kind":"progress","stage":"runtime_staging","done":16,"total":17,"unit":"steps"}
   8.126 {"kind":"progress","stage":"runtime_staging","done":17,"total":17,"unit":"steps"}
   8.127 {"kind":"done","stage":"runtime_staging","ms":1000}
  10.166 {"kind":"stage","stage":"machine","label":"Preparando la maquina","total_bytes":null,"point_of_no_return":false}
  15.227 {"kind":"progress","stage":"machine","done":5,"total":null,"unit":"steps"}
  24.368 {"kind":"progress","stage":"machine","done":5,"total":null,"unit":"steps"}
  29.419 {"kind":"progress","stage":"machine","done":10,"total":null,"unit":"steps"}
  34.753 {"kind":"done","stage":"machine","ms":25000}
  35.128 {"kind":"stage","stage":"pull_engine","label":"Descargando Safent","total_bytes":null,"point_of_no_return":false}
  39.239 {"kind":"progress","stage":"pull_engine","done":1,"total":null,"unit":"steps"}
  43.253 {"kind":"progress","stage":"pull_engine","done":2,"total":null,"unit":"steps"}
  47.268 {"kind":"progress","stage":"pull_engine","done":3,"total":null,"unit":"steps"}
  51.284 {"kind":"progress","stage":"pull_engine","done":4,"total":null,"unit":"steps"}
  55.299 {"kind":"progress","stage":"pull_engine","done":5,"total":null,"unit":"steps"}
  59.314 {"kind":"progress","stage":"pull_engine","done":6,"total":null,"unit":"steps"}
  63.330 {"kind":"progress","stage":"pull_engine","done":7,"total":null,"unit":"steps"}
  67.347 {"kind":"progress","stage":"pull_engine","done":8,"total":null,"unit":"steps"}
  71.361 {"kind":"progress","stage":"pull_engine","done":9,"total":null,"unit":"steps"}
  75.374 {"kind":"progress","stage":"pull_engine","done":10,"total":null,"unit":"steps"}
  79.386 {"kind":"progress","stage":"pull_engine","done":11,"total":null,"unit":"steps"}
  83.406 {"kind":"progress","stage":"pull_engine","done":12,"total":null,"unit":"steps"}
  87.424 {"kind":"progress","stage":"pull_engine","done":13,"total":null,"unit":"steps"}
  91.456 {"kind":"progress","stage":"pull_engine","done":14,"total":null,"unit":"steps"}
  95.496 {"kind":"progress","stage":"pull_engine","done":15,"total":null,"unit":"steps"}
  99.520 {"kind":"progress","stage":"pull_engine","done":16,"total":null,"unit":"steps"}
 103.536 {"kind":"progress","stage":"pull_engine","done":17,"total":null,"unit":"steps"}
 107.568 {"kind":"progress","stage":"pull_engine","done":18,"total":null,"unit":"steps"}
 111.589 {"kind":"progress","stage":"pull_engine","done":19,"total":null,"unit":"steps"}
 115.612 {"kind":"progress","stage":"pull_engine","done":20,"total":null,"unit":"steps"}
 119.632 {"kind":"progress","stage":"pull_engine","done":21,"total":null,"unit":"steps"}
 123.647 {"kind":"progress","stage":"pull_engine","done":22,"total":null,"unit":"steps"}
 127.671 {"kind":"progress","stage":"pull_engine","done":23,"total":null,"unit":"steps"}
 131.733 {"kind":"done","stage":"pull_engine","ms":97000}
 132.152 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
 132.968 {"kind":"done","stage":"container","ms":1000}
 132.973 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
 133.099 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
 138.742 {"kind":"done","stage":"health","ms":6000}
 138.845 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3","companion_digest":null}
rc=0        stderr: 0 bytes
```

**MAC5-01 cerrado.** El latido de `machine` sale ahora con `"unit":"steps"`
(líneas 15,227 · 24,368 · 29,419), el envoltorio lo acepta, y la etapa **cierra
con un único `done`** (`ms:25000`). No hay `cli_porcelain_unsupported`. Con ello
**MAC5-04 también desaparece**: una sola apertura y un solo cierre por etapa,
las cinco etapas en orden — `runtime_staging` → `machine` → `pull_engine` →
`container` → `health` → `ready`.

### Duraciones por etapa

| Etapa | Entra | Cierra | Duración real | `ms` declarado | Avances |
|---|---|---|---|---|---|
| `runtime_staging` | 7,106 | 8,127 | **1,021 s** | 1000 | 17/17 |
| `machine` | 10,166 | 34,753 | **24,587 s** | 25000 | 3 latidos |
| `pull_engine` | 35,128 | 131,733 | **96,605 s** | 97000 | 23 latidos |
| `container` | 132,152 | 132,968 | **0,816 s** | 1000 | — |
| `health` | 132,973 | 138,742 | **5,769 s** | 6000 | 1 |
| `ready` | — | 138,845 | — | — | digest rc2, `companion_digest:null` |
| **Total a `ready`** | | | **138,845 s** | | rc=0 |

### Ninguna ventana muda > 15 s — **PASA**

Huecos, de mayor a menor: **9,141 s** (`machine`, entre los dos primeros latidos)
· 7,106 s (lanzamiento del `.app` hasta la primera línea) · 5,769/5,643 s
(`health`) · 5,334 s y 5,061 s (`machine`) · 4,111 s máx. entre latidos de
`pull_engine` · 2,039 s (`runtime_staging` → `machine`). **Máximo 9,141 s**, muy
por debajo del límite de 15 s, y el peor hueco vive dentro de una etapa que
**narra**, no en silencio.

### `runtime_staging` sin hash por fichero — **PASA**

**1,021 s** para desplegar 17 ficheros (≈1,05 GB), frente a **1,26 s** en la
verificación 5. La prueba no es sólo el total, es **dónde** se ha ido el tiempo:
la entrada nº 8 es `podman-machine.aarch64.applehv.raw.zst`, de **931 934 236 B**,
el único fichero grande del paquete.

| | verificación 5 (con sha256 por fichero) | verificación 6 (sello de Apple) |
|---|---|---|
| paso 7 → paso 8 (el fichero de 932 MB) | 2,410 → 3,098 = **0,688 s** | 7,744 → 8,018 = **0,274 s** |
| `runtime_staging` completa | **1,26 s** | **1,021 s** |
| campo `cdhash` en el manifiesto | 8 no nulos | **no existe el campo** |

El coste del hash de 932 MB (≈0,4-0,5 s en este hardware) **ha desaparecido**; lo
que queda es el `cp` (que en APFS clona, de ahí lo rápido) más un único
`codesign --verify --strict` sobre el `.app`. Concuerda con el código:
`safent:1965` sólo entra a `_sha256` cuando `$OS != Darwin`.

### `/healthz` desde el host macOS — **PASA**

```
$RT machine list -> mactest6-engine  applehv  Currently running   (nombre real, verificado)
$RT ps           -> 12797d0a533f  mactest6  Up About a minute  127.0.0.1:38121->7517/tcp
                    ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3
curl -sS -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:38121/healthz  -> 200 200 200
curl -sS http://127.0.0.1:38121/healthz
  -> {"status":"ok","service":"hermes-shell-server","version":"0.9.0","ts":"2026-09-11T09:32:24.769753+00:00"}
lsof -nP -iTCP:38121 -> gvproxy 7950 … 127.0.0.1:38121 (LISTEN)
```

### gvproxy/vfkit en ejecución = los EMPAQUETADOS — **PASA**

```
ps -o args= -p 7950
  /private/tmp/safent-mac-test6/Safent.app/Contents/Resources/runtime/gvproxy -mtu 1500
  -ssh-port 62290 -listen-vfkit unixgram:///tmp/safent-mac-test6/tmp/podman/mactest6-engine-gvproxy.sock …
ps -o args= -p 7952
  /private/tmp/safent-mac-test6/Safent.app/Contents/Resources/runtime/vfkit --cpus 4 --memory 8192
  --bootloader efi,variable-store=/tmp/safent-mac-test6/xdg-data/containers/podman/machine/applehv/efi-bl-mactest6-engine,create …
```

Los dos helpers salen **de dentro del `.app`**, no de `/opt/podman`, y el socket
de gvproxy lleva el nombre de **nuestra** máquina, así que no pisa el del dueño.
(El `variable-store` bajo `xdg-data` es del **arnés**, no del producto: MAC5-05.)

### La URL `?k=` que abriría la app — **PASA**

Vale obtenido por el descriptor 3, como manda `app-engine.md` §5, con la imagen
fijada al digest:

```
SAFENT_IMAGE=ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3 \
  $APP/Contents/Resources/runtime/safent --no-companion up --secret-fd 3 --porcelain 3>ticket
  {"t":"stage","id":"container","label":"Creando el contenedor"}
  {"t":"done","id":"container","ms":0}                     <- no toca el motor sano
  {"t":"stage","id":"health","label":"Esperando a que Safent este listo"}
  {"t":"done","id":"health","ms":0}
  {"t":"ready","endpoint_ref":"stdout-secret"}
rc=0
ANTES 95ffe3f0e3ed/44145  ->  DESPUES 95ffe3f0e3ed/44145   (ni id ni puerto se mueven)

ticket: 1 línea, 91 bytes -> http://127.0.0.1:44145/?k=<VALE>     (nunca por stdout)
curl -sS  '…/?k=<VALE>'   -> http=307  redirect=http://127.0.0.1:44145/app/?k=<VALE>
curl -sSL '…/?k=<VALE>'   -> http=200  bytes=1555
  HTML: <script>window.__SAFENT_TOKEN__="f0c35325…";</script>     <- bearer inyectado
  el vale NO aparece literal en el HTML (se canjea, no se refleja): grep -c -> 0
curl 'http://127.0.0.1:44145/app/'  (sin vale) -> http=200 bytes=1488, sin token
```

---

## Comprobación 3 — REAPERTURA (una sola vez) — **PASA**

```
ANTES:   id=95ffe3f0e3ed  puerto=44145  estado=Up 19 seconds
$APP/Contents/MacOS/safent-desktop --selftest
   0.948 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
   1.412 {"kind":"done","stage":"container","ms":0}
   1.416 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
   1.729 {"kind":"done","stage":"health","ms":1000}
   1.809 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6…dbf8a3","companion_digest":null}
DESPUES: id=95ffe3f0e3ed  puerto=44145  estado=Up 21 seconds     stderr: 0 bytes
```

**Mismo contenedor, mismo puerto**, el «Up» no se reinicia (19 s → 21 s: el
contenedor siguió vivo) y `ready` en **1,809 s**. `container` cierra con `ms:0`:
el reconciliador ve el motor sano y no lo toca. MAC4-02 se sostiene.

---

## Comprobación 4 — MÁQUINA DEL DUEÑO Y DESMONTAJE — **PASA**

Nombres reales verificados **antes** de borrar nada:

```
contenedor  95ffe3f0e3ed  mactest6  ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3
volumen     mactest6-data
imágenes    ghcr.io/devwspito/safent  365e584d7f5c  8.17 GB
            ghcr.io/devwspito/safent  7d40881ed70a  8.11 GB   (ver MAC6-01)
máquina     mactest6-engine  applehv  running
redes       podman            (no se creó safent-companions: todo fue --no-companion)

$RT rm -f mactest6                 -> mactest6
$RT volume rm -f mactest6-data     -> mactest6-data
$RT rmi -f 365e584d7f5c 7d40881ed70a -> Deleted: 365e584d…  Deleted: 7d40881e…
$RT machine stop mactest6-engine   -> Machine "mactest6-engine" stopped successfully
$RT machine rm -f mactest6-engine  -> (sin máquinas en el sandbox)
du -sh /tmp/safent-mac-test6       -> 3,0 G      ->  rm -rf  ->  No such file or directory
```

Comprobaciones finales (11:39):

```
mount | grep -c safent-mac-test6        -> 0
hdiutil info | grep -c safent-mac-test6 -> 0
pgrep -fl "safent-mac-test6|mactest6"   -> 0
pgrep -fl "podman run"                  -> 0
ls -d /tmp/safent-mac-test*             -> no matches found
```

| Medida | Antes (11:19) | Después (11:39) |
|---|---|---|
| `podman-machine-default` | libkrun · running | libkrun · **running** |
| `krunkit` (PID) | 20043 | **20043** |
| `gvproxy` del dueño (PID) | 20042 | **20042** |
| `~/.config/containers` (mtime) | 2026-07-16T11:02:28 | **igual** |
| `~/.local/share/containers` | 2026-07-16T11:01:56 | **igual** |
| `/opt/podman` | 2026-07-08T22:08:46 | **igual** |
| `~/.safent` | no existe | **no existe** |
| `…/machine/applehv` | sólo `cache` | **sólo `cache`** |
| `podman run` huérfanos | 0 | **0** |

Los **mismos PID** al empezar y al terminar: la máquina del dueño ni se reinició
ni se tocó. Espacio libre 91 Gi → 85 Gi; el sandbox está borrado y verificado
inexistente, y `/private/tmp` contiene ficheros de **otros** proyectos, ajenos a
esta prueba.

---

## Hallazgos

### MAC6-01 · OBSERVACIÓN (no es defecto del artefacto) · invocar el CLI sin `SAFENT_IMAGE` corre `:latest`

Durante la prueba se invocó el CLI empaquetado **a pelo**, sin `SAFENT_IMAGE`.
`safent:25` fija `IMAGE="${SAFENT_IMAGE:-ghcr.io/devwspito/safent:latest}"`, y
`_container_matches_desired` (`safent:2151-2160`) exige un digest:

```sh
case "$IMAGE" in *@sha256:*) ;; *) return 1 ;; esac
```

Con una **etiqueta** nunca casa → el CLI **destruyó el contenedor sano** y
descargó y arrancó `ghcr.io/devwspito/safent:latest`
(`sha256:f3ad64079c22fa8e9d77cfd9876633dd876371e7b19fd25b5ceb052f5321c179`,
**otra imagen distinta** de la fijada), 106 s de descarga, y terminó en
`{"t":"failed","id":"health","code":"daemon_unhealthy","detail":"El puerto
publicado no responde desde este equipo","retryable":true}` (rc=24), con el
puerto publicado colgado desde el host aunque el daemon respondía 200 dentro del
contenedor y dentro de la VM.

**Es un error del arnés, no del producto**: la app de escritorio **siempre** pasa
el digest fijado, y al volver a lanzarla detectó la imagen intrusa, reconcilió al
digest correcto y llegó a `ready` en **13,523 s**, con `/healthz` 200 desde el
host en el puerto nuevo. Se documenta por dos motivos: (a) el fallo se **narró
bien** y con `retryable:true`, que es la conducta correcta; (b) el `:latest` del
registro **no** es el `v0.9.0-rc2` que se publica — quien depure con el CLI a
mano debe exportar `SAFENT_IMAGE`. Mejora sugerida (no bloqueante): que el CLI
resuelva el digest desde `runtime-bundle.json` cuando exista, en vez de caer a
`:latest`.

### MAC5-03 · MENOR (sigue) · el contador de latidos se reinicia dentro de la misma etapa

`machine` → `done:5` (15,227) · `done:5` (24,368) · `done:10` (29,419). Siguen
siendo **dos** llamadas a `_run_with_heartbeat` (`machine init` y `machine start`,
`safent:2033`/`2054`), cada una con su `_hb_elapsed` desde cero, dentro de la
**misma** etapa. Una barra que no retrocede pero se repite. Cosmético; se cierra
si el acumulador vive en la etapa y no en la llamada.

### MAC5-05 (MAC4-08) · MENOR (sigue) · el producto no aísla su podman del del dueño

`grep -n "XDG_DATA_HOME\|XDG_CONFIG_HOME" safent` → sin coincidencias. El
`variable-store` de vfkit apunta a `/tmp/safent-mac-test6/xdg-data/…` **porque la
prueba lo redirigió**; sin ese arnés la máquina nacería en
`~/.local/share/containers` del dueño, junto a la suya. No reproducido a
propósito: las reglas de la prueba lo prohíben.

### Cerrados en esta verificación

| Hallazgo | Estado | Evidencia |
|---|---|---|
| **MAC5-01** · `cli_porcelain_unsupported` en Mac limpio | **CERRADO** | arranque en frío **de caja** hasta `ready`, rc=0; latidos de `machine` con `"unit":"steps"` |
| **MAC5-02** · manifiesto caducado del canal de actualización | **CERRADO por construcción** | ya no existe el campo `cdhash` (`grep -c cdhash` → 0); en macOS no se comparan hashes por fichero |
| **MAC5-04** · etapa abierta dos veces sin cerrar | **CERRADO** | `machine` abre una vez y cierra con un único `done ms:25000` |
| **MAC4-01/02/03/06** | **se sostienen** | staging limpio sin arnés; reapertura con mismo id/puerto; `ensure-machine` rc=0 con helpers propios; DMG notarizado y grapado |

## Lo que no se pudo simular

- **Todo lo gráfico**: ventana, etapas pintadas, «Cancelar», barra de menús,
  instancia única, `Cmd+L`, Anuncios. No hay pantalla por SSH.
- **El doble clic con cuarentena**: se midió el veredicto de `spctl`/`stapler`
  sobre el DMG, no el diálogo de Gatekeeper sobre un DMG bajado por Safari.
  Con el `.app` **sin grapar**, el primer arranque tras instalar depende de la
  consulta en línea a Apple (o del ticket del DMG); `spctl -t exec` la resolvió
  **`accepted`** en esta máquina, con red.
- **El compañero (029)**: `--selftest` sin compañero, por encargo. MAC4-04 sigue
  sin ejercitarse.
- **El actualizador de punta a punta** (US2): requiere una versión nueva publicada.
- **Un helper ajeno de verdad**: habría exigido tocar `/opt/podman` o el PATH del dueño.

## Siguiente acción recomendada

1. **Publicar.** Las cuatro comprobaciones del encargo pasan y no queda ningún
   bloqueante abierto.
2. Después de publicar, sin prisa: MAC5-05 (aislar `XDG_*` desde el producto),
   MAC5-03 (acumulador de latidos por etapa) y MAC6-01 (que el CLI resuelva el
   digest del manifiesto en vez de caer a `:latest`).
3. **MAC4-04** sigue sin ejercitarse: comprobar el carril del compañero (029).
