# Verificación 3 — app nativa de macOS en el MacBook del dueño

**Qué**: tercera prueba real del artefacto notarizado `Safent-macOS-arm64` del run
**34558270518** (`devwspito/agents-autonomy`, rama `safent-desktop-pipeline-028`,
`986a0f43`; el run figura en rojo por otro job), versión 0.9.0, con el motor
fijado al digest `sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3`
(= `ghcr.io/devwspito/safent:v0.9.0-rc2`, público) y el compañero
`safent-ads` a `sha256:54c4fee8535d750c027ae2976984feef9884c7f11f7702894f410e03161264b9`.
**Dónde**: MacBook Air del dueño (`macbook-air-2-1`), macOS 15.5 (24F74), arm64,
24 GB de RAM, 77 GiB libres. Por SSH, **sin sesión gráfica**.
**Cuándo**: 11-sep-2026, 05:40–05:55 (hora local del Mac). **Quién**: exploratory-tester.
**Alcance**: paquete, arranque **en frío** hasta `ready`, la comprobación decisiva
(`/healthz` **desde macOS**), reapertura con el motor sano, reanudación tras matar
el contenedor y convivencia con el podman del dueño.

## Veredicto

**El bloqueante del release está resuelto: el puerto publicado responde desde
macOS.** El arranque en frío llega a `ready` **entero y solo**, en 100,6 s, con
todas las etapas en orden y `pull_engine` narrado con latidos. De los cinco
defectos que el encargo daba por arreglados, **cuatro se confirman en vivo** y
**uno no**: reabrir la app **sigue destruyendo y recreando** el motor sano, por
una causa nueva y concreta (MAC3-02). Y aparece una **regresión de empaquetado**:
el DMG de este run **ya no está firmado** (MAC3-01).

| Comprobación del encargo | Resultado |
|---|---|
| (1) Paquete: sha256 en los dos extremos | **PASA** |
| (1) `stapler validate` del DMG | **PASA con salvedad** (valida **en línea**, ver MAC3-01) |
| (1) `stapler validate` del `.app` | **FALLA** (MAC3-04 — sigue sin grapar) |
| (1) `spctl -a -vv -t exec` del `.app` | **PASA** |
| (1) `codesign --verify --strict` del `.app` | **PASA** |
| (1) `spctl`/`codesign` del **DMG** | **FALLA** (MAC3-01 — **regresión**: DMG sin firma) |
| (1) 15 entradas + `runtime-bundle.json` | **PASA** (15/15 sha256 y 15/15 modo) |
| (1) ¿`cdhash` poblados? | **NO** (15/15 `null` — MAC3-05, era MAC2-08) |
| (1) Digest del motor = rc2 | **PASA** (`sha256:9bafbad6…dbf8a3`) |
| (2) Arranque en frío: etapas en orden con avance | **PASA** |
| (2) `pull_engine` como etapa propia, con latidos, sin silencio > 15 s | **PASA** (17 latidos, hueco máximo **4,06 s**) |
| (2) **`curl /healthz` desde el HOST macOS → 200** | **PASA** (200/200/200) |
| (2) La URL `?k=` responde desde el host y el HTML inyecta el bearer | **PASA** (307 → 200, `window.__SAFENT_TOKEN__`) |
| (3) Reapertura sin destruir el contenedor (mismo id, mismo puerto) | **FALLA** (MAC3-02) |
| (4) Reanudación tras `podman kill`, desatendida | **PASA** (`ready` en 7,2 s) |
| (5) `podman-machine-default` intacta + sin `podman run` huérfanos | **PASA** |
| Desmontaje completo | **PASA** |

## Reglas de la prueba y arnés prestado

Todo bajo `/tmp/safent-mac-test3/` (`/tmp` → `private/tmp` en macOS) con
`SAFENT_NAME=mactest3` → contenedor `mactest3`, volumen `mactest3-data`,
**máquina `mactest3-engine`** (nombre real verificado con `machine list`, nunca
asumido). Igual que en las verificaciones 1 y 2, se añadió **desde fuera** un
arnés que el producto **no trae**: `XDG_CONFIG_HOME`, `XDG_DATA_HOME` y `TMPDIR`
dentro del sandbox (MAC2-14 sigue vigente). Comprobado **antes** de empezar:

```
$RT machine list   -> (vacío)
$RT machine info   -> machineconfigdir: /private/tmp/safent-mac-test3/xdg/config/containers/podman/machine/libkrun
                      machineimagedir:  /private/tmp/safent-mac-test3/xdg/data/containers/podman/machine/libkrun
                      numberofmachines: 0
$RT --version      -> podman version 6.1.1      (el del dueño es 6.0.1)
```

Nunca se ejecutó `tailscale`. Nunca se tocó `/opt/podman`, `~/.config/containers`,
`~/.local/share/containers`, `~/.ssh`, `/Applications` ni `podman-machine-default`.
Sin `SAFENT_ENGINE_DIGEST` ni `SAFENT_RUNTIME_DIR`: se exige que el paquete se
baste solo.

---

## Paso 1 — Descarga, traslado y verificación del paquete

