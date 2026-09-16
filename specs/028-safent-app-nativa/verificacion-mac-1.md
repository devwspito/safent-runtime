# Verificación 1 — app nativa de macOS en el MacBook del dueño

**Qué**: primera prueba real del artefacto notarizado `Safent-macOS-arm64`
(run 34536862732, repo `devwspito/agents-autonomy`, construido desde
`safent-runtime@feat/safent-next` en el merge D2, versión 0.9.0).
**Dónde**: MacBook Air del dueño (`macbook-air-2-1`), macOS 15.5, Apple Silicon
(arm64), 24 GB de RAM, 81 GB libres. Por SSH, **sin sesión gráfica**.
**Cuándo**: 11-sep-2026. **Quién**: exploratory-tester.
**Alcance**: hasta dónde llega el arranque headless (`--selftest`) del binario
empaquetado. Sin navegador, sin ventana, sin GUI.

## Veredicto

**FALLA.** El paquete está impecable por fuera (notarizado, grapado, firmado,
entitlements correctos, íntegro) y **no arranca en absoluto** en su plataforma
canónica: tres fallos bloqueantes independientes, cada uno suficiente por sí
solo para dejar al dueño en una pantalla de error sin salida. El recorrido murió
en la etapa 0 (`preflight`) y en la etapa 1 (`runtime_staging`); **nunca se llegó
a crear la máquina, ni a descargar el motor, ni a levantar el contenedor**.

| Bloque | Resultado |
|---|---|
| Integridad y traslado del DMG | **PASA** |
| Grapado (`stapler validate`) | **PASA** |
| Firma (`codesign --verify --deep --strict`) | **PASA** |
| Notarización (`spctl -a -vv -t exec`) | **PASA** |
| Entitlements de `krunkit` y `vfkit` | **PASA** |
| Ficheros de app (`app_files` del lock) | **PASA** (5/5) |
| sha256 del runtime contra el manifiesto del paquete | **FALLA** (8/15 no casan) |
| Selftest headless hasta `ready` | **FALLA** (muere en preflight) |
| Reanudación tras matar el contenedor | **NO EJECUTABLE** (no se llegó a haber contenedor) |
| Convivencia con el podman previo del dueño | **FALLA por diseño** (ver MAC-05) |

## Reglas de la prueba

Todo ocurrió bajo `/tmp/safent-mac-test/` con
`SAFENT_STATE_HOME=/tmp/safent-mac-test/state` y `SAFENT_NAME=mac-selftest-1`.

Se añadió **desde fuera** un arnés de aislamiento que la app **no tiene**:
`XDG_CONFIG_HOME` y `XDG_DATA_HOME` apuntando al sandbox. Sin él, el podman
empaquetado lee la configuración de máquinas del dueño
(`~/.config/containers`, `~/.local/share/containers`) y `cmd_ensure_machine`
habría **adoptado y arrancado la máquina libkrun viva del dueño**
(`podman-machine-default`). Ese arnés es prestado por la prueba, no del
producto: su necesidad **es** el hallazgo MAC-05.

Nunca se ejecutó `tailscale`. Nunca se tocó `/opt/podman`, `~/.config/containers`,
`~/.local/share/containers`, `~/.ssh` ni `podman-machine-default`. No se instaló
nada en `/Applications` ni se abrió ninguna app gráfica.

---

## Paso 1 — Descarga y traslado (PASA)

```
# DGX
gh run download -R devwspito/agents-autonomy 34536862732 -n Safent-macOS-arm64 -D <scratch>
  -> error downloading Safent-macOS-arm64: would result in path traversal
```
`gh run download` **rechaza este artefacto** (falso positivo de su guardia de
path traversal; el zip contiene un único fichero, `dmg/Safent_0.9.0_aarch64.dmg`,
sin ninguna ruta relativa). Se descargó por la API:

```
gh api /repos/devwspito/agents-autonomy/actions/artifacts/10175979813/zip > Safent-macOS-arm64.zip
unzip -l  ->  1006929683  dmg/Safent_0.9.0_aarch64.dmg   (zip: 1005864055 B)
unzip -p ... | ssh luiscorrea@macbook-air-2-1 'cat > /tmp/safent-mac-test/Safent_0.9.0_aarch64.dmg'
```

