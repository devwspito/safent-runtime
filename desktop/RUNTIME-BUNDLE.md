# Runtime empaquetado — cómo se rellena y cómo se consume

Fuente de verdad: `desktop/runtime-manifest.lock` (committeado). `desktop/scripts/
stage-runtime.sh` es el único programa que lo lee y el único que escribe bajo
`desktop/src-tauri/resources/runtime/` (gitignorado — nunca se commitea el
runtime en sí, sólo sus pines).

**`desktop/src-tauri/resources/runtime/<triple>/` sólo puede contener ficheros
YA EXTRAÍDOS** (binarios, `.dylib`, configs, la imagen de máquina `.raw.zst` —
esa sí es el artefacto final). **Nunca** un `.tgz`/`.zip`/`.pkg`/`.part` de
descarga, ni siquiera dentro de un subdirectorio oculto gitignorado: el glob de
`bundle.resources` (`resources/runtime/*/**/*`) barre TODO lo que hay debajo,
`.dotdirs` incluidos, y Apple notarytool rechazó de verdad la app la primera
vez que un `.tgz` viajó ahí dentro (inspecciona el contenido no firmado del
propio archivo). La caché de descargas de `stage-runtime.sh` vive en
`desktop/.cache/runtime-downloads/<triple>/` — **fuera** de `src-tauri/
resources/` por completo. Si el pipeline cachea esa carpeta (`actions/cache`),
la clave/ruta a cachear es esa, no la antigua `resources/runtime/.cache/`.
`desktop/scripts/tests/test-packaged-layout.sh` falla si algo de esto se
rompe otra vez (junto con la comprobación de los 5 ficheros de app de abajo:
es UN solo script, dos contratos).

## El CLI `safent` + sus vecinos también viven aquí (decisión, revisión de
## empaquetado Linux, `specs/028-safent-app-nativa/verificacion-paquete-linux.md`)

Hallazgo bloqueante: el paquete firmado (.deb/.rpm/AppImage) no llevaba el CLI
`safent` en ningún sitio — `boot.rs::resolve_config` lo resuelve como
`runtime_dir.join("safent")`, así que la app empaquetada no podía invocar
ningún verbo del contrato. **Decisión: se quedan en `stage-runtime.sh`/
`runtime-manifest.lock`, no en `bundle.resources` aparte.** Alternativa
descartada — un SEGUNDO patrón de `tauri.bundle.conf.json` apuntando
directamente a `./safent`/`ops/container/...`: habría introducido una SEGUNDA
fuente de verdad para qué vive bajo `resources/runtime/`, exactamente lo que
este documento existe para evitar (una única fuente, un único escritor).

`stage-runtime.sh` copia, **planos** (mismo nombre de fichero, sin
subcarpetas — igual que el resto del árbol una vez lo aplana el glob de
Tauri), estos cinco ficheros del propio checkout (nunca se descargan — no son
un tercero fijado, son código de este repo):

| Origen (relativo a la raíz del repo) | Nombre plano en el paquete |
|---|---|
| `safent` | `safent` |
| `ops/container/run-safent.sh` | `run-safent.sh` |
| `ops/container/companions/ads/provision.sh` | `provision.sh` |
| `ops/container/companions/ads/compose.yaml` | `compose.yaml` |
| `ops/container/companions/ads/caps.template.yaml` | `caps.template.yaml` |

Dos escrituras distintas, con semántica distinta:

1. **`runtime-manifest.lock` → `.app_files`** — igual de comprometido que
   `.targets[]`, pero **recalculado en cada ejecución** desde el checkout
   actual, no fijado a mano contra una fuente externa: es código nuestro, no
   un tercero. Sirve de provenance/auditoría (qué hash tenía `safent` en el
   momento de este build), no de verificación en tiempo de ejecución.