### 1.1 Descarga (DGX) — **PASA**

```
gh api repos/devwspito/agents-autonomy/actions/artifacts/10183585819/zip > f.zip
   -> 2 017 907 687 B   sha256 4f553f9415d8c4c84bfb9834f9a218bc52815ac3a4e4f142e1d01610540ef3f9
unzip -l:
   1 007 235 134  macos/Safent.app.tar.gz
   1 013 431 840  dmg/Safent_0.9.0_aarch64.dmg
             404  macos/Safent.app.tar.gz.sig
```

### 1.2 Traslado — **PASA**

```
scp dmg/Safent_0.9.0_aarch64.dmg luiscorrea@macbook-air-2-1:/tmp/safent-mac-test3/   (7,5 s)
sha256 DGX  : 0368bdb023276a722806600039d87751ff2b510aadcada2e6ccb831aade963ff
sha256 Mac  : 0368bdb023276a722806600039d87751ff2b510aadcada2e6ccb831aade963ff   IDÉNTICO
```

### 1.3 Grapado, firma y notarización — **FALLA en el DMG, PASA en el `.app`**

```
xcrun stapler validate Safent_0.9.0_aarch64.dmg
  -> The validate action worked!                                        rc=0
xcrun stapler validate -v Safent_0.9.0_aarch64.dmg   (cola)
  -> recordType = DeveloperIDTicket
     Downloaded ticket has been stored at file:///var/folders/…/….ticket   <- validó EN LÍNEA
spctl -a -vv -t open --context context:primary-signature <dmg>
  -> rejected · source=no usable signature                               rc=3   <- MAC3-01
spctl -a -vv -t open <dmg>
  -> rejected · source=Insufficient Context                              rc=3
codesign -dvv <dmg>
  -> code object is not signed at all                                    rc=1   <- MAC3-01
codesign --verify --strict --verbose=2 <dmg>
  -> code object is not signed at all                                    rc=1
xattr -l <dmg>                                                           -> (vacío)

hdiutil attach -nobrowse -readonly -mountpoint /tmp/safent-mac-test3/mnt <dmg>
  -> /dev/disk5s1 montado; contenido: Safent.app, Applications -> /Applications
ditto mnt/Safent.app /tmp/safent-mac-test3/Safent.app                    (0,9 s)
hdiutil detach                                                           -> "disk4" ejected

spctl -a -vv -t exec Safent.app
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)           rc=0
codesign --verify --strict --verbose=2 Safent.app   -> valid on disk · satisfies its DR   rc=0
codesign --verify --deep --strict --verbose=2 Safent.app                 rc=0
codesign -dvvv Safent.app
  -> Identifier=com.safent.desktop · Format=app bundle with Mach-O thin (arm64)
     CodeDirectory v=20500 size=33662 flags=0x10000(runtime) hashes=1045+3
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Timestamp=11 Sep 2026 at 05:29:14 · TeamIdentifier=JBMBA58A8X · Runtime Version=14.5.0
xcrun stapler validate Safent.app
  -> Safent.app does not have a ticket stapled to it.                    rc=65  <- MAC3-04
```

En la verificación 2 (run 34549128789) el DMG daba `accepted · source=Notarized
Developer ID` y `codesign --verify` rc=0. **Este DMG ha perdido la firma.**

### 1.4 `runtime-bundle.json` y las 15 entradas — **PASA (15/15)**

```
podman_version : 6.1.1
entries        : 15          cdhash no nulos: 0        <- MAC3-05
engine_image   : {"repo":"ghcr.io/devwspito/safent",     "digest":"sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3","platform":"linux/arm64"}
companion_image: {"repo":"ghcr.io/devwspito/safent-ads","digest":"sha256:54c4fee8535d750c027ae2976984feef9884c7f11f7702894f410e03161264b9","platform":"linux/arm64"}
```

**El digest del motor coincide exactamente con el rc2 del encargo.**

| Fichero | manifiesto (sha256) | real | modo | cdhash | `codesign --verify` | veredicto |
|---|---|---|---|---|---|---|
| provision.sh | eeef14235363da | = | 0755 | null | n/a | OK |
| caps.template.yaml | 1de55a12095a22 | = | 0644 | null | n/a | OK |
| podman | 129f6e047d2d70 | = | 0755 | **null** | rc=0 (CDHash ea3733d4…) | OK |
| vfkit | 41c48ca35eda0c | = | 0755 | **null** | rc=0 (CDHash b2817b0b…) | OK |
| libkrun.dylib | 59a6f1b1a7497f | = | 0644 | **null** | rc=0 (CDHash aed38420…) | OK |
| libepoxy.0.dylib | f89874992fd853 | = | 0644 | **null** | rc=0 (CDHash c4b2c942…) | OK |
| podman-machine.aarch64.applehv.raw.zst | b71b8a4e95a440 | = | 0644 | null | n/a | OK |
| krunkit | 1af31c49e6d9ae | = | 0755 | **null** | rc=0 (CDHash d039e2e7…) | OK |
| KRUN_EFI.silent.fd | 9ba725c245f634 | = | 0644 | null | n/a | OK |
| gvproxy | f8766f3a3907e5 | = | 0755 | **null** | rc=0 (CDHash e946b4d0…) | OK |
| libMoltenVK.dylib | a9a5f0e64e2856 | = | 0644 | **null** | rc=0 (CDHash 70a4304c…) | OK |
| compose.yaml | 502fd5789a8786 | = | 0644 | null | n/a | OK |
| run-safent.sh | b2a2b45624ac67 | = | 0755 | null | n/a | OK |
| safent | 4855ad52fc0026 | = | 0755 | null | n/a | OK |
| libvirglrenderer.1.dylib | 6804be4676641d | = | 0644 | **null** | rc=0 (CDHash 92b22cda…) | OK |