sha256 en los dos extremos, idéntico:
`d43108f05061b39006b9858b2f3973fccd9e2a8315b5f23936a5a0190bae7501`.
Tamaño real 1.006.929.683 B, dentro de la horquilla del lock
(`dmg_total_bytes_est_low` 1,02e9 / `high` 1,10e9) y muy por debajo del tope de
2 GiB de release de GitHub. **PASA.**

## Paso 2 — Grapado, firma y notarización (PASA)

```
xcrun stapler validate Safent_0.9.0_aarch64.dmg
  -> Processing: /private/tmp/safent-mac-test/Safent_0.9.0_aarch64.dmg
     The validate action worked!            rc=0

hdiutil attach -nobrowse -readonly -mountpoint /tmp/safent-mac-test/mnt Safent_0.9.0_aarch64.dmg
  -> todas las particiones "verificado CRC32"; /dev/disk4s1 montado    rc=0
     contenido: Safent.app, Applications -> /Applications, .VolumeIcon.icns

ditto mnt/Safent.app /tmp/safent-mac-test/Safent.app     (1,0 GB, 1,4 s)
hdiutil detach /tmp/safent-mac-test/mnt                  -> "disk4" ejected
```

```
codesign --verify --deep --strict --verbose=2 Safent.app
  -> valid on disk
     satisfies its Designated Requirement              rc=0

spctl -a -vv -t exec Safent.app
  -> accepted
     source=Notarized Developer ID
     origin=Developer ID Application: Luis Correa (JBMBA58A8X)   rc=0

codesign -dvvv Safent.app
  -> Identifier=com.safent.desktop
     Format=app bundle with Mach-O thin (arm64)
     CodeDirectory v=20500 flags=0x10000(runtime)     <- hardened runtime activo
     Authority=Developer ID Application: Luis Correa (JBMBA58A8X)
     Authority=Developer ID Certification Authority / Apple Root CA
     Timestamp=11 Sep 2026 at 00:23:19 · TeamIdentifier=JBMBA58A8X
```

Sin atributo de cuarentena (`xattr -l` vacío en la app y en `podman`).
El requisito de quickstart §1 —«ningún aviso de origen desconocido»— **se
cumple**: `source=Notarized Developer ID` es exactamente la respuesta que hace
que Gatekeeper no pregunte. **PASA.**

## Paso 3 — Entitlements de virtualización (PASA)

```
codesign -d --entitlements :- Contents/Resources/runtime/krunkit
  -> com.apple.security.hypervisor      = true
     com.apple.security.virtualization  = true
     (firmado Developer ID JBMBA58A8X, flags 0x10000(runtime))

codesign -d --entitlements :- Contents/Resources/runtime/vfkit
  -> com.apple.security.hypervisor      = true
     com.apple.security.virtualization  = true
     com.apple.security.network.client  = true
     com.apple.security.network.server  = true
     (firmado Developer ID JBMBA58A8X, flags 0x10000(runtime))
```
Los dos hipervisores pueden arrancar una VM. **PASA.**

Nota de ruta: la instrucción de la tarea buscaba
`Contents/Resources/runtime/*/bin/krunkit`; el árbol real está **aplanado**
(`Contents/Resources/runtime/krunkit`), tal y como documenta
`desktop/RUNTIME-BUNDLE.md` — el glob de `bundle.resources` aplana la estructura.
La documentación acierta; la expectativa con `bin/` no.

## Paso 4 — Integridad del runtime empaquetado (FALLA)

`Contents/Resources/runtime/` trae 17 ficheros (1,0 GB), de los cuales
`podman-machine.aarch64.applehv.raw.zst` son 931.934.236 B — el **93 %** del DMG.

Contraste de cada fichero contra `runtime-bundle.json` (el manifiesto que viaja
dentro del paquete y que `cmd_stage_runtime` lee de verdad en el equipo del
dueño):

