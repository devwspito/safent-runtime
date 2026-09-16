# Verificación 5 — app nativa de macOS en el MacBook del dueño

**Qué**: quinta prueba real del artefacto notarizado `Safent-macOS-arm64` del run
**34572890786** (`devwspito/agents-autonomy`, rama `safent-desktop-pipeline-028`,
`dfc5f3dc`; el run figura en rojo por otro job), versión 0.9.0, con el motor
fijado al digest `sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3`
(= `ghcr.io/devwspito/safent:v0.9.0-rc2`) y el compañero `safent-ads` a
`sha256:54c4fee8535d750c027ae2976984feef9884c7f11f7702894f410e03161264b9`.
**Dónde**: MacBook Air del dueño (`macbook-air-2-1`), macOS 15.5 (24F74), arm64,
81 GiB libres. Por SSH, **sin sesión gráfica**.
**Cuándo**: 11-sep-2026, 10:01–10:18 (hora local del Mac). **Quién**: exploratory-tester.

## Veredicto

**NO se puede publicar.** Los seis arreglos del encargo se confirman en vivo —
**los cinco que se podían medir en el camino feliz pasan** (MAC4-01 cdhash,
MAC4-02 reapertura idempotente, MAC4-03 guardia con ruta completa, MAC4-06 `.app`
grapado, MAC4-05 latidos en `machine`)— pero **el arreglo de MAC4-05 ha
introducido un bloqueante nuevo**: en un Mac limpio la app **no arranca**, muere a
los **19,6 s** con `cli_porcelain_unsupported` (MAC5-01). Los latidos que ahora sí
narran la etapa `machine` salen con un **vocabulario que el propio envoltorio
rechaza** (`"unit":"seconds"`, fuera de las tres palabras del contrato).

Con ese único punto puenteado (la máquina creada de antemano, para que el latido
no llegue a emitirse), **todo el resto del encargo pasa, sin un solo fallo**:
reapertura con el **mismo id y el mismo puerto**, reanudación desatendida,
`/healthz` desde el host, la URL `?k=`, helpers empaquetados y la máquina del
dueño intacta.

Aparte, el artefacto trae un **segundo defecto grave e independiente** que no
afecta al primer arranque pero sí a la actualización: el `.app` del canal de
actualización (`macos/Safent.app.tar.gz`) viaja con un **manifiesto caducado**
que no casa con sus propios binarios (MAC5-02).

| Comprobación del encargo | Resultado |
|---|---|
| (1) sha256 en los dos extremos | **PASA** |
| (1) `codesign --verify --strict` + `-dvv` del **DMG** (`Authority=Developer ID … Luis Correa`) | **PASA** |
| (1) `spctl -a -t open --context context:primary-signature` del DMG | **PASA** (`accepted`) |
| (1) `stapler validate` del **DMG** | **PASA** |
| (1) `stapler validate` del **`.app`** | **PASA** (MAC4-06 **arreglado**) |
| (1) `spctl -a -vv -t exec` del `.app` | **PASA** |
| (1) `cdhash` no nulo y **igual** al de `codesign -dvvv` | **PASA** (8/8 Mach-O, en el DMG) |
| (1) las 17 entradas sha256 del manifiesto casan | **PASA** (17/17 sha256 y 17/17 modo) |
| (1) `containers.conf` en el paquete | **PASA** |
| (2) **Arranque en frío en un Mac limpio** | **FALLA — BLOQUEANTE (MAC5-01)** |
| (2) Etapas en orden, ninguna ventana muda > 15 s (incluida `machine`) | **PASA** (máx. **9,16 s**; en el pase bueno, 5,52 s) |
| (2) `curl /healthz` desde el **host macOS** → 200 | **PASA** (200/200/200) |
| (2) La URL `?k=` sirve el HTML con el bearer inyectado | **PASA** (307 → 200, `window.__SAFENT_TOKEN__`) |
| (2) **gvproxy/vfkit en ejecución = los EMPAQUETADOS** | **PASA** (sha256 idénticos al bundle) |
| (3) **Reapertura con motor sano: mismo id y mismo puerto** | **PASA** (MAC4-02 **arreglado**) |
| (4) Reanudación tras `podman kill`, desatendida | **PASA** (`ready` en 6,98 s) |
| (5) `podman-machine-default` intacta, cero huérfanos, desmontaje | **PASA** |

## Reglas de la prueba y arnés prestado

Todo bajo `/tmp/safent-mac-test5/` con `SAFENT_STATE_HOME=/tmp/safent-mac-test5/state`
**escrito en su forma no canónica a propósito** (MAC3-03) y `SAFENT_NAME=mactest5`
→ contenedor `mactest5`, volumen `mactest5-data`, **máquina `mactest5-engine`**
(nombre real verificado con `machine list`, nunca supuesto). Nunca se ejecutó
`tailscale`; nunca se tocó `/opt/podman`, `~/.config/containers`,
`~/.local/share/containers`, `~/.ssh`, `/Applications` ni `podman-machine-default`.
Sin `SAFENT_ENGINE_DIGEST` ni `SAFENT_RUNTIME_DIR`.