`Contents/Resources/runtime/` trae **16** ficheros (los 15 + `runtime-bundle.json`):
**sigue sin `containers.conf`** (MAC2-13 → consecuencia medida en MAC3-07).

---

## Paso 2 — Arranque en frío (`--selftest`, sin compañero)

Binario: `/tmp/safent-mac-test3/Safent.app/Contents/MacOS/safent-desktop --selftest`.
NDJSON capturado en vivo con marca de tiempo relativa (perl, `$|=1`).

### Pase 1 — con `SAFENT_STATE_HOME=/tmp/safent-mac-test3/state` **literal** (**FALLA**)

Sandbox limpio: `machines: []`, `state: []`.

```
   2.078 {"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
   2.103 {"kind":"progress","stage":"runtime_staging","done":1,"total":15,"unit":"steps"}
   2.117 {"kind":"progress","stage":"runtime_staging","done":2,"total":15,"unit":"steps"}
   2.160 {"kind":"progress","stage":"runtime_staging","done":3,"total":15,"unit":"steps"}
   2.222 {"kind":"progress","stage":"runtime_staging","done":4,"total":15,"unit":"steps"}
   2.240 {"kind":"progress","stage":"runtime_staging","done":5,"total":15,"unit":"steps"}
   2.255 {"kind":"progress","stage":"runtime_staging","done":6,"total":15,"unit":"steps"}
   3.050 {"kind":"progress","stage":"runtime_staging","done":7,"total":15,"unit":"steps"}
   3.072 {"kind":"progress","stage":"runtime_staging","done":8,"total":15,"unit":"steps"}
   3.090 {"kind":"progress","stage":"runtime_staging","done":9,"total":15,"unit":"steps"}
   3.132 {"kind":"progress","stage":"runtime_staging","done":10,"total":15,"unit":"steps"}
   3.154 {"kind":"progress","stage":"runtime_staging","done":11,"total":15,"unit":"steps"}
   3.166 {"kind":"progress","stage":"runtime_staging","done":12,"total":15,"unit":"steps"}
   3.179 {"kind":"progress","stage":"runtime_staging","done":13,"total":15,"unit":"steps"}
   3.193 {"kind":"progress","stage":"runtime_staging","done":14,"total":15,"unit":"steps"}
   3.209 {"kind":"progress","stage":"runtime_staging","done":15,"total":15,"unit":"steps"}
   3.211 {"kind":"done","stage":"runtime_staging","ms":1000}
   5.260 {"kind":"stage","stage":"machine","label":"Preparando la maquina","total_bytes":null,"point_of_no_return":false}
  25.843 {"kind":"done","stage":"machine","ms":20000}
  26.184 {"kind":"stage","stage":"pull_engine","label":"Descargando Safent","total_bytes":null,"point_of_no_return":false}
  30.213 {"kind":"progress","stage":"pull_engine","done":1,"total":null,"unit":"steps"}
  … (17 latidos, uno cada 4,0 s) …
  94.506 {"kind":"progress","stage":"pull_engine","done":17,"total":null,"unit":"steps"}
  95.535 {"kind":"done","stage":"pull_engine","ms":69000}
  95.927 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
  99.007 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
  99.532 {"kind":"failed","code":"daemon_unhealthy","detail":"Error: opening seccomp profile failed: open /tmp/safent-mac-test3/state/safent-seccomp.json: no such file or directory","retryable":true}
```

**MAC2-07 reproducido tal cual** (MAC3-03): el fichero **existe en el host**
(19 766 B, escrito un segundo antes); quien no lo ve es el podman **de dentro de
la VM**. Se comprobó además que la etapa `container` se abre **dos veces sin
cierre** (MAC3-08, era MAC2-09). Al terminar: **cero** `podman run` huérfanos
(MAC2-04 arreglado y verificado). Se repitió el pase con la **misma carpeta** en
su forma canónica (`/private/tmp/safent-mac-test3/state`), tras borrar máquina,
imagen y estado, para medir un arranque en frío de verdad.

### Pase 2 — arranque en frío completo (**PASA**, rc=0)

Sandbox limpio otra vez: `machines: []`, `state: []`, imagen borrada.