| Fichero | manifiesto | real | veredicto |
|---|---|---|---|
| provision.sh | eeef142353 | eeef142353 | OK |
| caps.template.yaml | 1de55a1209 | 1de55a1209 | OK |
| **podman** | 7f0b4d70e7 | 11ec5be971 | **NO CASA** |
| **vfkit** | 1f3803030f | 6bb85639d8 | **NO CASA** |
| **krunkit** | d938398d96 | 0b1243fcdd | **NO CASA** |
| **gvproxy** | de135ddd9c | 2f864a114d | **NO CASA** |
| podman-machine.aarch64.applehv.raw.zst | b71b8a4e95 | b71b8a4e95 | OK |
| **libkrun.dylib** | edc0dac555 | a2de739533 | **NO CASA** |
| **libepoxy.0.dylib** | 779432cbbe | 59c3099ac6 | **NO CASA** |
| **libMoltenVK.dylib** | 4c5420eaf4 | 64930ba92a | **NO CASA** |
| **libvirglrenderer.1.dylib** | f4793d0492 | 559c82792e | **NO CASA** |
| compose.yaml | 502fd5789a | 502fd5789a | OK |
| run-safent.sh | b2a2b45624 | b2a2b45624 | OK |
| safent | 6f3e0c8e04 | 6f3e0c8e04 | OK |
| KRUN_EFI.silent.fd | 9ba725c245 | 9ba725c245 | OK |

**8 de 15 no casan, y son exactamente los 8 Mach-O.** Todo lo que no es un
binario (scripts, YAML, la imagen de máquina, el firmware EFI) casa al byte.
Ver hallazgo **MAC-02**.

Los 5 `app_files` del lock committeado sí casan con el checkout actual y con el
paquete (`safent` 6f3e0c8e…, `run-safent.sh` b2a2b456…, `provision.sh`
eeef1423…, `compose.yaml` 502fd578…, `caps.template.yaml` 1de55a12…). **PASA**
esa mitad.

`desktop/runtime-manifest.lock` **no tiene `entries[]` para
`aarch64-apple-darwin`** (sólo el sha256 del `.pkg` exterior, el del `.tgz` de
krunkit y el `blob_digest` de la imagen de máquina). Los hashes por fichero de
macOS sólo existen dentro del paquete. Ver **MAC-08**.

## Paso 5 — Selftest headless (FALLA)

Binario: `/tmp/safent-mac-test/Safent.app/Contents/MacOS/safent-desktop --selftest`
(modo sin compañero: `--selftest` a secas pasa `companion_image: None`).

### Transcripción NDJSON completa

**Pase A — tal cual lo viviría el dueño al abrir la app (sin variables):**
```
{"kind":"failed","code":"cli_porcelain_unsupported","detail":"SAFENT_ENGINE_DIGEST no está definido (falta el manifiesto del runtime)","retryable":false}
```
`rc=1`, stderr vacío, duración < 1 s. **Cero etapas.** → **MAC-03**

**Pase B — con el digest publicado, dejando que la app resuelva sola su runtime:**
```
{"kind":"failed","code":"daemon_unhealthy","detail":"no pude ejecutar 'facts': No such file or directory (os error 2)","retryable":true}
```
`rc=1`, stderr vacío, < 1 s. `Contents/MacOS/` contiene **sólo** `safent-desktop`;
no existe ningún `Contents/MacOS/runtime/`. → **MAC-04**

**Pase C — con `SAFENT_RUNTIME_DIR` apuntando a la ruta real del paquete:**
```
{"kind":"failed","code":"unsupported_os","detail":"este sistema operativo no está servido","retryable":false}
```
`rc=1`, stderr vacío, 6 s (00:43:14 → 00:43:20). El CLI **sí** se ejecutó (dejó
`state/podman/storage.conf` y el árbol XDG del sandbox), observó el equipo, y el
envoltorio declaró macOS 15.5 arm64 «sistema operativo no servido». → **MAC-01**

Entorno del pase C:
```
SAFENT_RUNTIME_DIR=/tmp/safent-mac-test/Safent.app/Contents/Resources/runtime
SAFENT_ENGINE_DIGEST=sha256:52b6478472e54d726f5123b5bc0349435dd3597c48081f4e3df10e42eb0c4716
SAFENT_STATE_HOME=/tmp/safent-mac-test/state   SAFENT_NAME=mac-selftest-1
XDG_CONFIG_HOME=/tmp/safent-mac-test/xdg/config  XDG_DATA_HOME=/tmp/safent-mac-test/xdg/data
```