2. **`resources/runtime/<triple>/runtime-bundle.json`** — generado también en
   cada ejecución, **dentro del árbol staged** (gitignorado, viaja con el
   paquete). Es lo que `cmd_stage_runtime` (`safent`) lee de verdad en el
   equipo del dueño — `bundle_dir/runtime-bundle.json`, sentado junto al
   propio script una vez aplanado — para copiar y verificar cada fichero
   hacia `$SAFENT_STATE_HOME/runtime/<versión>/` antes del primer uso real
   (data-model.md, invariante `RuntimeBundle`: «un binario que no verifica
   no se ejecuta jamás»). También carga `engine_image`/`companion_image`
   (repo+digest, copiados de `runtime-manifest.lock`'s campos del mismo
   nombre) — `boot.rs`/`selftest.rs` los leen de aquí, nunca de un env var.

   **Verificación por fichero (decisión del dueño, 11-sep-2026 — "el
   código más simple es el que funciona mejor")**: en macOS, la integridad
   es la propia firma de Apple, nada más — `cmd_stage_runtime` ejecuta UN
   `codesign --verify --strict` superficial sobre el `.app` que contiene el
   runtime (su sello `CodeResources` ya cubre criptográficamente cada
   fichero bajo `Contents/Resources/`); en Linux, el manifiesto `sha256`
   sigue exactamente igual que siempre (se calcula en el staging y nada
   muta los ficheros después). El mecanismo anterior — un `cdhash` por
   fichero, `stage-runtime.sh --refresh-bundle-json` reescaneando el `.app`
   YA FIRMADO tras notarizar — quedaba re-verificando lo que Apple ya
   verifica, y fue el origen directo de dos bloqueantes reales (MAC4-01:
   el propio consumidor leía el canal equivocado de `codesign -d`;
   MAC5-02: el manifiesto del `.app.tar.gz` del actualizador quedaba
   caducado frente a sus propios binarios). Retirado por completo.

`normalize-staged-tree.sh`'s `_EXECUTABLE_BASENAMES` incluye `safent`,
`run-safent.sh` y `provision.sh` (0755); `compose.yaml`/`caps.template.yaml`
quedan en el 0644 por defecto.

## Entrada del script: TRIPLE de Rust, no un nombre corto

```
desktop/scripts/stage-runtime.sh <triple>
```

Acepta exactamente los mismos triples que ya usa la matriz de
`agents-autonomy/.github/workflows/safent-desktop.yml` — sin capa de
traducción en el pipeline:

| Triple | Qué stagea | Lleva máquina |
|---|---|---|
| `aarch64-apple-darwin` | podman + gvproxy + vfkit (del `.pkg` oficial, expandido con `pkgutil --expand-full`, **nunca instalado**) + krunkit + imagen de máquina | sí (932 MB) |
| `x86_64-unknown-linux-gnu` | podman-static v6.1.1 amd64 (subconjunto curado) | no |
| `aarch64-unknown-linux-gnu` | podman-static v6.1.1 arm64 (subconjunto curado) | no |

`x86_64-apple-darwin` es un triple **reconocido y rechazado a propósito**: v6.1.1
no publica instalador macOS Intel (sólo `podman-installer-macos-arm64.pkg` +
`podman-remote-release-darwin_arm64.zip`), y coincide con `contracts/update.md`
(`darwin-x86_64` ausente a propósito en `latest.json`). Cualquier otro valor se
rechaza con un mensaje que lista los tres triples válidos. `aarch64-apple-darwin`
sólo puede stagearse en un runner Darwin (usa `pkgutil`); en Linux falla rápido
con un mensaje claro en vez de hacer trabajo a medias.

El script es idempotente (si lo ya stageado casa sha256 con el lock, no vuelve a
tocar la red) y falla cerrado (sha256 o tamaño que no casa ⇒ borra y aborta;
nunca deja un binario a medio verificar).

## Dónde caen los ficheros (contrato para quien consuma esto — T009)

`bundle.resources` usa **un** patrón glob — `"resources/runtime/*/**/*":
"runtime"` — porque en cualquier build real sólo existe UN triple bajo
`resources/runtime/` (el que ese runner stageó) y porque ni `MacConfig` ni
`LinuxConfig` (verificado contra `tauri-utils` 2.9.3, el `Cargo.lock` real de
este crate) tienen un campo `resources` propio — `bundle.macOS`/`bundle.linux`
sólo traen `files`/`dmg`/`deb`/`appimage`/`rpm`, nada que sirva para esto. Un
patrón glob en `bundle.resources` **aplana** la estructura (comportamiento
verificado leyendo `tauri-utils::resources` y probado con el `glob` crate real
0.3.3 contra el árbol stageado de verdad): en el paquete final, **todo** queda
directo bajo `$RESOURCES/runtime/<nombre>`, sin `bin/`/`libexec/podman/`/
`etc/containers/` — cada nombre de fichero es único en todo el conjunto (macOS
+ Linux), así que aplanar no pisa nada. En tiempo de ejecución:
`app.path().resource_dir()?.join("runtime").join("podman")`, etc.

### `bundle.resources` vive en un overlay, NO en `tauri.conf.json`

`tauri-build`'s propio `build.rs` (`tauri_build::try_build`, no sólo `tauri
build`) resuelve el glob de `bundle.resources` en **cada** `cargo build`/
`clippy`/`test` — con `resources/runtime/` vacío o ausente (checkout limpio,
sin `stage-runtime.sh` corrido) el glob no matchea nada y `try_build` hace
panic: `glob pattern resources/runtime/*/**/* path not found or didn't match
any files`. Se comprobó en vivo (gate roto en trunk con checkout limpio) y se
reprodujo aquí a propósito para confirmarlo antes de corregirlo.

Corrección: `bundle.resources` vive **sólo** en
`desktop/src-tauri/tauri.bundle.conf.json` (overlay, nunca en
`tauri.conf.json`). `cargo build`/`clippy`/`test` normales no lo ven — pasan
en un checkout limpio sin nada stageado (verificado: `rm -rf
resources/runtime/*` + `cargo fmt --check && cargo clippy --all-targets --
-D warnings && cargo test`, limpio). El build de producción lo aplica
explícitamente, **después** de `stage-runtime.sh` para el target de ese
runner:

```sh
desktop/scripts/stage-runtime.sh <triple>          # puebla resources/runtime/<triple>/
cd desktop/src-tauri
TAURI_CONFIG="$(cat tauri.bundle.conf.json)" cargo tauri build   # o: tauri build --config tauri.bundle.conf.json
```

(`--config`/`TAURI_CONFIG` son el mismo mecanismo — `tauri-build` lee
`TAURI_CONFIG` directamente en `build.rs` y hace `json_patch::merge` sobre la
config base; no requiere el CLI `tauri` instalado para probarlo.) Probado sin
el CLI (no está instalado en esta DGX): con el overlay + el target stageado,
`TAURI_CONFIG="$(cat tauri.bundle.conf.json)" cargo build` termina en verde y
copia exactamente los 16 ficheros esperados (aplanados) a
`target/debug/runtime/`; con el overlay puesto y `resources/runtime/` vacío,
el MISMO comando reproduce el panic exacto de arriba — confirma que el overlay
ejercita el glob de verdad, no es un no-op.

`plugins.updater.pubkey` se queda en `tauri.conf.json` (no en el overlay): el
placeholder `__TAURI_UPDATER_PUBKEY__` no es una ruta ni un glob, `try_build`
no lo valida — confirmado, no rompe ningún `cargo build`/`clippy`/`test` con o
sin overlay.

`bundle.linux.deb.post_install_script` es donde va el postinst que instala el
ayudante privilegiado (perfil AppArmor + `newuidmap`/`newgidmap` +
`subuid`/`subgid`) — **no** lo toca esta entrega; es de `T011`
(`bootstrap_service.rs`/`privilege_helper.rs`, otro lane). Ver
`research.md` → «Decisión: Linux sin VM» → «Verificación en vivo» para la
condición exacta que dispara ese ayudante (no es «siempre en Ubuntu 24.04+»,
es `HostFacts.userNsAllowed == false`, medido con una sonda barata).

## Tamaños medidos (10-sep-2026, valores reales, no estimados)

| Triple | Descarga verificada | Stageado (subconjunto curado) |
|---|---|---|
| `aarch64-unknown-linux-gnu` | 31 857 193 B (podman-static) | 71 122 384 B ≈ 67,8 MiB |
| `x86_64-unknown-linux-gnu` | 34 628 342 B (podman-static) | 75 352 816 B ≈ 71,9 MiB |
| `aarch64-apple-darwin` | 76 334 314 B (`.pkg`) + 6 434 546 B (krunkit) + 931 934 236 B (imagen de máquina, por digest) | envoltorio Tauri ~12 MB + podman/gvproxy/vfkit/krunkit ~80 MB + imagen 932 MB ≈ **1,02–1,10 GB** |

Límite de GitHub Releases: **2 GiB por fichero**. El DMG macOS queda a ~45 % de
margen. Linux no lleva máquina — ni de lejos cerca del límite.

## Decisión Linux: rootless por defecto (confirmado en esta DGX)

Verificado en vivo (contenedores `desk2-*`, destruidos): el cage completo
(systemd PID1, Landlock, netns+nftables del navegador/MCP, mismas flags de
`run-safent.sh`) pasa **rootless**, tanto con el podman del sistema (perfilado)
como con un podman **reubicado y sin perfil AppArmor** (el sustituto más fiel
posible del binario empaquetado). El ayudante privilegiado no se ejecuta
incondicionalmente — sólo cuando una sonda barata (`<podman empaquetado>
unshare true`) confirma que el kernel lo exige. Detalle completo, con los
comandos exactos, en `research.md`.

## Publicación de puertos rootless (pasta) — verificado, no era el pasta

Se investigó una sospecha de que el `pasta` empaquetado por `podman-static`
v6.1.1 (`passt 2026_06_11.a9c61ff`) fuera demasiado antiguo para el
`--map-guest-addr` que podman 6.1.1 invoca — **no se reprodujo**: el propio
binario `--help` ya lista esa opción, y se probó en vivo, dos veces, con el
podman/netavark/pasta EMPAQUETADOS reales (no el del sistema): un contenedor
alpine con `-p 127.0.0.1:PORT:80` y la imagen real
`ghcr.io/devwspito/safent:0.8.42` con `-p 127.0.0.1:PORT:7517` — ambos
respondieron por curl (HTTP 307 en el segundo caso). El hallazgo real, sí
confirmado y corregido: sin `helper_binaries_dir` en `containers.conf`, el
podman empaquetado usaba en silencio el netavark **del sistema** (1.4.0) en
vez del empaquetado (2.1.0) en esta misma DGX. `stage-runtime.sh` ahora
parchea el `containers.conf` extraído (`lib/patch-containers-conf.sh`) con
`helper_binaries_dir = ["$BINDIR/../libexec/podman", "$BINDIR"]` (`$BINDIR` es
el token de containers-common para "directorio del podman que se está
ejecutando ahora", resuelto en tiempo real — válido tanto aquí como en el
destino final `~/.safent/runtime/<versión>/`) + `default_rootless_network_cmd
= "pasta"`. `conmon_path`/el runtime OCI (crun) usan un mecanismo DISTINTO que
no entiende `$BINDIR` — pendiente para quien cablee la invocación real
(T009/T011): pasarle rutas absolutas explícitas en tiempo de ejecución, no
algo fijado en este fichero en tiempo de stage.

Hallazgo independiente de otro lane contra el MISMO fichero, ya fusionado
aquí (`specs/028-safent-app-nativa/verificacion-paquete-linux.md`, «Pasada
1»): un podman empaquetado (musl estático) y el podman/docker del propio
host (glibc), corriendo con el mismo uid, chocan en UN solo segmento
`/dev/shm` de locks rootless que cada libc dimensiona distinto —
reproducido en vivo: «failed to open 2048 locks in
/libpod_rootless_lock_1000: numerical result out of range» (el mismo
síntoma que esta investigación del pasta encontró por una vía distinta al
probar contra un host compartido). `patch_containers_conf_for_isolated_locks`
añade `lock_type = "file"` — locks por fichero, uno por árbol de
almacenamiento, sin segmento compartido por uid posible. Las dos
transformaciones (`_bundled_helpers` + `_isolated_locks`) se aplican juntas,
en `_patch_containers_conf`, y se verifican contra un único hash combinado
(`containers_conf_patch` en el lock).

## Actualizador de Tauri: clave y manifiestos

`tauri.conf.json` → `plugins.updater.pubkey` lleva el placeholder literal
`__TAURI_UPDATER_PUBKEY__`. El pipeline (`safent-desktop.yml`, otro lane) debe:

1. Sustituir ese placeholder por la clave pública minisign real ANTES de
   `tauri build` (un `sed`/paso de template — la clave privada
   `TAURI_SIGNING_PRIVATE_KEY` es el secreto que ya existe en `agents-autonomy`,
   reutilizado, no uno nuevo).
2. Publicar `latest.json` (`includeUpdaterJson: true`) en
   `https://github.com/devwspito/safent-runtime/releases/latest/download/latest.json`
   (ya es el endpoint fijado en `plugins.updater.endpoints`).
3. Publicar `runtime-manifest.json` **firmado con la MISMA clave** junto a
   `latest.json` — `desktop/src-tauri/src/update/tauri_updater.rs`
   (`RuntimeManifestVerifier`) lo verifica con el mismo pubkey embebido.

`update/` (T014) ya trae: `plan_update` puro (regla "botón sólo si hay
novedad", probada con los tres casos — sólo app, sólo motor/compañero, los
tres a la vez, y el caso "versión legible cambia pero el digest no" que NO debe
generar plan); `run_update` (orquestador de los 11 pasos de
`contracts/update.md` §4, reversión automática entre `apply_engine` y
`verify_ready`, probada con dobles de test — feliz, cada punto de fallo con
reversión, y el caso "la propia reversión también falla" con un resultado
tipado en vez de silencioso); verificación minisign de `runtime-manifest.json`
con una firma real generada con `minisign -G`/`-S` (no un fixture inventado).
24 tests, `cargo test` limpio. Falta cablear `run_update`/`UpdatePorts` al CLI
embebido real — eso es integración de T011, no de esta entrega.

## Grapar también el `.app`, no sólo el DMG (MAC2-12, verificacion-mac-2.md)

**Decisión: SÍ, grapar el `.app` además del DMG.** `xcrun stapler validate
Safent.app` fallaba (rc=65, «does not have a ticket stapled to it») en el
`.app` extraído de un DMG **ya grapado y con `spctl` en verde** — grapar el
contenedor (DMG) no grapa lo que hay dentro; son dos operaciones
independientes, cada una soportada oficialmente por
`xcrun stapler staple <ruta>` sea un `.dmg` o un `.app`.

**Por qué importa pese a que el DMG en sí ya funciona sin red**: en la propia
prueba, `spctl -a -vv -t exec Safent.app` aceptó el `.app` sin red porque el
vale ya estaba en la **caché local de Gatekeeper** tras montar/validar el DMG
grapado un momento antes, en la MISMA máquina — no porque el `.app` llevara
vale propio. Ese atajo de caché no existe en la vía del actualizador: este
mismo artefacto de CI publica `macos/Safent.app.tar.gz` (para
`update/tauri_updater.rs`), que el actualizador descarga y extrae
**directamente, sin DMG de por medio en ningún momento** — no hay paso previo
que precaliente la caché de Gatekeeper. Un `.app` sin vale propio, extraído
así en un Mac sin red en ese instante, no tiene ninguna vía offline para
validarse.

**Pendiente para T023** (`agents-autonomy/safent-desktop.yml`, otro repo,
`devops-engineer`): tras `xcrun notarytool wait` (el mismo paso que ya graba
el DMG), añadir `xcrun stapler staple Safent.app` **antes** de comprimirlo a
`Safent.app.tar.gz` — la MISMA firma/notarización cubre ambos contenedores
(DMG y `.app` suelto), así que es grapar dos veces el mismo vale, no pedir
una notarización nueva.

### MAC4-06 (verificacion-mac-4.md): el orden exacto que falta — grapar el
### `.app` es ANTES de construir el DMG, no después

Verificación 4 confirma que la sección de arriba **todavía no se aplicó**
(`stapler validate Safent.app` → rc=65 dentro del propio DMG ya grapado y
notarizado). La pregunta nueva del encargo — «¿grapar el `.app` antes de
meterlo en el DMG?» — la respuesta es **sí, y es la ÚNICA posición que
funciona**, por una razón mecánica, no de estilo:

`xcrun stapler staple` **escribe** el vale dentro del propio paquete (un
recurso bajo `Contents/` del `.app`, o el equivalente en el `.dmg`) — necesita
un destino en el que se pueda escribir. Un `.app` ya copiado dentro de la
imagen de disco final (normalmente UDZO/UDBZ, comprimida y de solo lectura
para distribución) ya no es escribible in situ; grapar tendría que desmontar,
reconstruir la imagen o similar. El propio fichero `.dmg`, en cambio, SIEMPRE
es escribible en el disco del runner (aunque su contenido interno sea de solo
lectura una vez montado), así que grapar el `.dmg` en sí nunca tiene este
problema — es exactamente por eso que ese paso YA funciona hoy y el del `.app`
no.

**El orden que funciona, de principio a fin** (un solo `.app` firmado,
reutilizado para AMBOS artefactos publicados):

1. Construir y firmar `Safent.app` (hardened runtime, entitlements) en una
   carpeta de trabajo normal, escribible.
2. Empaquetar ESE `.app` para el envío a notarización (`ditto -c -k` a un
   `.zip`, o el propio DMG en modo borrador — Apple acepta ambos formatos de
   envío; el formato de envío es independiente del artefacto final).
3. `xcrun notarytool submit --wait` sobre ese envío.
4. En cuanto notarytool confirma `Accepted`: `xcrun stapler staple
   <workdir>/Safent.app` — el `.app` TODAVÍA está suelto en la carpeta de
   trabajo, escribible, nunca dentro de ninguna imagen de disco todavía.
5. A partir de AQUÍ, el `.app` ya lleva vale propio. Los dos artefactos
   publicados se construyen los DOS a partir de esta MISMA copia, ya grapada:
   - `hdiutil create` (o `create-dmg`) el DMG final, copiando el `.app` YA
     GRAPADO a la imagen.
   - `tar -czf Safent.app.tar.gz Safent.app` para el actualizador — el mismo
     paso que ya pedía la sección de arriba, ahora automático porque parte
     del mismo `.app` ya grapado, sin necesitar su propia llamada aparte.
6. `xcrun stapler staple Safent.dmg` sobre el `.dmg` YA CONSTRUIDO — una
   segunda llamada independiente, sobre un fichero plano en disco (siempre
   escribible), nunca sobre el `.app` que lleva dentro.

Grapar el `.app` DESPUÉS de construir el DMG (el orden actual, a juzgar por
el síntoma) sólo grapa el contenedor exterior; el `.app` que un dueño extrae
—o que el actualizador descarga vía `Safent.app.tar.gz`— sigue sin vale
propio. Invertir el orden (pasos 4 y 5 arriba) resuelve los dos artefactos a
la vez con una única llamada nueva a `stapler staple`.

`runtime-manifest.lock` → `excluded_bundle_formats.appimage` tiene el
razonamiento completo. En corto: linuxdeploy/patchelf reescriben el RUNPATH
de 6 de los 15 binarios del runtime al empaquetar el AppImage — parte normal
de cómo AppImage se hace reubicable, no manipulación — así que su sha256 ya
no casa con `targets[].entries` (el valor pre-empaquetado) y
`cmd_stage_runtime` los rechaza correctamente con `runtime_hash_mismatch` en
cuanto esa verificación corre de verdad. El `.deb` verifica 15/15 exacto
(medido en vivo) y el `.rpm` comparte el mismo modelo no reubicable, así que
Linux ya queda cubierto sin el AppImage. **Pendiente para T023**
(`agents-autonomy/safent-desktop.yml`, otro repo): dejar de publicar el
AppImage en `latest.json`/el Release; sólo `.deb` + `.rpm`.