```
   2.081 {"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
   2.105 {"kind":"progress","stage":"runtime_staging","done":1,"total":15,"unit":"steps"}
   2.118 {"kind":"progress","stage":"runtime_staging","done":2,"total":15,"unit":"steps"}
   2.159 {"kind":"progress","stage":"runtime_staging","done":3,"total":15,"unit":"steps"}
   2.218 {"kind":"progress","stage":"runtime_staging","done":4,"total":15,"unit":"steps"}
   2.237 {"kind":"progress","stage":"runtime_staging","done":5,"total":15,"unit":"steps"}
   2.253 {"kind":"progress","stage":"runtime_staging","done":6,"total":15,"unit":"steps"}
   2.977 {"kind":"progress","stage":"runtime_staging","done":7,"total":15,"unit":"steps"}
   2.995 {"kind":"progress","stage":"runtime_staging","done":8,"total":15,"unit":"steps"}
   3.010 {"kind":"progress","stage":"runtime_staging","done":9,"total":15,"unit":"steps"}
   3.041 {"kind":"progress","stage":"runtime_staging","done":10,"total":15,"unit":"steps"}
   3.059 {"kind":"progress","stage":"runtime_staging","done":11,"total":15,"unit":"steps"}
   3.072 {"kind":"progress","stage":"runtime_staging","done":12,"total":15,"unit":"steps"}
   3.085 {"kind":"progress","stage":"runtime_staging","done":13,"total":15,"unit":"steps"}
   3.098 {"kind":"progress","stage":"runtime_staging","done":14,"total":15,"unit":"steps"}
   3.113 {"kind":"progress","stage":"runtime_staging","done":15,"total":15,"unit":"steps"}
   3.115 {"kind":"done","stage":"runtime_staging","ms":1000}
   5.167 {"kind":"stage","stage":"machine","label":"Preparando la maquina","total_bytes":null,"point_of_no_return":false}
  23.812 {"kind":"done","stage":"machine","ms":19000}
  24.247 {"kind":"stage","stage":"pull_engine","label":"Descargando Safent","total_bytes":null,"point_of_no_return":false}
  28.273 {"kind":"progress","stage":"pull_engine","done":1,"total":null,"unit":"steps"}
  32.286 {"kind":"progress","stage":"pull_engine","done":2,"total":null,"unit":"steps"}
  36.302 {"kind":"progress","stage":"pull_engine","done":3,"total":null,"unit":"steps"}
  40.313 {"kind":"progress","stage":"pull_engine","done":4,"total":null,"unit":"steps"}
  44.327 {"kind":"progress","stage":"pull_engine","done":5,"total":null,"unit":"steps"}
  48.337 {"kind":"progress","stage":"pull_engine","done":6,"total":null,"unit":"steps"}
  52.349 {"kind":"progress","stage":"pull_engine","done":7,"total":null,"unit":"steps"}
  56.365 {"kind":"progress","stage":"pull_engine","done":8,"total":null,"unit":"steps"}
  60.398 {"kind":"progress","stage":"pull_engine","done":9,"total":null,"unit":"steps"}
  64.430 {"kind":"progress","stage":"pull_engine","done":10,"total":null,"unit":"steps"}
  68.462 {"kind":"progress","stage":"pull_engine","done":11,"total":null,"unit":"steps"}
  72.527 {"kind":"progress","stage":"pull_engine","done":12,"total":null,"unit":"steps"}
  76.549 {"kind":"progress","stage":"pull_engine","done":13,"total":null,"unit":"steps"}
  80.571 {"kind":"progress","stage":"pull_engine","done":14,"total":null,"unit":"steps"}
  84.593 {"kind":"progress","stage":"pull_engine","done":15,"total":null,"unit":"steps"}
  88.616 {"kind":"progress","stage":"pull_engine","done":16,"total":null,"unit":"steps"}
  92.628 {"kind":"progress","stage":"pull_engine","done":17,"total":null,"unit":"steps"}
  93.668 {"kind":"done","stage":"pull_engine","ms":69000}
  94.080 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
  94.950 {"kind":"done","stage":"container","ms":1000}
  94.955 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
  95.096 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
 100.566 {"kind":"done","stage":"health","ms":5000}
 100.635 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3","companion_digest":null}
rc=0
```

**Duraciones por etapa (pase 2, arranque en frío)**

| Etapa | Entra | Cierra | Duración real | `ms` declarado | Avance |
|---|---|---|---|---|---|
| `runtime_staging` | 2,081 | 3,115 | **1,03 s** | 1000 | 15/15 |
| `machine` | 5,167 | 23,812 | **18,65 s** | 19000 | **0 eventos** (MAC3-06) |
| `pull_engine` | 24,247 | 93,668 | **69,42 s** | 69000 | 17 latidos, hueco máx. **4,06 s** |
| `container` | 94,080 | 94,950 | **0,87 s** | 1000 | — (una apertura, un cierre) |
| `health` | 94,955 | 100,566 | **5,61 s** | 5000 | 1 |
| `ready` | — | 100,635 | — | — | digest rc2, `companion_digest:null` |
| **Total** | | | **100,6 s** | | |