Arneses **añadidos desde fuera** (el producto no los trae):

1. `XDG_CONFIG_HOME`, `XDG_DATA_HOME` y `TMPDIR` dentro del sandbox — **sigue
   vigente MAC4-08**. Que hacía falta se ve en los propios argumentos de vfkit:
   `--bootloader efi,variable-store=/tmp/safent-mac-test5/xdg-data/containers/podman/machine/applehv/…`
   Sin ese redirigido, la máquina de la prueba habría nacido dentro del
   `~/.local/share/containers` del dueño.
2. **La máquina creada de antemano** (`safent ensure-machine` a mano, desde la
   terminal, antes del pase). Sin ella **no hay pase 2, ni 3, ni 4**: la app no
   arranca (MAC5-01). Con la máquina ya creada y corriendo, `machine start`
   devuelve en menos de un segundo, el contador de latidos no llega nunca a 5 y
   **la línea venenosa no se emite**. Es el arnés que convierte el bloqueante en
   algo medible, y su existencia **es** la prueba del defecto.
   **Ya NO hizo falta el arnés `SAFENT_CODESIGN` de la verificación 4**: MAC4-01
   está arreglado y `runtime_staging` pasa 17/17 tal cual sale de la caja.

Estado del dueño **antes** de empezar (10:01): `podman-machine-default` libkrun
`running`, `krunkit`=20043, `gvproxy`=20042, `~/.safent` inexistente,
`…/machine/applehv` sólo con `cache`, 0 `podman run` huérfanos, 81 Gi libres.

---

## Paso 1 — Descarga, traslado y verificación del paquete

### 1.1 Descarga (DGX) — **PASA**

```
gh api repos/devwspito/agents-autonomy/actions/artifacts/10188800618/zip > f.zip
   -> 2 018 128 214 B   sha256 7b2254dc831013d0f2bf6fc1913e8996ce34972161d26ea53a4fa27418bbfbee
unzip -l:
   1 007 254 255  macos/Safent.app.tar.gz
   1 013 576 851  dmg/Safent_0.9.0_aarch64.dmg
             404  macos/Safent.app.tar.gz.sig
```

### 1.2 Traslado — **PASA**

```
scp dmg/Safent_0.9.0_aarch64.dmg luiscorrea@macbook-air-2-1:/tmp/safent-mac-test5/   (65,1 s)
sha256 DGX : 06f4553e04290db0482ba36d1703b51e803cde4a2eb6effa53207aadec2b0a5e
sha256 Mac : 06f4553e04290db0482ba36d1703b51e803cde4a2eb6effa53207aadec2b0a5e   IDÉNTICO
```

### 1.3 Firma, notarización y grapado — **DMG PASA · `.app` PASA (MAC4-06 arreglado)**

```
codesign --verify --strict --verbose=2 Safent_0.9.0_aarch64.dmg
  -> valid on disk · satisfies its Designated Requirement            rc=0
codesign -dvv Safent_0.9.0_aarch64.dmg
  -> Identifier=Safent_0.9.0_aarch64 · Format=disk image
     CodeDirectory v=20200 size=308 flags=0x0(none) hashes=1+6
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Authority=Developer ID Certification Authority · Authority=Apple Root CA
     Timestamp=11 Sep 2026 at 09:15:50
     Notarization Ticket=stapled          <- el ticket VIAJA DENTRO del fichero
     TeamIdentifier=JBMBA58A8X                                       rc=0
spctl -a -vv -t open --context context:primary-signature <dmg>
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)       rc=0
xcrun stapler validate <dmg>   -> The validate action worked!        rc=0
xattr -l <dmg>                 -> (vacío)

hdiutil attach -nobrowse -readonly -mountpoint …/mnt <dmg>   -> /dev/disk5s1
   (todas las particiones «verificado», CRC32 $10799168)
   contenido: Safent.app, Applications -> /Applications
ditto mnt/Safent.app …/Safent.app        (0,90 s)
hdiutil detach …/mnt                     -> "disk4" ejected;  mount | grep -c = 0

spctl -a -vv -t exec Safent.app
  -> accepted · source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)       rc=0
codesign --verify --strict --verbose=2 Safent.app          -> valid on disk   rc=0
codesign --verify --deep --strict --verbose=2 Safent.app                      rc=0
codesign -dvvv Safent.app
  -> Identifier=com.safent.desktop · CodeDirectory v=20500 flags=0x10000(runtime)
     hashes=1045+3
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Timestamp=11 Sep 2026 at 09:12:16 · TeamIdentifier=JBMBA58A8X · Runtime 14.5.0
     Notarization Ticket=stapled                                     <- NUEVO
xcrun stapler validate Safent.app
  -> Processing: /tmp/safent-mac-test5/Safent.app
     The validate action worked!                                     rc=0
```