### Etapas: esperadas contra alcanzadas

| Etapa esperada | Alcanzada | Duración |
|---|---|---|
| `preflight` | sí — **falla** `unsupported_os` | 6 s |
| `runtime_staging` | sólo por el CLI directo — **falla** `runtime_hash_mismatch` | < 1 s |
| `machine` (creación rootful, nombre propio, imagen empaquetada) | **NO** | — |
| `pull_engine` | **NO** | — |
| `container` | **NO** | — |
| `health` | **NO** | — |
| `ready` | **NO** | — |

No hay máquina creada, ni CPU/memoria/disco elegidos, ni digest de motor
aplicado, ni puerto, ni vale de arranque que registrar. El paso (3) del encargo
—matar el contenedor con `podman kill` y repetir el selftest para probar la
reanudación— **no es ejecutable**: nunca hubo contenedor.

## Paso 6 — El CLI embebido por separado (aísla la culpa)

Para separar «el envoltorio está roto» de «el CLI está roto», se invocó el CLI
empaquetado directamente con el podman empaquetado:

```
Contents/Resources/runtime/podman --version   ->  podman version 6.1.1

# aislamiento verificado ANTES de nada: el sandbox no ve ninguna máquina
XDG_CONFIG_HOME=... XDG_DATA_HOME=... podman machine list
  -> NAME  VM TYPE  CREATED  LAST UP  CPUS  MEMORY  DISK SIZE      (vacío)

safent facts --json
  {"os": "darwin",  "arch": "arm64",
   "freeDiskBytes": 84585459712, "totalMemoryBytes": 25769803776,
   "runtimeStaged": false, "runtimeHashOk": false, "machines": [],
   "localEngineImageDigest": null, "userNsAllowed": true, "helperInstalled": false}

safent stage-runtime --porcelain
  {"t":"stage","id":"runtime_staging","label":"Preparando la base de ejecucion"}
  {"t":"progress","id":"runtime_staging","done":1,"total":15,"unit":"steps"}
  {"t":"progress","id":"runtime_staging","done":2,"total":15,"unit":"steps"}
  {"t":"failed","id":"runtime_staging","code":"runtime_hash_mismatch","detail":"podman no coincide con el manifiesto","retryable":false}
  rc=14
```

El CLI se porta bien: emite NDJSON válido por stdout, cierra la etapa con
exactamente un `failed`, y el código de salida 14 cae en el rango 10..39 que
exige `contracts/app-engine.md` §2. Falla **por el contenido del paquete**, no
por su propia lógica. El `"os": "darwin"` que emite es la otra mitad de MAC-01.

---

## Hallazgos

### MAC-01 · BLOQUEANTE · macOS entero declarado «no servido»
- **Evidencia**: `{"kind":"failed","code":"unsupported_os","detail":"este sistema operativo no está servido","retryable":false}` en un MacBook Air con macOS 15.5 arm64.
- **Causa**: `safent` (`cmd_facts`) emite `"os":"darwin"`; `desktop/src-tauri/src/engine_adapter.rs:814-819` (`map_os`) sólo reconoce `"macos"` y `"linux"`, así que devuelve `HostOs::Unsupported`; `desktop/src-tauri/src/reconcile.rs:31-35` lo convierte en `unsupported_os` **no reintentable**.
- **Efecto en el dueño**: la app muere en el primer latido, en su plataforma canónica, con un mensaje que le dice que su Mac no está soportado. Sin reintento, sin salida.
- **Arreglo**: una línea — aceptar `"darwin"` en `map_os` (o que el CLI emita `"macos"`). Hay que decidir cuál de los dos es el contrato: `data-model.md` no fija el vocabulario de `os`, y esa ausencia es lo que dejó pasar el desajuste.