Huecos entre eventos NDJSON, de mayor a menor: **18,65 s** (dentro de `machine`),
5,47 s (dentro de `health`), 4,06 s y 4,03 s (latidos de `pull_engine`).
**`pull_engine` cumple el encargo: ninguna ventana muda > 15 s.** El de `machine`
sobrevive al vigía de 15 s sólo porque `last_activity` también se refresca con
stderr (`engine_adapter.rs:545-551`), no porque emita avance.

### La comprobación decisiva — `/healthz` **desde el host macOS** (**PASA**)

```
$RT ps -> eacfcaf3379a  mactest3  Up About a minute  127.0.0.1:37727->7517/tcp
PUERTO_PUBLICADO=37727     CONTAINER_ID=eacfcaf3379a

curl -sS -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:37727/healthz
  -> 200
  -> 200
  -> 200
curl -sS http://127.0.0.1:37727/healthz
  -> {"status":"ok","service":"hermes-shell-server","version":"0.9.0","ts":"2026-09-11T03:49:49.902162+00:00"}
lsof -nP -iTCP:37727 -> gvproxy 84104 … 127.0.0.1:37727 (LISTEN)      (sin CLOSE_WAIT)
```

**MAC2-05 está arreglado.** La regla que faltaba está dentro de la imagen rc2:

```
$RT exec mactest3 nft list chain inet hermes_host input
  type filter hook input priority filter - 10; policy drop;
  ip saddr 10.0.2.0/24      tcp dport 7517 accept comment "Lumen UI: host hipervisor vía QEMU user-net"
  ip saddr 192.168.64.0/24  tcp dport 7517 accept comment "Lumen UI: host hipervisor vía Apple VZ NAT"
  ip saddr 192.168.127.0/24 tcp dport 7517 accept comment "Safent UI: host hipervisor vía gvproxy (podman machine)"
  log prefix "hermes_host INPUT DROP: " drop
```

### La URL `?k=` que abriría la app (**PASA**)

```
curl -sS  'http://127.0.0.1:37727/?k=<VALE>'   -> http=307  redirect=http://127.0.0.1:37727/app/?k=<VALE>
curl -sSL 'http://127.0.0.1:37727/?k=<VALE>'   -> http=200  bytes=1555  url_final=http://127.0.0.1:37727/app/?k=<VALE>
grep del HTML servido:
  <script>window.__SAFENT_TOKEN__="<64 hex>";</script>      <- el bearer SÍ se inyecta
  (el vale de arranque NO aparece literal en el HTML: el `?k=` se canjea, no se refleja)
curl 'http://127.0.0.1:37727/app/'  (sin vale)  -> http=200 bytes=1488, SIN token inyectado
```

Es decir: el envoltorio navegaría a una página que **carga y viene autenticada**.
El armazón de la SPA se sirve sin vale, pero **sin bearer**, así que no hay
credencial regalada.

### `facts` con el proveedor y el tamaño reales (**PASA**)

```
SAFENT_IMAGE=ghcr.io/devwspito/safent@sha256:9bafbad6… safent facts --json
  "os": "darwin", "arch": "arm64",
  "machines": [{"name":"mactest3-engine","provider":"applehv","cpus":4,
                "memoryBytes":8589934592,"rootful":true,"running":true,"ours":true}]
  "localEngineImageDigest": "sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3"
  "engineContainer": {"exists":true,"running":true,
                      "imageDigest":"365e584d7f5c1396db6943087d439409b811c166d862351e0dfe3f1343786f0a"}   <- MAC3-02
  "publishedPort": 37727, "daemonHealth": "healthy"
```

`provider`, `cpus` y `memoryBytes` son ya los de verdad (MAC2-01 arreglado) y por
eso `pull_engine` se ejecuta como etapa propia. **Pero `engineContainer.imageDigest`
no es un digest**: es el ID local de la imagen. Ver MAC3-02.

---

## Paso 3 — Reapertura con el motor sano (**FALLA — MAC3-02**)

```
ANTES:   id=eacfcaf3379a  puerto=127.0.0.1:37727->7517/tcp  estado=Up 2 minutes
   0.838 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
   7.223 {"kind":"done","stage":"container","ms":7000}
   7.233 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
   7.371 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
  12.943 {"kind":"done","stage":"health","ms":5000}
  13.032 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6…dbf8a3","companion_digest":null}
rc=0
DESPUES: id=506e554cc462  puerto=127.0.0.1:41769->7517/tcp  estado=Up 5 seconds
```

**Otro contenedor, otro puerto, 13,0 s.** `quickstart.md` §4.2 pide ≤ 10 s y «no
se repite la preparación». Los datos sobreviven (volumen `mactest3-data`); el
trabajo en curso del motor, no.

## Paso 4 — Reanudación tras matar el contenedor (**PASA**)

```
$RT kill mactest3   -> mactest3            (Exited (137) 3 seconds ago)
   0.588 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
   1.350 {"kind":"done","stage":"container","ms":1000}
   1.359 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
   1.476 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
   7.137 {"kind":"done","stage":"health","ms":6000}
   7.217 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6…dbf8a3","companion_digest":null}
rc=0
DESPUES: id=9fa70dbc575a  puerto=127.0.0.1:39399->7517/tcp  estado=Up 6 seconds
curl /healthz desde el host tras reanudar -> 200
```