**MAC4-06 arreglado**: el `.app` **y** el DMG están notarizados y grapados, y las
dos herramientas lo dicen por separado (`stapler validate` rc=0 y
`codesign -dvv` → `Notarization Ticket=stapled`).

### 1.4 `runtime-bundle.json` del DMG: 17 entradas, 8 `cdhash` reales — **PASA**

`Contents/Resources/runtime/` trae **18** ficheros (17 + `runtime-bundle.json`),
**planos** (sin `bin/`), con `containers.conf` (43 B).

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
| containers.conf | OK | 0644 | null | n/a | n/a |
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
| safent.json | OK | 0644 | null | n/a | n/a |
| run-safent.sh | OK | 0755 | null | n/a | n/a |
| safent | OK | 0755 | null | n/a | n/a |
| libvirglrenderer.1.dylib | OK | 0644 | 92b22cda…70481 | **IGUAL** | rc=0 |

`17/17 sha256 · 17/17 modo · 8/8 cdhash · 0 discrepancias`.

**Nota importante**: esto vale **para el `.app` que viaja dentro del DMG**. El
`.app` del **canal de actualización** (`macos/Safent.app.tar.gz`, del **mismo**
artefacto) trae un manifiesto **distinto y caducado** — ver **MAC5-02**.

---

## Paso 2 — Arranque en frío (`--selftest`, sin compañero)

Binario: `Safent.app/Contents/MacOS/safent-desktop --selftest`, NDJSON capturado
en vivo con marca de tiempo relativa (`perl -MTime::HiRes`). Sandbox comprobado
**antes**: `machine list` vacío, `state` vacío, sin `SAFENT_CODESIGN` en el
entorno (`env | grep -c SAFENT_CODESIGN` → **0**).

### Pase 1 — tal y como sale de la caja (**FALLA — MAC5-01**)

```
   2.077 {"kind":"stage","stage":"runtime_staging","label":"Preparando la base de ejecucion","total_bytes":null,"point_of_no_return":false}
   2.102 {"kind":"progress","stage":"runtime_staging","done":1,"total":17,"unit":"steps"}
   2.114 {"kind":"progress","stage":"runtime_staging","done":2,"total":17,"unit":"steps"}
   2.127 {"kind":"progress","stage":"runtime_staging","done":3,"total":17,"unit":"steps"}
   2.204 {"kind":"progress","stage":"runtime_staging","done":4,"total":17,"unit":"steps"}
   2.322 {"kind":"progress","stage":"runtime_staging","done":5,"total":17,"unit":"steps"}
   2.378 {"kind":"progress","stage":"runtime_staging","done":6,"total":17,"unit":"steps"}
   2.410 {"kind":"progress","stage":"runtime_staging","done":7,"total":17,"unit":"steps"}
   3.098 {"kind":"progress","stage":"runtime_staging","done":8,"total":17,"unit":"steps"}
   3.139 {"kind":"progress","stage":"runtime_staging","done":9,"total":17,"unit":"steps"}
   3.154 {"kind":"progress","stage":"runtime_staging","done":10,"total":17,"unit":"steps"}
   3.207 {"kind":"progress","stage":"runtime_staging","done":11,"total":17,"unit":"steps"}
   3.240 {"kind":"progress","stage":"runtime_staging","done":12,"total":17,"unit":"steps"}
   3.254 {"kind":"progress","stage":"runtime_staging","done":13,"total":17,"unit":"steps"}
   3.266 {"kind":"progress","stage":"runtime_staging","done":14,"total":17,"unit":"steps"}
   3.279 {"kind":"progress","stage":"runtime_staging","done":15,"total":17,"unit":"steps"}
   3.293 {"kind":"progress","stage":"runtime_staging","done":16,"total":17,"unit":"steps"}
   3.327 {"kind":"progress","stage":"runtime_staging","done":17,"total":17,"unit":"steps"}
   3.335 {"kind":"done","stage":"runtime_staging","ms":1000}
   5.387 {"kind":"stage","stage":"machine","label":"Preparando la maquina","total_bytes":null,"point_of_no_return":false}
  14.548 {"kind":"stage","stage":"machine","label":"Preparando la maquina","total_bytes":null,"point_of_no_return":false}
  19.586 {"kind":"failed","code":"cli_porcelain_unsupported","detail":"el motor instalado no habla el protocolo esperado todavía","retryable":false}
rc=1
```

**`runtime_staging` pasa 17/17 en 1,26 s, sin arnés: MAC4-01 está arreglado.**
La app muere después, en `machine`, a los **19,586 s**, con `retryable:false` (no
reintenta nunca más) y el fichero de stderr de la app **vacío** (0 bytes): el
dueño no tiene nada que leer. Ver MAC5-01 para la causa exacta y el arreglo de
una palabra.

Se ve también que la etapa `machine` se **abre dos veces** (5,387 y 14,548) sin
`done` ni `failed` entre medias, y **nunca cierra** — MAC4-07 sin cambio.

### La causa, medida directamente contra el CLI (stdout crudo)