### MAC-02 · BLOQUEANTE · la firma invalida los hashes del propio paquete
- **Evidencia**: `{"t":"failed","id":"runtime_staging","code":"runtime_hash_mismatch","detail":"podman no coincide con el manifiesto","retryable":false}` (rc=14), y la tabla del paso 4: los 8 Mach-O no casan, los 7 no-Mach-O casan todos.
- **Causa**: `stage-runtime.sh` calcula los sha256 **antes** de firmar; el pipeline de notarización re-firma cada Mach-O del bundle (confirmado: `krunkit` y `vfkit` llevan `Authority=Developer ID Application: Luis Correa`), lo que cambia sus bytes. `safent:1765` compara contra el hash pre-firma y muere cerrado.
- **Nota**: es **el mismo fallo que el lock ya documenta para AppImage** («correctly, and permanently, fails runtime_hash_mismatch for an AppImage build»), aplicado al DMG notarizado, que es la entrega principal. Se descartó AppImage por esto y se dejó pasar en macOS.
- **Efecto**: aunque se arregle MAC-01, la app muere en la etapa siguiente. Sin reintento posible: `retryable:false`.
- **Arreglo**: capturar los hashes **después** de firmar (el runner de firma reescribe `runtime-bundle.json`), o verificar los Mach-O por firma (`codesign --verify`) en vez de por sha256, y dejar el sha256 sólo para lo que nadie firma.

### MAC-03 · BLOQUEANTE · el digest del motor no lo pone nadie
- **Evidencia**: `{"kind":"failed","code":"cli_porcelain_unsupported","detail":"SAFENT_ENGINE_DIGEST no está definido (falta el manifiesto del runtime)","retryable":false}` — el pase A **es** el doble clic del dueño.
- **Causa**: `desktop/src-tauri/src/boot.rs:704-707` exige `SAFENT_ENGINE_DIGEST` del entorno. Nada en el paquete, en `tauri.conf.json`, en el `Info.plist` ni en el arranque lo define. `runtime-manifest.lock` **no tiene ninguna entrada de imagen de motor**: fija podman, gvproxy, vfkit, krunkit y la imagen de máquina, pero no `ghcr.io/devwspito/safent`. El propio comentario del código lo llama «la costura hasta que aterrice el manifiesto»; no ha aterrizado.
- **Agravante — el digest de 0.9.0 no existe**: `ghcr.io/devwspito/safent` publica 20 etiquetas, la más alta `0.8.42`; **no hay `0.9.0`** (`GET /v2/.../manifests/0.9.0` → HTTP 404). `latest` resuelve a `sha256:52b6478472e54d726f5123b5bc0349435dd3597c48081f4e3df10e42eb0c4716` (índice OCI multi-arch: arm64 = `sha256:1121ff249db9e3ab8a7d7ca2b9ff5410ac0fa8f6b0669d156cd55c4cf56ab08e`). Una app 0.9.0 que exigiera «su» digest no tendría a dónde apuntar. Aquí se paró y se reporta, como pedía el encargo: **no se inventó ningún rodeo**; el pase C usó el digest publicado de `latest` únicamente porque el propio código documenta esa variable como costura de desarrollo.
- **Efecto**: sin publicar la imagen 0.9.0 y sin fijar su digest en el manifiesto, la app no puede arrancar aunque se arreglen MAC-01 y MAC-02.

### MAC-04 · MAYOR · el selftest busca el runtime donde un `.app` nunca lo tiene
- **Evidencia**: `{"kind":"failed","code":"daemon_unhealthy","detail":"no pude ejecutar 'facts': No such file or directory (os error 2)","retryable":true}`; `Contents/MacOS/` sólo contiene `safent-desktop`.
- **Causa**: `desktop/src-tauri/src/selftest.rs:113-116` resuelve `exe.parent()/runtime` → `Contents/MacOS/runtime`. En un `.app` los recursos van a `Contents/Resources/runtime` (que es lo que `boot::resolve_config` obtiene bien vía `resource_dir()`). El comentario de `boot.rs:783` afirma que el selftest «ya lo hace bien»; acierta en lo del triple aplanado y falla en el salto `MacOS/` → `Resources/`.
- **Segundo defecto en la misma línea**: el fallo se clasifica como `daemon_unhealthy` con `retryable:true`. No hay ningún demonio: falta el binario. Un `retryable:true` sobre una ruta que no puede existir jamás es un **bucle de reintento infinito sin progreso**; el contrato reserva `cli_porcelain_unsupported` justo para esto.
- **Efecto**: la única puerta headless del producto —la que exige `quickstart.md` para verificar sin pantalla— no funciona desde el paquete firmado sin una variable de entorno que nadie documenta.