Se recupera **sola**, en **7,2 s**, sin preguntar nada, y el puerto nuevo
**responde desde macOS**. Este era el paso (4) del encargo: **PASA**.

## Paso 5 — Convivencia con el podman del dueño (**PASA**)

| Medida | Antes (05:42) | Después (05:55) |
|---|---|---|
| `podman-machine-default` | libkrun · rootful=true · running | libkrun · rootful=true · **running** |
| `krunkit` (PID) | 20043 | **20043** |
| `gvproxy` del dueño (PID) | 20042 | **20042** |
| `~/.config/containers` (mtime) | 2026-07-16T11:02:28 | **igual** |
| `~/.local/share/containers` | 2026-07-16T11:01:56 | **igual** |
| `~/.local/share/containers/podman/machine` | 2026-07-16T11:02:28 | **igual** |
| `/opt/podman` | 2026-07-08T22:08:46 | **igual** |
| `~/.safent` | no existe | **no existe** |
| `…/machine/applehv` | sólo `cache` | **sólo `cache`** |
| `podman run` huérfanos | 0 | **0** |
| `/var/folders/…/T/podman/` | sólo ficheros de `podman-machine-default` | **igual** |

---

## Hallazgos

### MAC3-01 · BLOQUEANTE (release) · el DMG de este run **no está firmado** — regresión

- **Evidencia**: `codesign -dvv Safent_0.9.0_aarch64.dmg` → **«code object is not
  signed at all»** rc=1; `spctl -a -vv -t open --context context:primary-signature`
  → **rejected · source=no usable signature** rc=3; sin `--context` →
  **rejected · source=Insufficient Context** rc=3.
- **Contraste**: el DMG de la verificación 2 (run 34549128789) daba
  `accepted · source=Notarized Developer ID` y `codesign --verify` rc=0. Entre un
  run y otro se ha perdido la firma del DMG; el `.app` **sí** sigue firmado y
  notarizado correctamente (`spctl -t exec` accepted, `codesign --verify --deep
  --strict` rc=0, timestamp 11-sep 05:29:14).
- **Salvedad sobre el grapado**: `xcrun stapler validate <dmg>` devuelve rc=0,
  pero la traza verbosa termina en `Downloaded ticket has been stored at
  file:///var/folders/…/….ticket` — **validó descargando el ticket de Apple**, no
  contra uno embebido. Con un DMG sin firma no hay `cdhash` al que grapar nada,
  así que ese rc=0 **no demuestra** que el ticket viaje dentro del fichero.
- **Efecto en el dueño**: `quickstart.md` §1 exige **cero** avisos de origen
  desconocido. Un `.dmg` descargado (con `com.apple.quarantine`) que Gatekeeper
  no puede evaluar es exactamente ese aviso, en el primer paso del recorrido.
- **Nota**: este es un fallo de **empaquetado/CI**, no de producto. Hay que
  comparar el paso de firma del DMG de este run con el del 34549128789.

### MAC3-02 · MAYOR · reabrir la app **sigue destruyendo** el motor sano (MAC2-06 vivo, causa nueva)

- **Evidencia**: pase 3 — `eacfcaf3379a`/puerto 37727 (Up 2 minutes) →
  `506e554cc462`/puerto 41769 (Up 5 seconds), 13,0 s, con el motor sano y sin que
  nadie lo pida.
- **Causa, en dos líneas concretas**:
  1. `safent:1691` — `d="$("$RT" inspect -f '{{.Image}}' "$NAME")"`. En podman,
     `inspect -f '{{.Image}}'` devuelve el **ID local** de la imagen
     (`365e584d7f5c1396db6943087d439409b811c166d862351e0dfe3f1343786f0a`),
     **nunca** el digest del manifiesto. Así lo emite `facts`:
     `"engineContainer":{"exists":true,"running":true,"imageDigest":"365e584d7f5c…"}`.
  2. `reconcile.rs:161-166` (`images_gap`) compara ese valor con
     `want.digest` = `sha256:9bafbad6…dbf8a3`. **Nunca** pueden coincidir → devuelve
     `RecreateEngine` en **cada** observación → `cli_invocation_for`
     (`engine_adapter.rs:389-393`) lo traduce a **`up`** → `cmd_up` → `_run` →
     `"$RT" rm -f "$NAME"` (`safent:347`).
- **Por qué el test unitario no lo caza**:
  `mac2_06_reopening_the_app_with_a_healthy_engine_never_destroys_it`
  (`reconcile.rs:316`) usa una `converged_macos_facts()` cuyo `image_digest` lleva
  el digest del manifiesto — **un valor que el CLI real no produce jamás**. El
  arreglo de MAC2-01 movió el fallo una capa más abajo, de `machine_gap` a
  `images_gap`, sin quitarlo.