```
$CLI --no-companion ensure-machine --porcelain
   0.011 {"t":"stage","id":"machine","label":"Preparando la maquina"}
   5.064 {"t":"progress","id":"machine","done":5,"unit":"seconds"}     <- AQUÍ
  12.179 {"t":"progress","id":"machine","done":5,"unit":"seconds"}
  17.229 {"t":"progress","id":"machine","done":10,"unit":"seconds"}
  22.583 {"t":"done","id":"machine","ms":23000}
rc=0
machine list despues -> mactest5-engine  applehv  Currently running
```

El CLI hace su trabajo (la máquina se crea y arranca, rc=0, y los latidos llegan
cada ~5 s, que era justo lo que pedía MAC4-05). Lo que rompe es **la palabra**:
`"unit":"seconds"` no está en el vocabulario cerrado de `app-engine.md` §3
(`bytes` · `layers` · `steps`).

### Pase 2 — con la máquina ya creada (arnés) — **PASA**, rc=0

```
   0.479 {"kind":"stage","stage":"pull_engine","label":"Descargando Safent","total_bytes":null,"point_of_no_return":false}
   4.512 {"kind":"progress","stage":"pull_engine","done":1,"total":null,"unit":"steps"}
   8.539 {"kind":"progress","stage":"pull_engine","done":2,"total":null,"unit":"steps"}
  12.559 {"kind":"progress","stage":"pull_engine","done":3,"total":null,"unit":"steps"}
  16.583 {"kind":"progress","stage":"pull_engine","done":4,"total":null,"unit":"steps"}
  20.610 {"kind":"progress","stage":"pull_engine","done":5,"total":null,"unit":"steps"}
  24.634 {"kind":"progress","stage":"pull_engine","done":6,"total":null,"unit":"steps"}
  28.660 {"kind":"progress","stage":"pull_engine","done":7,"total":null,"unit":"steps"}
  32.688 {"kind":"progress","stage":"pull_engine","done":8,"total":null,"unit":"steps"}
  36.709 {"kind":"progress","stage":"pull_engine","done":9,"total":null,"unit":"steps"}
  40.729 {"kind":"progress","stage":"pull_engine","done":10,"total":null,"unit":"steps"}
  44.758 {"kind":"progress","stage":"pull_engine","done":11,"total":null,"unit":"steps"}
  48.787 {"kind":"progress","stage":"pull_engine","done":12,"total":null,"unit":"steps"}
  52.821 {"kind":"progress","stage":"pull_engine","done":13,"total":null,"unit":"steps"}
  56.855 {"kind":"progress","stage":"pull_engine","done":14,"total":null,"unit":"steps"}
  60.888 {"kind":"progress","stage":"pull_engine","done":15,"total":null,"unit":"steps"}
  64.931 {"kind":"progress","stage":"pull_engine","done":16,"total":null,"unit":"steps"}
  68.969 {"kind":"progress","stage":"pull_engine","done":17,"total":null,"unit":"steps"}
  73.002 {"kind":"progress","stage":"pull_engine","done":18,"total":null,"unit":"steps"}
  77.033 {"kind":"progress","stage":"pull_engine","done":19,"total":null,"unit":"steps"}
  81.072 {"kind":"progress","stage":"pull_engine","done":20,"total":null,"unit":"steps"}
  84.122 {"kind":"done","stage":"pull_engine","ms":83000}
  84.605 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
  85.593 {"kind":"done","stage":"container","ms":1000}
  85.603 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
  85.734 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
  91.249 {"kind":"done","stage":"health","ms":5000}
  91.324 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6e92f6520f07aea83aba9ac91d7ca5f03329706f6ce2a817f74dbf8a3","companion_digest":null}
rc=0                       stderr: 0 bytes
```

`runtime_staging` y `machine` ya no aparecen porque el reconciliador las ve
satisfechas (el runtime quedó desplegado y verificado en el pase 1, la máquina
corre): es exactamente lo que debe hacer.

**Duraciones por etapa**

| Pase | Etapa | Entra | Cierra | Duración real | `ms` declarado | Avances |
|---|---|---|---|---|---|---|
| 1 (falla) | `runtime_staging` | 2,077 | 3,335 | **1,26 s** | 1000 | 17/17 |
| 1 (falla) | `machine` | 5,387 | — | **SIN CIERRE** | — | 0 (reabierta) |
| 2 | `pull_engine` | 0,479 | 84,122 | **83,64 s** | 83000 | 20 latidos, hueco máx. **4,04 s** |
| 2 | `container` | 84,605 | 85,593 | **0,99 s** | 1000 | — |
| 2 | `health` | 85,603 | 91,249 | **5,65 s** | 5000 | 1 |
| 2 | `ready` | — | 91,324 | — | — | digest rc2, `companion_digest:null` |
| 2 | **Total** | | | **90,8 s** | | |