### MAC-05 · BLOQUEANTE de convivencia · la app adopta la máquina del dueño
- **Evidencia**: en este Mac hay una máquina libkrun **viva** del dueño, arrancada por su podman de `/opt/podman/bin`: `krunkit --cpus 4 --memory 8192 ... podman-machine-default-arm64.raw` + `gvproxy ... podman-machine-default-gvproxy.sock`. Con el arnés XDG puesto, `podman machine list` del paquete devuelve **vacío**; sin él, devolvería esa máquina.
- **Causa**: `safent:1788` — `cmd_ensure_machine` hace `"$RT" machine list -q | head -1` y adopta **la primera máquina que haya, sea de quien sea**. No usa un nombre propio, no aísla `XDG_CONFIG_HOME`/`XDG_DATA_HOME` (el CLI sí aísla el **almacenamiento** con `CONTAINERS_STORAGE_CONF`, `safent:150-161`, pero las máquinas viven fuera de eso), y si esa máquina es rootless muere con `machine_create_failed` / `retryable:false` («La maquina existente es rootless») — callejón sin salida permanente.
- **Choque con la especificación**: `quickstart.md` §2 exige adoptarla «si sirve» o dejarla **intacta** y crear la propia; §9.3 exige que «el podman empaquetado se va con la app». Aquí la app arrancaría la máquina del dueño y metería su contenedor dentro; al desinstalar no puede distinguir cuál es suya. Agravante: `cmd_ensure_machine` **nunca escribe `machine.json`**, que es justo lo que `_machines_json` (`safent:1633`) lee para decidir `ours`; por tanto `ours` será siempre `false` y `uninstall --scope this-install` no tiene forma de saber qué creó.
- **Agravante de versión**: el podman empaquetado es **6.1.1** y la máquina del dueño la creó un podman **5**; adoptarla implica además un salto de esquema de configuración no probado.
- **Nota de esta prueba**: por eso el arnés XDG fue obligatorio. Es una carencia del producto, no de la prueba.

### MAC-06 · MAYOR · la imagen de máquina de 932 MB viaja y no se usa
- **Causa**: `safent:1790` — `machine init --rootful --cpus 4 --memory 8192 --disk-size 60`, **sin `--image`**. El `podman-machine.aarch64.applehv.raw.zst` empaquetado (931.934.236 B, el 93 % del DMG) no se le pasa nunca; podman se descargaría la imagen de `quay.io`.
- **Choque**: `contracts/app-engine.md` §4 dice que `ensure-machine` «crea la nuestra desde la imagen empaquetada, **sin red**».
- **Colateral**: `MachineSpec` de `boot.rs:740-745` (applehv, 4 CPU, **6 GiB**, os 6.1) es **letra muerta** — el CLI no acepta parámetros y fija 4/8192/60 a pelo. El envoltorio cree que decide el tamaño de la máquina; no decide nada.
- **Efecto**: el dueño paga 932 MB de descarga y de disco por un fichero que no se usa, y aun así necesita red para crear la máquina.

### MAC-07 · MENOR · falta `containers.conf` en el bundle de macOS
- **Evidencia**: los 17 ficheros de `Contents/Resources/runtime/` listados; no hay `containers.conf`.
- **Causa/efecto**: `safent:145` fija `CONTAINERS_CONF` sólo si existe `containers.conf` junto al podman empaquetado. En macOS no existe, así que el podman empaquetado cae en la configuración del sistema o del dueño. En macOS el impacto real es bajo (el cliente habla con la máquina, y `lock_type`/`helper_binaries_dir` importan dentro de la VM), pero es una desviación silenciosa de lo que `RUNTIME-BUNDLE.md` describe como parte del contrato del paquete.