- **Arreglo**: que el CLI emita el digest de verdad. La información **ya existe**:
  `"$RT" ps -a --format '{{.Image}}'` devolvió, en esta misma prueba,
  `ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3`; el campo equivalente en
  `inspect` es `{{.ImageName}}`. Y el test tiene que fijar el vocabulario real
  (`data-model.md`, misma tabla que ya se hizo para `os` y para `machines[]`).

### MAC3-03 · MAYOR · MAC2-07 sin arreglar: el perfil seccomp viaja como ruta del host

- **Evidencia**: pase 1, línea exacta —
  `{"kind":"failed","code":"daemon_unhealthy","detail":"Error: opening seccomp profile failed: open /tmp/safent-mac-test3/state/safent-seccomp.json: no such file or directory","retryable":true}`
  con el fichero **existiendo** en el host (19 766 B, `-rw-r--r--`).
- **Causa**: `_run` (`safent:364`) pasa `--security-opt seccomp="$SECCOMP"`, con
  `SECCOMP="$SAFENT_STATE_HOME/safent-seccomp.json"` (`safent:69-70`). En macOS lo
  abre el podman **de dentro de la VM**, que sólo ve `/Users`, `/private` y
  `/var/folders`.
- **Efecto**: funciona con el `~/.safent` por defecto **por casualidad**. Cualquier
  `SAFENT_STATE_HOME` fuera de esos tres árboles rompe el arranque con un error
  crudo de podman disfrazado de `daemon_unhealthy`. Idéntico a MAC2-07, sin
  cambios.

### MAC3-04 · MENOR · el `.app` sigue sin ticket grapado (MAC2-12 dado por arreglado, no lo está)

- **Evidencia**: `xcrun stapler validate Safent.app` → rc=65, «Safent.app does not
  have a ticket stapled to it».
- `spctl -t exec` lo acepta aquí porque el ticket ya estaba en la caché local tras
  validar el DMG (que además lo **descargó**, ver MAC3-01). Importa para la vía del
  actualizador (`macos/Safent.app.tar.gz`, que este mismo artefacto publica).

### MAC3-05 · MENOR · los 15 `cdhash` siguen a `null` (MAC2-08 sin cambio)

- **Evidencia**: `cdhash no-null: 0` sobre 15 entradas; los 8 Mach-O pasan
  `codesign --verify --strict` (rc=0) y tienen CDHash real, pero nadie lo compara.
- La verificación real la hace el sha256, que casa **15/15** (y también los modos).
  La rama `codesign`+cdhash de `cmd_stage_runtime` sigue sin ejercitarse.

### MAC3-06 · MENOR · la etapa `machine` pasa **18,65 s** sin un solo `progress`

- **Evidencia**: pase 2 — `stage machine` en 5,167 y `done` en 23,812, sin nada en
  medio. Es el mayor hueco de todo el arranque.
- **Choque**: `app-engine.md` §3 invariante 2 pide `progress` **al menos cada 5 s**
  mientras la etapa vive. Sobrevive al vigía porque `last_activity` se refresca
  también con stderr (`engine_adapter.rs:545-551`), no porque narre nada.
- **Efecto**: 19 s de «Preparando la maquina» con la barra quieta. Es el mismo
  arreglo que ya se hizo para `pull_engine` (`_pull_with_heartbeat`), pendiente en
  `cmd_ensure_machine`.

### MAC3-07 · MENOR · el producto ejecuta los helpers **del dueño**, no los suyos

- **Evidencia**: con la máquina de la app viva,
  `ps -o pid=,comm= -p 84104 -p 84106` →
  `84104 /opt/podman/bin/gvproxy` · `84106 /opt/podman/bin/vfkit`.
  Y **no son los mismos binarios**:

  | binario | empaquetado (sha256) | `/opt/podman/bin` | |
  |---|---|---|---|
  | gvproxy | f8766f3a3907e5da… | 36c0ca43b5552db4… | **DISTINTOS** |
  | vfkit | 41c48ca35eda0cf3… | 0489f7caef8f91f4… | **DISTINTOS** |

- **Causa**: sigue sin `containers.conf` en el bundle (MAC2-13) → `CONTAINERS_CONF`
  vacío → podman resuelve los helpers por PATH; y `augmented_path()`
  (`engine_adapter.rs:373-379`) **añade `/opt/podman/bin` al PATH** que el
  envoltorio le pasa al CLI.
- **Efecto**: el trabajo de empaquetar, firmar y verificar por sha256 gvproxy,
  vfkit y krunkit **no se usa**: en un Mac con podman instalado la app arranca su
  máquina con los binarios ajenos, cuya versión no controla (aquí coinciden en
  v0.6.4, pero es casualidad). Contradice la regla «nunca el del PATH del usuario»
  de `app-engine.md` §1, que hoy sólo se cumple para el propio `podman`.

### MAC3-08 · MENOR · una etapa abierta dos veces sin cerrarse (MAC2-09 sin cambio)

- **Evidencia**: pase 1 — `stage container` en 95,927 y otra vez en 99,007, sin
  `done` ni `failed` entre medias. En el pase 2 (camino feliz) **no ocurre**: una
  apertura, un cierre.
- **Choque**: `app-engine.md` §3 invariante 3.