Huecos mudos, de mayor a menor — pase 1: **9,16 s** (dentro de `machine`), 5,04 s,
2,05 s. Pase 2: **5,52 s** (dentro de `health`), 4,04 s (latidos de `pull_engine`).
**Ninguna ventana muda supera los 15 s en ninguna etapa, incluida `machine`: el
encargo de MAC4-05 se cumple en lo medible.**

### La comprobación decisiva — `/healthz` **desde el host macOS** (**PASA**)

```
$RT machine list -> mactest5-engine  applehv  Currently running   (nombre real, verificado)
$RT ps           -> 48eabb037a60  mactest5  Up 8 minutes  127.0.0.1:36645->7517/tcp
                    ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3
curl -sS -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:36645/healthz
  -> 200 / 200 / 200
curl -sS http://127.0.0.1:36645/healthz
  -> {"status":"ok","service":"hermes-shell-server","version":"0.9.0","ts":"2026-09-11T08:16:03.199023+00:00"}
lsof -nP -iTCP:36645 -> gvproxy 70939 … 127.0.0.1:36645 (LISTEN)   (sin CLOSE_WAIT)
```

### La comprobación de «Mac limpio» — los helpers son los **EMPAQUETADOS** (**PASA**)

```
ps -o comm= -p 70939 -> /private/tmp/safent-mac-test5/Safent.app/Contents/Resources/runtime/gvproxy
ps -o comm= -p 70972 -> /private/tmp/safent-mac-test5/Safent.app/Contents/Resources/runtime/vfkit
shasum -a 256 de los binarios EN EJECUCIÓN:
  gvproxy 7a1ca760b82bacf9…  == empaquetado 7a1ca760b82bacf9…  != /opt/podman 36c0ca43b5552db4…
  vfkit   e20019be4d9bb6cf…  == empaquetado e20019be4d9bb6cf…  != /opt/podman 0489f7caef8f91f4…
ps -o args= -p 70939 | cut -c1-190
  …/runtime/gvproxy -mtu 1500 -ssh-port 55850 -listen-vfkit unixgram:///tmp/safent-mac-test5/tmp/podman/mactest5-engine-gvproxy.sock …
```

El socket de gvproxy lleva el **nombre de la máquina**, así que no pisa el del
dueño. Y el guardia de helpers ajenos (`_foreign_engine_helper`, arreglado en
MAC4-03) **no da falso positivo**: `ensure-machine` terminó rc=0 con los helpers
propios, que era el riesgo concreto que la verificación 4 avisaba («arreglar el
recorte sin arreglar la ruta marcaría como ajeno al helper propio y bloquearía
todos los arranques»). No se forzó un helper ajeno de verdad.

### Seccomp y estado canónico (**PASA**)

```
$RT inspect mactest5 --format '{{.HostConfig.SecurityOpt}}'
  -> [label=disable seccomp=/private/tmp/safent-mac-test5/Safent.app/Contents/Resources/runtime/safent.json]
```

Con `SAFENT_STATE_HOME=/tmp/…` **escrito sin canonizar**, el perfil sale del
propio paquete (tier 0), en forma canónica (`/private/tmp/…`), que es lo que el
podman **de dentro de la VM** puede abrir. MAC3-03 sigue arreglado.

### La URL `?k=` que abriría la app (**PASA**)

Vale obtenido por el descriptor 3, como manda `app-engine.md` §5:

```
$CLI --no-companion up --secret-fd 3 --porcelain 3>ticket
  {"t":"stage","id":"container","label":"Creando el contenedor"}
  {"t":"done","id":"container","ms":0}
  {"t":"stage","id":"health","label":"Esperando a que Safent este listo"}
  {"t":"done","id":"health","ms":1000}
  {"t":"ready","endpoint_ref":"stdout-secret"}
rc=0
fd 3 -> http://127.0.0.1:36645/?k=<VALE>          (UNA sola línea, nunca en stdout)

curl -sS  'http://127.0.0.1:36645/?k=<VALE>'  -> http=307  redirect=…/app/?k=<VALE>
curl -sSL 'http://127.0.0.1:36645/?k=<VALE>'  -> http=200  bytes=1555
  HTML: <script>window.__SAFENT_TOKEN__="0be9807b…";</script>   <- bearer inyectado
  el vale NO aparece literal en el HTML (se canjea, no se refleja): grep -c = 0
curl 'http://127.0.0.1:36645/app/'  (sin vale) -> http=200 bytes=1488, SIN token
```

Y de paso, **este `up` tampoco movió nada**: `ANTES id=48eabb037a60 puerto=36645`
→ `DESPUES id=48eabb037a60 puerto=36645`.

---

## Paso 3 — Reapertura con el motor sano (**PASA — MAC4-02 arreglado**)

```
ANTES:   id=48eabb037a60  puerto=36645  estado=Up 8 minutes
   0.909 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
   1.374 {"kind":"done","stage":"container","ms":1000}
   1.378 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
   1.683 {"kind":"done","stage":"health","ms":0}
   1.755 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6…dbf8a3","companion_digest":null}
rc=0
DESPUES: id=48eabb037a60  puerto=36645  estado=Up 8 minutes
```