### MAC-08 · MENOR (proceso) · el lock no permite auditar macOS desde el repo
- `runtime-manifest.lock` no tiene `entries[]` para `aarch64-apple-darwin`: sólo el sha256 del `.pkg` (9c7b90b4…), el del `.tgz` de krunkit (de9ab62d…) y el `blob_digest` de la imagen de máquina (b71b8a4e…, que **sí** casa con el fichero empaquetado). Los hashes por fichero de macOS sólo nacen en el runner, dentro de `runtime-bundle.json`. La verificación pedida «contra el lock» sólo es posible para los 5 `app_files`; para los binarios hay que fiarse del paquete, que es precisamente lo que MAC-02 demuestra que no se puede.

### MAC-09 · MENOR (tooling) · `gh run download` no puede bajar este artefacto
- `gh run download -R devwspito/agents-autonomy 34536862732 -n Safent-macOS-arm64` aborta con `would result in path traversal`, pese a que el zip contiene un único fichero sin rutas relativas. Hay que usar `gh api .../artifacts/<id>/zip`. Afecta a cualquiera que siga el runbook tal cual.

## Lo que no se pudo simular

- **Todo lo posterior a `preflight`/`runtime_staging`**: máquina, descarga del motor, contenedor, salud, `ready`, entrega del vale por `--secret-fd`, y la reanudación tras `podman kill`. Los tres bloqueantes lo impiden y arreglarlos exige tocar código, fuera del alcance de esta inspección.
- **Todo lo gráfico**: ventana, etapas nombradas, «Cancelar», barra de menús, instancia única, `Cmd+L`, Anuncios. No hay pantalla por SSH.
- Revisión estática favorable, sin ejecución: la entrega del vale por descriptor está **bien** implementada en los dos lados (`safent:1841-1851` escribe una línea en `/dev/fd/N`; `engine_adapter.rs:954-1029` crea el pipe y lo ancla en el fd 3 con `pre_exec`), y el bucle de salud de `cmd_up` emite progreso cada 5 s durante 48 vueltas, respetando NFR-001/002.

## Desmontaje

```
podman machine list -q (en el sandbox)  -> vacío   # no se creó ninguna máquina: nada que parar ni borrar
pgrep -fl safent-mac-test               -> vacío   # ningún proceso del paquete vivo
mount | grep -c safent-mac-test         -> 0       # DMG desmontado
rm -rf /tmp/safent-mac-test             -> rc=0
ls -d /tmp/safent-mac-test              -> No such file or directory
ls -d ~/.safent                         -> No such file or directory   (no existía antes; sigue sin existir)
```
La máquina `podman-machine-default` del dueño sigue **arrancada e intacta**,
igual que antes de empezar. No se ejecutó ningún `machine stop`/`rm` porque el
selftest no llegó a crear ninguna máquina.

## Siguiente acción recomendada

Por orden, cada uno es requisito del siguiente:

1. **MAC-01** — `map_os` acepta `darwin` (o el CLI emite `macos`) y se fija el
   vocabulario en `data-model.md`. Una línea + un test que recorra el
   vocabulario completo de `os`.
2. **MAC-03** — publicar la imagen del motor 0.9.0 y fijar su digest en
   `runtime-manifest.lock`, con `boot.rs` leyéndolo del manifiesto en vez del
   entorno. Mientras no exista, ninguna app 0.9.0 puede arrancar.
3. **MAC-02** — que los hashes del runtime se capturen después de firmar.
4. **MAC-05** — nombre de máquina propio, aislamiento de `XDG_*`, y escribir
   `machine.json` al crearla.
5. **MAC-06** — pasar `--image` con la imagen empaquetada (o dejar de
   empaquetarla y ahorrar 932 MB).
6. **MAC-04** — arreglar la ruta del selftest y su código de fallo, para que la
   próxima verificación headless no necesite variables no documentadas.

Especialistas: `debug-engineer` para MAC-01/MAC-04 (mecánicos), `devops-engineer`
para MAC-02/MAC-03/MAC-09 (pipeline y registro), `backend-engineer` +
`software-architect` para MAC-05/MAC-06 (contrato de máquina y adopción).