### MAC3-09 · MENOR · MAC2-14 sigue: el producto no aísla su podman del del dueño

- El arnés `XDG_CONFIG_HOME`/`XDG_DATA_HOME`/`TMPDIR` lo pone **la prueba**, no el
  producto. Sin él, la máquina propia se crearía dentro de
  `~/.config/containers`/`~/.local/share/containers` del dueño y el
  `-pid-file $TMPDIR/podman/gvproxy.pid` (sin nombre de máquina) pisaría el suyo.
  No reproducido a propósito: las reglas de la prueba lo prohíben.

### Nota · el selftest sigue sin tocar el puerto, pero ya no hace falta

`selftest.rs:150-151` se conforma con `LoopOutcome::Ready` y devuelve 0 sin hacer
una sola petición. El segundo defecto de MAC2-05 sigue ahí **en ese fichero** —
pero está cubierto una capa más abajo: `cmd_up` (`safent:2004-2018`) exige un
`200` real de `http://127.0.0.1:<puerto>/healthz` **desde el host** antes de
emitir `ready`, con 12 intentos cada 2 s. Por eso el `ready` de esta verificación
es de fiar. Si algún día `up` deja de probarlo, el selftest volverá a mentir.

---

## Lo que no se pudo simular

- **Todo lo gráfico**: ventana, etapas pintadas, «Cancelar», barra de menús,
  instancia única, `Cmd+L`, Anuncios. No hay pantalla por SSH.
- **El aviso de Gatekeeper de MAC3-01**: se observó el veredicto de `spctl`, no el
  diálogo real de un doble clic sobre un DMG descargado con cuarentena.
- **El compañero (029)**: `--selftest` sin compañero, por encargo. Su digest sí
  quedó verificado en el manifiesto.
- **El recorrido con `~/.safent`**: prohibido escribir en el home del dueño, así
  que MAC3-03 se observó en el sandbox.
- **La actualización de un botón (US2 de 028)**: requiere una versión nueva
  publicada.

## Desmontaje

```
Nombres reales verificados ANTES de borrar:
  contenedor  9fa70dbc575a  mactest3  ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3
  volumen     mactest3-data
  imagen      ghcr.io/devwspito/safent  365e584d7f5c  8.17 GB
  maquina     mactest3-engine  applehv  running

$RT rm -f mactest3                 -> mactest3
$RT volume rm -f mactest3-data     -> mactest3-data
$RT rmi -f ghcr.io/devwspito/safent
$RT machine stop mactest3-engine   -> Machine "mactest3-engine" stopped successfully
$RT machine rm -f mactest3-engine  -> (sin máquinas en el sandbox)
mount | grep -c safent-mac-test3   -> 0        (DMG desmontado en el paso 1)
hdiutil info | grep -c safent-mac-test3 -> 0
rm -rf /private/tmp/safent-mac-test3 -> No such file or directory
pgrep -fl "safent-mac-test3|mactest3" -> (vacío)
pgrep -fl "podman run"                -> (vacío)
podman machine list (dueño, entorno limpio) -> podman-machine-default  libkrun  Currently running
krunkit=20043  gvproxy=20042                 (los mismos PID que al empezar)
~/.safent                                    -> No such file or directory
~/.local/share/containers/podman/machine/applehv -> sólo 'cache'
df -h /System/Volumes/Data                   -> 76Gi libres (77Gi al empezar)
```

Todo lo creado por la prueba —máquina, contenedor, volumen, imagen, sandbox, DMG
montado— está retirado con el podman **empaquetado**, verificando los nombres
antes de cada borrado. La máquina del dueño quedó **arrancada e intacta**.

## Siguiente acción recomendada

1. **MAC3-01** — recuperar la firma del DMG en el pipeline y volver a grapar
   **contra un DMG firmado**; verificar con `spctl -a -vv -t open` **y** que
   `stapler validate -v` **no** descargue el ticket. Es lo único que bloquea la
   publicación. `devops-engineer`.
2. **MAC3-02** — que `engineContainer.imageDigest` sea un digest de verdad
   (`{{.ImageName}}` o el `@sha256:` de `ps --format '{{.Image}}'`), y que el test
   de regresión de MAC2-06 use el valor que el CLI **realmente** emite.
   `debug-engineer` + una línea en `data-model.md`.
3. **MAC3-03** — el perfil seccomp no puede ser una ruta del host en macOS:
   copiarlo dentro de la máquina/volumen, o validar la restricción antes de
   `podman run`. `backend-engineer`.
4. **MAC3-04** — grapar el `.app` de verdad (importa para el actualizador).
5. **MAC3-07 + MAC2-13** — empaquetar `containers.conf` con `helper_binaries_dir`
   apuntando al runtime propio, y dejar de meter `/opt/podman/bin` en el PATH del
   CLI. `software-architect` + `devops-engineer`.
6. **MAC3-06 / MAC3-08** — latidos en `ensure-machine` y un solo cierre por etapa.
7. **MAC3-05 / MAC3-09** — poblar los `cdhash` y aislar `XDG_*`/`TMPDIR` desde el
   propio producto.