**Mismo contenedor, mismo puerto, y el «Up 8 minutes» no se reinicia**: el motor
sano no se toca. `ready` en **1,755 s**, muy por debajo de los ≤ 10 s que pide
`quickstart.md` §4.2. `_container_matches_desired` (`safent:2148-2158`) toma la
rama de convergencia y `_run` no llega a ejecutarse. **MAC4-02 cerrado**, con la
medida sobre **id y puerto**, no sólo sobre `ready`.

## Paso 4 — Reanudación tras matar el contenedor (**PASA**)

```
$RT kill mactest5  -> mactest5        (48eabb037a60  Exited (137) 2 seconds ago)
   0.596 {"kind":"stage","stage":"container","label":"Creando el contenedor","total_bytes":null,"point_of_no_return":true}
   1.309 {"kind":"done","stage":"container","ms":1000}
   1.314 {"kind":"stage","stage":"health","label":"Esperando a que Safent este listo","total_bytes":null,"point_of_no_return":true}
   1.429 {"kind":"progress","stage":"health","done":0,"total":48,"unit":"steps"}
   6.904 {"kind":"done","stage":"health","ms":5000}
   6.978 {"kind":"ready","app_version":"0.9.0","engine_digest":"sha256:9bafbad6…dbf8a3","companion_digest":null}
rc=0
DESPUES: id=ef5ffc8e879b  puerto=38065  estado=Up 6 seconds
curl /healthz desde el host tras reanudar -> 200
```

Se recupera **sola**, en **6,98 s**, sin preguntar nada, y el puerto nuevo
**responde desde macOS**. Aquí el contenedor sí cambia de id y puerto, y **debe**:
el anterior está muerto, no hay motor sano que preservar.

## Paso 5 — Convivencia con el podman del dueño (**PASA**)

| Medida | Antes (10:01) | Después (10:18) |
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

### MAC5-01 · BLOQUEANTE (release) · en un Mac limpio la app **no arranca**: `cli_porcelain_unsupported`

- **Evidencia**: pase 1, línea exacta —
  `{"kind":"failed","code":"cli_porcelain_unsupported","detail":"el motor instalado no habla el protocolo esperado todavía","retryable":false}`
  a los **19,586 s**, con rc=1 y **stderr vacío**. Reproducible al 100 % (el
  propio envoltorio lo intentó dos veces —dos `stage machine`— y murió igual).
- **Causa, en una palabra**: `safent:1299` (dentro de `_run_with_heartbeat`) —
  `[ $((_hb_elapsed % 5)) -eq 0 ] && _stage_progress "$_hb_elapsed" "" seconds`.
  La unidad **`seconds`** no existe en el vocabulario **cerrado** de
  `app-engine.md` §3 (`bytes` · `layers` · `steps`). Del otro lado,
  `map_progress_unit` (`engine_adapter.rs:1244-1255`) es *fail-closed* a
  propósito: cualquier otra cadena devuelve
  `EngineError::Protocol("unknown progress unit: seconds")`, que el envoltorio
  convierte en `cli_porcelain_unsupported`, **no reintentable**.
- **Comprobado en vivo, contra el CLI directamente**:

  ```
  $CLI --no-companion ensure-machine --porcelain   (stdout crudo)
     5.064 {"t":"progress","id":"machine","done":5,"unit":"seconds"}
  ```

- **Por qué aparece justo ahora**: es **el arreglo de MAC4-05** (commit `3f61512`).
  `cmd_ensure_machine` pasó a envolver `machine init`/`machine start` con
  `_run_with_heartbeat` (`safent:2033`, `2037`, `2054`) — que era el latido
  **genérico ya existente**, sí, pero hasta ahora **sólo lo usaba el carril del
  compañero** (`companion_up`, `safent:1316`), que el envoltorio nunca ha
  ejercitado en ninguna de las cinco verificaciones. Su unidad ilegal llevaba
  ahí desde siempre; moverlo al camino crítico la encendió por primera vez.
  Mismo patrón que MAC4-01: una rama que nadie había ejecutado nunca.
- **Arreglo**: `seconds` → `steps` en `safent:1299`. Una palabra. (La alternativa
  —añadir `seconds` al contrato §3, a `ProgressUnit` y a `map_progress_unit`— es
  peor: cambia un vocabulario cerrado por comodidad de un solo emisor.) El test
  que acompañe al arreglo tiene que hacer pasar un NDJSON **con latido** por
  `map_progress_unit`, no sólo por el analizador de líneas: lo que falla no es el
  formato, es el valor.
- **Lo que ve el dueño**: la app se cierra sola a los veinte segundos diciendo
  que «el motor instalado no habla el protocolo esperado todavía», sin salida ni
  diagnóstico, y **no reintenta**. Suena a que el paquete está incompleto cuando
  está perfecto: es el propio producto hablándose mal a sí mismo.

### MAC5-02 · MAYOR · el `.app` del canal de **actualización** viaja con un manifiesto caducado

- **Evidencia**: el **mismo** artefacto publica dos copias del `.app`, con
  `runtime-bundle.json` **distintos**:

  | | `dmg/Safent_0.9.0_aarch64.dmg` | `macos/Safent.app.tar.gz` |
  |---|---|---|
  | tamaño del manifiesto | **3654 B** | **3464 B** |
  | `cdhash` no nulos | **8/8** | **3** (podman, vfkit, gvproxy) |
  | `cdhash` correctos | **8/8 IGUAL** | **0/3** |
  | sha256 de los 8 Mach-O | **8/8 casan** | **0/8 casan** |

  ```
  # el mismo binario, medido en los dos paquetes:
  podman  sha256 = aa5858a2a9dda08d5174…  (idéntico en DMG y en tar.gz)
  podman  cdhash real       = ea3733d42d13d5ecde9044eb9ebc52bf981f2a20
  podman  cdhash del DMG    = ea3733d42d13d5ecde9044eb9ebc52bf981f2a20   IGUAL
  podman  cdhash del tar.gz = 008317c69faa0223ea018464e5966256181b13e2   DISTINTO
  gvproxy sha256 en ejecución en el Mac = 7a1ca760b82bacf9… = el del tar.gz
  ```

  Los **binarios son byte a byte los mismos** en los dos paquetes; lo único que
  difiere es el manifiesto. El del `tar.gz` es una foto **anterior a la firma
  final**: registra los sha256 de antes de firmar (por eso ninguno de los 8
  Mach-O casa consigo mismo), tres `cdhash` de una firma intermedia y **cinco a
  `null`** (krunkit y los cuatro dylib), que sí están firmados.
- **Consecuencia**: `cmd_stage_runtime` (`safent:1934-1963`) verifica el paquete
  contra su **propio** manifiesto. Sobre el `.app` del `tar.gz` moriría en la
  entrada 4 (`podman`, la primera con `cdhash`) con
  `runtime_hash_mismatch` … `retryable:false`. Es decir: **la app se actualiza y
  deja de arrancar**. El primer arranque desde el DMG no se ve afectado.
- **Alcance**: US2 de 028 (actualización de un botón) y el `.sig` que la
  acompaña. No se pudo ejercitar el actualizador de punta a punta (hace falta una
  versión nueva publicada), así que queda demostrado sobre el artefacto, no sobre
  el gesto.
- **Arreglo**: que el `tar.gz` se genere **del mismo `.app` ya firmado y grapado**
  del que se genera el DMG, después de la firma, nunca antes — y un guardia de
  empaquetado que compare los dos `runtime-bundle.json` del artefacto y falle si
  difieren en un solo byte.

### MAC5-03 · MENOR · el contador de latidos se reinicia dentro de la misma etapa

- **Evidencia**: `ensure-machine` →
  `done:5` (5,064) · `done:5` (12,179) · `done:10` (17,229).
- **Causa**: `machine init` y `machine start` son **dos** llamadas a
  `_run_with_heartbeat` (`safent:2033`/`2054`), cada una con su propio
  `_hb_elapsed` empezando en cero, pero ambas dentro de la **misma** etapa
  `machine`. Un `progress.done` que baja dentro de una etapa viva es una barra de
  progreso que retrocede. `app-engine.md` §3 no lo prohíbe explícitamente — pero
  tampoco lo contempla, y `pull_engine` (`_pull_with_heartbeat`) sí es monótono.
- **Impacto**: cosmético hoy, y tapado por MAC5-01. Se arregla con el mismo
  cambio si el acumulador vive en la etapa y no en la llamada.

### MAC5-04 · MENOR · una etapa abierta dos veces sin cerrarse (MAC4-07/MAC3-08 sin cambio)

- **Evidencia**: pase 1 — `stage machine` en 5,387 y otra vez en 14,548, sin
  `done` ni `failed` entre medias, y **ninguna de las dos cierra**. Choca con
  `app-engine.md` §3 invariante 3 («cada `stage` cierra con exactamente un `done`
  **o** un `failed`»). En el camino feliz (pase 2, reapertura, reanudación) no
  ocurre: una apertura y un cierre para todas las etapas.

### MAC5-05 · MENOR · MAC4-08 sigue: el producto no aísla su podman del del dueño

- Los argumentos de vfkit lo enseñan tal cual:
  `--bootloader efi,variable-store=/tmp/safent-mac-test5/xdg-data/containers/podman/machine/applehv/…`
  Esa ruta es del **arnés** (`XDG_DATA_HOME`), no del producto. Sin él, la máquina
  de esta app nacería dentro del `~/.local/share/containers` del dueño. No
  reproducido a propósito: las reglas de la prueba lo prohíben.

### Arreglos del encargo que SÍ se confirman en vivo

| Arreglo | Estado | Evidencia |
|---|---|---|
| MAC4-01 · cdhash leído del canal correcto | **CONFIRMADO** | `runtime_staging` 17/17 en **1,26 s**, sin `SAFENT_CODESIGN`, rc del staging limpio |
| MAC4-02 · `up` idempotente | **CONFIRMADO** | reapertura: `48eabb037a60`/36645 → **el mismo**, «Up 8 minutes» intacto, 1,755 s |
| MAC4-03 · guardia con ruta completa | **CONFIRMADO (parcial)** | `ensure-machine` rc=0 con los helpers propios: **no hay falso positivo**. Un helper ajeno de verdad no se forzó |
| MAC4-04 · fallo porcelain limpio | **NO EJERCITADO** | se usó `--no-companion` en todos los pases; el carril que lo provoca es el del 029 |
| MAC4-05 · latidos en `machine` | **CONFIRMADO en el fondo, ROTO en la forma** | latidos cada ~5 s (hueco máx. 9,16 s, antes 20,93 s) — pero con la unidad ilegal de MAC5-01 |
| MAC4-06 · `.app` y DMG grapados | **CONFIRMADO** | `stapler validate` rc=0 en **los dos**; `codesign -dvv` → `Notarization Ticket=stapled` en los dos |

## Lo que no se pudo simular

- **Todo lo gráfico**: ventana, etapas pintadas, «Cancelar», barra de menús,
  instancia única, `Cmd+L`, Anuncios. No hay pantalla por SSH.
- **El doble clic con cuarentena**: se midió el veredicto de `spctl`/`stapler`, no
  el diálogo de Gatekeeper sobre un DMG descargado por Safari.
- **El compañero (029)**: `--selftest` sin compañero, por encargo. Su digest sí
  quedó verificado en el manifiesto, y MAC4-04 sigue sin ejercitarse.
- **El actualizador de punta a punta** (US2): MAC5-02 está demostrado sobre el
  artefacto publicado, no sobre el gesto de actualizar — requiere una versión
  nueva publicada.
- **El recorrido con `~/.safent`**: prohibido escribir en el home del dueño.
- **Un helper ajeno de verdad**: habría exigido tocar `/opt/podman` o el PATH del
  dueño.

## Desmontaje

```
Nombres reales verificados ANTES de borrar:
  contenedor  ef5ffc8e879b  mactest5  ghcr.io/devwspito/safent@sha256:9bafbad6…dbf8a3
  volumen     mactest5-data
  imagen      ghcr.io/devwspito/safent  365e584d7f5c  8.17 GB
  maquina     mactest5-engine  applehv  Currently running
  redes       podman            (no se creó safent-companions: todo fue --no-companion)

$RT rm -f mactest5                  -> mactest5
$RT volume rm -f mactest5-data      -> mactest5-data
$RT rmi -f ghcr.io/devwspito/safent -> rc=0
$RT machine stop mactest5-engine    -> Machine "mactest5-engine" stopped successfully
$RT machine rm -f mactest5-engine   -> (sin máquinas en el sandbox)
rm -rf /tmp/safent-mac-test5  (3,0 GB)  -> No such file or directory

mount | grep -c safent-mac-test5        -> 0
hdiutil info | grep -c safent-mac-test5 -> 0
pgrep -fl "safent-mac-test5|mactest5"   -> 0
pgrep -fl "podman run"                  -> 0
podman machine list (dueño)             -> podman-machine-default  libkrun  running
krunkit=20043  gvproxy=20042            (los mismos PID que al empezar)
~/.safent                               -> sigue sin existir
~/.local/share/containers/podman/machine/applehv -> sólo 'cache'
df -h /System/Volumes/Data              -> 75 Gi libres (81 Gi al empezar; el sandbox
                                           está borrado y no queda nada nuestro —
                                           /private/tmp tiene ficheros de OTROS
                                           proyectos, ajenos a esta prueba)
```

Todo lo creado por la prueba —máquina, contenedor, volumen, imagen, sandbox, DMG
montado— está retirado con el podman **empaquetado**, verificando los nombres
antes de cada borrado. La máquina del dueño quedó **arrancada e intacta**.

## Siguiente acción recomendada

1. **MAC5-01** — `seconds` → `steps` en `safent:1299`, y un test que haga pasar un
   latido real por `map_progress_unit`. Es lo único que impide arrancar.
   `debug-engineer`.
2. **MAC5-02** — generar `macos/Safent.app.tar.gz` **del `.app` ya firmado y
   grapado**, y un guardia de empaquetado que falle si los dos
   `runtime-bundle.json` del artefacto difieren. `devops-engineer`.
3. **MAC5-03 / MAC5-04** — acumulador de latidos por etapa (no por llamada); una
   sola apertura y un solo cierre por etapa, incluso en el camino de fallo.
4. **MAC4-04** — sigue sin ejercitarse: comprobar el carril del compañero (029).
5. **MAC5-05 (MAC4-08)** — aislar `XDG_CONFIG_HOME`/`XDG_DATA_HOME` desde el
   propio producto.

Cuando estén 1 y 2, esta misma prueba debería repetirse **entera y sin arneses**:
todo lo demás del encargo ya está verificado en vivo, y los seis arreglos de la
verificación 4 se sostienen.
