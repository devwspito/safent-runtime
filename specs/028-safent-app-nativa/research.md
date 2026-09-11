# Research — Phase 0 (specs 028 + 029)

Cada bloque resuelve un `NEEDS CLARIFICATION` de `028/spec.md` o `029/spec.md`, o
fija una decisión de arquitectura que el diseño necesita. Las cifras están
**medidas** el 10-sep-2026 contra los registros reales, no estimadas.

## Mediciones de partida (fuente de todas las decisiones de tamaño)

| Artefacto | Medida | Fuente |
|---|---|---|
| Límite por fichero en GitHub Releases | **2 GiB** | docs.github.com «Each file included in a release must be under 2 GiB» |
| `ghcr.io/devwspito/safent:latest` arm64 | **2,50 GB comprimido**, 66 capas, mayor capa 791 MB | manifest OCI de ghcr.io |
| `ghcr.io/devwspito/safent:latest` amd64 | **2,51 GB comprimido**, 66 capas, mayor capa 780 MB | ídem |
| `ghcr.io/devwspito/safent-ads:latest` | **0,44 GB comprimido**, 11 capas | ídem |
| `quay.io/podman/machine-os:6.1` applehv aarch64 | **932 MB** (disco zstd) | manifest OCI de quay.io |
| `machine-os:6.1` qemu/hyperv x86_64 | 1.110 MB | ídem |
| `machine-os:6.1` wsl x86_64 / aarch64 | 250 MB / 235 MB | ídem (para Windows, más adelante) |
| `podman-installer-macos-arm64.pkg` v6.1.1 | 76,3 MB | releases de containers/podman |
| `podman-linux-{amd64,arm64}.tar.gz` (podman-static v6.1.1) | 34,6 MB / 31,9 MB | releases de mgoltzsche/podman-static |

## Decisión: qué se empaqueta dentro de la app y qué se descarga

- **Elegido**: la app **empaqueta** el binario de podman fijado (v6.1.1) con sus
  auxiliares y, en macOS, **la imagen de máquina** `machine-os:6.1` applehv
  aarch64. **No empaqueta** ni la imagen del runtime de Safent ni la del
  compañero: ésas se traen por **digest** en el primer arranque.
- **Por qué**: la imagen del runtime mide 2,50 GB comprimida — **por sí sola
  supera el límite de 2 GiB por fichero** de GitHub Releases, así que no cabe
  discusión. La imagen de máquina (932 MB) sí cabe y es la pieza que hoy rompe
  la promesa: sin ella el dueño tiene que instalar podman a mano. Presupuesto
  del DMG macOS arm64: envoltorio Tauri ~12 MB + podman/gvproxy/vfkit/krunkit
  ~80 MB + imagen de máquina 932 MB ≈ **1,02–1,10 GB**, con ~45 % de margen
  bajo el límite. Linux no lleva máquina: .deb/.AppImage ≈ **45–110 MB**.
- **Descartadas**:
  - *Empaquetar también las imágenes de contenedor* — imposible (2,50 GB > 2 GiB)
    y, aunque cupiese, congelaría cada versión del runtime dentro de cada
    instalador y multiplicaría por seis el peso de cada release.
  - *Depender del podman del usuario* (lo de hoy) — es exactamente el paso de
    usuario que puede salir mal: versión distinta, PATH distinto, máquina
    rootless, brew a medias. Rompe el principio rector.
  - *Instalar el .pkg oficial de podman con un `osascript … with administrator
    privileges`* (lo que hace hoy `desktop/src-tauri/src/main.rs`) — pide
    contraseña de administrador, deja una instalación global que no controlamos
    y que otra herramienta puede actualizar por debajo, y depende de la API de
    GitHub en el momento del arranque.
  - *Descarga troceada de la imagen de máquina en el primer arranque* — no hace
    falta: cabe empaquetada, y empaquetada es determinista y funciona sin red
    para la fase más frágil.
- **Riesgos**: DMG de ~1 GB (descarga lenta la primera vez → NFR-001 lo cubre
  con progreso honesto) · la imagen de máquina envejece con cada release del
  instalador (mitigación: el instalador la actualiza; una máquina ya creada no
  se recrea salvo que el reconciliador lo pida) · Windows con Hyper-V pediría
  1.110 MB y con WSL 250 MB — decidir al abrir Windows, no ahora.
- **Links**: 028 FR-004/FR-006/FR-008, NFR-007, Assumption 4; 029 A-4.

## Decisión: cómo se traen las imágenes que no van empaquetadas

- **Elegido**: `pull` **fijado por digest** (`ghcr.io/devwspito/safent@sha256:…`),
  con reintentos con espera creciente y **reanudación por capa**; el digest lo
  publica un manifiesto firmado (ver contrato `update.md`).
- **Por qué**: el digest hace la descarga verificable y reproducible (una etiqueta
  `:latest` puede moverse bajo los pies durante la propia instalación). `podman
  pull` conserva en el almacén las capas ya completadas, así que reintentar tras
  un corte **no repite lo ya bajado**: la reanudación es real a nivel de capa.
- **Descartadas**: *reanudación byte a byte* — `containers/image` no la ofrece;
  con 66 capas y la mayor de 791 MB, el peor caso de un corte es repetir esa capa,
  no los 2,5 GB · *`:latest`* — no verificable y no reproducible.
- **Riesgos**: una capa de 791 MB reiniciada en una red mala puede no converger
  nunca. Mitigación: el reconciliador cuenta intentos sin progreso y, tras dos
  fallos idénticos, para y muestra **una** pantalla honesta con «Reintentar» —
  nunca un bucle infinito ni una instrucción de terminal.
- **Links**: 028 FR-006/FR-017/FR-020, SC-005; 029 FR-004/FR-006.

## Decisión: el motor de instalación es el CLI `safent` embebido

- **Elegido**: la app **embebe** `safent` y `ops/container/run-safent.sh` y los
  ejecuta con `SAFENT_PODMAN=<ruta del podman empaquetado>`. Una sola
  implementación de la jaula, del aprovisionado y del compañero.
- **Por qué**: la jaula (caps + seccomp + `unmask=/sys/kernel/security` +
  `--systemd=always` + los binds del compañero) es una superficie de seguridad
  que ya está verificada en vivo (matriz 025, filas INST-01…INST-06). Duplicarla
  en Rust crearía dos verdades que divergirían en la primera corrección.
- **Descartadas**: *reimplementar el arranque en Rust* — duplicación de una
  superficie de seguridad · *seguir descargando el CLI de `raw.githubusercontent`
  en el primer arranque* (lo de hoy) — depende de la red para la pieza que
  gobierna la instalación y no está firmada.
- **Riesgos**: el CLI es POSIX sh y hoy escupe texto humano; la app lo raspa
  línea a línea (`run_and_stream`). Mitigación: el CLI gana un modo
  `--porcelain` que emite **NDJSON** de etapas (contrato `app-engine.md`); el
  texto humano se conserva intacto para el operador de terminal.
- **Links**: 028 FR-006/FR-025/FR-028; 029 A-1/FR-003.

## Decisión: Linux sin VM — rootless por defecto, ayudante privilegiado acotado

- **Elegido**: en Linux, podman estático empaquetado ejecutándose **rootless**
  como camino por defecto. El **ayudante privilegiado se instala una sola vez**
  (postinst del .deb, o un `pkexec` declarado para el AppImage) y su trabajo es
  **sólo** lo que exige el kernel: (a) perfil AppArmor con permiso `userns` para
  la ruta del podman empaquetado, (b) capacidades de fichero en
  `newuidmap`/`newgidmap`, (c) rangos en `/etc/subuid`/`/etc/subgid` si faltan.
  Queda además preparado para ejecutar el motor en ámbito de sistema si el
  kernel del equipo no permite espacios de nombres sin privilegio.
- **Por qué**: la fila **DIST-06** de la matriz 025 es evidencia en vivo de que
  el cage entero funciona en **podman rootless** — Landlock `enforcing=True`,
  seccomp de PID1, netns del navegador y del MCP, nftables, compañero y CLI —
  con una única diferencia observada: desde el host no se alcanza
  `10.201.0.10:8443` (afecta a la sonda de `safent companion status`, no al
  producto). Pedir root para el motor donde no hace falta es superficie
  regalada. Pero Ubuntu 24.04+/Debian 13 restringen los espacios de nombres sin
  privilegio salvo perfil AppArmor, y un podman **empaquetado en `$HOME`** no
  tiene ese perfil: ahí el ayudante es imprescindible y no es opcional.
- **Descartadas**: *rootful siempre* — root permanente para el motor sin
  evidencia de necesitarlo · *sin ayudante* — en Ubuntu 24.04+ el primer arranque
  fallaría con un error de kernel que el dueño no puede resolver, es decir,
  exactamente el fallo prohibido · *pedir al dueño que instale `uidmap`* — es un
  paso de usuario, prohibido.
- **Riesgos**: el AppImage no tiene postinst; su primer arranque en un equipo
  restringido necesita un `pkexec` declarado (autorización del sistema, no un
  comando). Si el dueño la deniega, pantalla honesta con «Reintentar», nunca una
  instrucción de terminal. **Escalado a `tech-lead`**: esto acota la instrucción
  «el cage necesita rootful» a lo que el kernel realmente exige; el ayudante se
  mantiene como pieza y como puerta al ámbito de sistema.
- **Links**: 028 FR-014, matriz 025 DIST-06; gobernanza en `plan.md`.

### Verificación en vivo — esta DGX, worktree `desk2`, 10-sep-2026 (T010)

Matriz 025 DIST-06 es evidencia de **otra** máquina. Esta sección la repite en
esta DGX (Ubuntu 24.04.4 LTS, aarch64, kernel 6.17, `kernel.
apparmor_restrict_unprivileged_userns=1` confirmado activo) con el **binario
pinneado real** (podman-static v6.1.1 aarch64, el mismo de
`runtime-manifest.lock`), y **afina** el «por qué» del apartado anterior con un
hallazgo que cambia la condición exacta del ayudante.

- **Cage completo, rootless, con el podman del sistema** (`/usr/bin/podman`
  4.9.3, perfilado por `/etc/apparmor.d/podman`): contenedor `desk2-cage-test`
  levantado con las flags exactas de `run-safent.sh` (`--systemd=always`,
  `--cap-add NET_ADMIN,SYS_ADMIN,AUDIT_READ`, `--security-opt
  seccomp=<perfil>`, `unmask=/sys/kernel/security`, `label=disable`,
  `apparmor=unconfined` porque AppArmor está `Y`), imagen real
  `ghcr.io/devwspito/safent:0.8.42` (la del `VERSION` del repo). Resultado —
  **PASA los cuatro invariantes**: `systemd` PID1 (`is-system-running:
  running`, cero unidades fallidas), Landlock (`landlock_loader BROWSER OK —
  confinamiento FS activo` en el journal, LSM list del kernel incluye
  `landlock`), netns (`/run/netns/{hermes-browser,hermes-mcp}` +
  `veth-hbr-host`/`veth-hmcp-host` reales), nftables (las tres tablas
  `hermes_host`/`hermes_browser_egress`/`hermes_mcp_egress` con las reglas
  anti-pivot cargadas). Contenedor destruido tras la prueba.
- **El mismo cage con un binario podman REUBICADO Y SIN PERFIL** (copiado a
  `/tmp`, confirmado `unconfined` vía `/proc/self/attr/current`) — el sustituto
  más fiel posible, desde este host, de lo que será
  `~/.safent/runtime/<versión>/podman`: **también PASA los cuatro invariantes**,
  idéntico resultado (`desk2-cage-test2`, misma imagen, mismo journal de
  Landlock, mismas tres tablas nftables, cero unidades fallidas). Contenedor y
  volumen destruidos tras la prueba.
- **Hallazgo que afina el «por qué»**: la restricción de Ubuntu 24.04
  (`apparmor_restrict_unprivileged_userns=1`) sólo bloquea la creación de
  espacio de nombres de usuario a procesos **confinados por un perfil que no
  la declara** — comprobado con `unshare --user --map-root-user id` (el
  binario `/usr/bin/unshare` real, sin perfil dedicado) → **bloqueado**
  (`Operación no permitida` en `/proc/self/uid_map`). Un binario **realmente
  sin perfil** (el podman copiado a `/tmp`, `unconfined` confirmado) →
  **permitido** (`podman unshare id` da `uid=0`), y sostiene el cage entero
  como demuestra la prueba anterior. Es decir: en esta máquina, un podman
  empaquetado en `$HOME`/`~/.safent` **no necesita el ayudante** — la
  condición real que sí lo exige es `kernel.unprivileged_userns_clone=0` (el
  interruptor Debian/Ubuntu clásico, **distinto** del de AppArmor; en esta DGX
  vale `1`), no «Ubuntu 24.04+» como tal.
- **Decisión (confirmada, condición afinada)**: rootless por defecto se
  mantiene — es el resultado medido, dos veces, con el binario pinneado real.
  El ayudante privilegiado **no se ejecuta incondicionalmente**: el
  reconciliador lo invoca sólo cuando `HostFacts.userNsAllowed` observa el
  bloqueo real (sonda barata en `preflight`/`engine_provisioning`: `<podman
  empaquetado> unshare true`; éxito ⇒ `userNsAllowed=true` ⇒ sin ayudante;
  fallo con «Operación no permitida» en `uid_map` ⇒ `userNsAllowed=false` ⇒
  `InstallPrivilegedHelper`). Esto ya es exactamente lo que `data-model.md`
  modela (`HostFacts.userNsAllowed: bool`, `RepairAction::
  InstallPrivilegedHelper` condicional) — esta verificación confirma que el
  diseño existente es el correcto y fija la sonda concreta que `T011`
  (`bootstrap_service.rs`, otro lane) debe usar para poblar ese campo.
- **No hizo falta rootful**: al pasar rootless los cuatro invariantes con el
  binario real (dos veces), la rama «si rootless no basta, rootful con
  ayudante acotado» de este ticket no se activa. No se probó rootful en esta
  DGX por no ser necesario, no por no poder.
- **Evidencia**: comandos, salidas y limpieza en el registro de la sesión T010
  (worktree `lumen-runtime-desk2`, contenedores `desk2-cage-test`/
  `desk2-cage-test2`, ambos destruidos; ningún contenedor ajeno tocado).

## Decisión: reconciliador auto-sanador como componente de primer nivel

- **Elegido**: un **planificador puro** `reconcile(HostFacts, DesiredState) →
  [RepairAction]` en el dominio, y un bucle de aplicación que observa, planifica,
  aplica una acción idempotente, vuelve a observar y repite hasta converger o
  hasta detectar **ausencia de progreso** (misma acción, mismo código de error,
  dos veces) → `degraded` con una pantalla y un «Reintentar».
- **Por qué**: es la traducción literal del principio del dueño: no hay estado del
  equipo que exija una decisión del usuario. Máquina preexistente rootless o de
  otro tamaño → se adopta o se ignora; puerto ocupado → se elige otro y no se
  enseña; contenedor o compañero a medio aprovisionar → se converge; descarga
  interrumpida → se reanuda; imagen vieja → se re-baja verificada; segunda
  instancia → se enfoca la ventana existente. Como el planificador es **puro**,
  todos esos casos son tests unitarios sin contenedores ni red, que es justo lo
  que exige el Principio V de la constitución.
- **Descartadas**: *condicionales dispersos en el arranque* (lo de hoy en
  `main.rs`) — cada caso nuevo añade una rama y ninguna se prueba · *preguntar al
  dueño ante el conflicto* — prohibido por la decisión vinculante.
- **Riesgos**: un reconciliador demasiado listo puede destruir estado ajeno.
  Mitigación: ninguna acción borra datos del dueño ni toca máquinas de podman que
  no llevan nuestra marca; adoptar una máquina ajena es **sólo lectura + uso**,
  nunca `machine set` ni `machine rm`. Si la máquina ajena no sirve (rootless en
  macOS), se crea **la nuestra** con nombre propio y se deja la ajena intacta.
- **Links**: 028 FR-007/FR-009/FR-010/FR-028, SC-005; 029 FR-005/FR-006.

## Decisión: FR-030 — cerrar la ventana no detiene el motor

- **Elegido**: cerrar la ventana **deja el motor vivo**, con un elemento en la
  barra de menús (macOS) / bandeja (Linux) que declara el estado y ofrece
  «Abrir», «Reiniciar el motor» y **«Salir de Safent»** explícito. Salir sí
  detiene el motor de forma ordenada (`SIGRTMIN+3`, 30 s).
- **Por qué**: el motor ejecuta trabajo del agente por iniciativa propia (cola del
  daemon, Principio 0 de la constitución); cerrar una ventana no puede matar
  trabajo en curso. Es además el comportamiento de las apps que el dueño cita
  como referencia.
- **Descartadas**: *cerrar = parar* — mata trabajo en curso sin decirlo ·
  *cerrar = parar tras un tiempo* — comportamiento no declarable.
- **Links**: 028 FR-030, pregunta abierta 2.

## Decisión: FR-023 — actualizar con trabajo del motor en curso

- **Elegido**: la app **declara** el trabajo en curso, **pausa la cola** del
  daemon (no acepta nuevos ítems), **espera acotada** a que el ítem vivo termine
  (≤ 10 min con progreso) y entonces actualiza. Si excede la espera, avisa y
  continúa: el ítem se **re-encola** para ejecutarse tras el reinicio. Sin
  pregunta al dueño.
- **Por qué**: la decisión vinculante prohíbe pasos de usuario que puedan salir
  mal, y elegir entre «esperar» y «cortar» es exactamente uno. Re-encolar es
  posible porque el trigger del agente ya es su propia cola.
- **Descartadas**: *advertir y cortar bajo confirmación* (una de las dos opciones
  de la spec) — introduce una decisión del usuario · *esperar indefinidamente* —
  una tarea colgada dejaría el producto sin actualizar para siempre.
- **Riesgos**: re-encolar exige que el ítem sea idempotente. Mitigación: sólo se
  re-encolan ítems que el daemon marca como reanudables; el resto se declaran
  como interrumpidos en el diagnóstico.
- **Links**: 028 FR-023, pregunta abierta 5.

## Decisión: canal de distribución y firma

- **Elegido**: **descarga directa firmada** desde GitHub Releases más el
  actualizador de Tauri con manifiestos firmados. **Nada de tiendas.**
  **La notarización pasa de "mejor esfuerzo" a requisito de producción.**
- **Por qué**: la App Store sandboxea la app y prohíbe que instale y gobierne un
  motor local y una máquina virtual; la propia promesa del producto es
  incompatible con esas reglas. Y sobre la notarización: Gatekeeper es el único
  aviso del sistema que puede aparecer y que el dueño podría «hacer mal» — con la
  app notarizada ese aviso **desaparece**, así que notarizar es la condición
  técnica del principio rector, no un adorno.
- **Descartadas**: *App Store / Microsoft Store* — incompatibles con instalar un
  motor local y autoactualizarse · *sólo codesign sin notarizar* (lo de hoy en
  `safent-desktop.yml`) — deja el aviso de origen desconocido, que es el fallo
  de usuario prohibido.
- **Estado (10-sep-2026)**: el acuerdo del Programa de Desarrolladores de Apple
  **ya está aceptado** y la notarización funciona. El comentario de
  `safent-desktop.yml` («403 hasta que el titular acepte, no fallamos el build»)
  queda **obsoleto**: el paso de notarizar + grapar pasa a ser **gate duro** del
  build de producción — si no notariza, no hay release.
- **Riesgos**: un fallo transitorio del servicio de notarización de Apple ahora
  rompe el build en vez de degradarlo. Es lo correcto: un DMG sin grapar produce
  el aviso de Gatekeeper, que es el fallo de usuario prohibido. Mitigación:
  reintento del paso, nunca un `continue-on-error`.
- **Links**: 028 FR-005, SC-011, pregunta abierta 4; `safent-desktop.yml` §macOS.

## Decisión: claves del actualizador

- **Elegido**: actualizador de Tauri v2 con manifiestos firmados (minisign). El
  pipeline necesita el secreto **`TAURI_SIGNING_PRIVATE_KEY`** (y su contraseña,
  vacía) — el mismo nombre que ya usa `tauri-release.yml` en `agents-autonomy` —
  y la **clave pública** va literal en `desktop/src-tauri/tauri.conf.json`
  (`plugins.updater.pubkey`). `safent-desktop.yml` hoy **no** pasa ese secreto ni
  publica `latest.json`: hay que añadir ambas cosas.
- **Por qué**: es la cadena que el grupo ya opera; reutilizarla evita construir una
  segunda infraestructura de firma (fuera de alcance por la propia spec).
- **Descartadas**: *par de claves nuevo por producto* — segunda cadena que
  mantener · *actualizador propio* — reimplementar verificación de firma.
- **Riesgos**: si se pierde la clave privada, ninguna instalación puede recibir
  actualizaciones firmadas. Mitigación: custodia como el resto de secretos del
  pipeline; **no** se genera ni se guarda una copia en ningún repo.
- **Única entrada pendiente del dueño**: confirmar si `safent-desktop.yml` reutiliza
  el `TAURI_SIGNING_PRIVATE_KEY` que ya existe en `agents-autonomy` o si Safent
  estrena par propio, y fijar la URL pública desde la que las instalaciones leerán
  `latest.json`. Es la última decisión externa que queda: firma de código y
  notarización ya están resueltas.
- **Links**: 028 FR-005/FR-017; `agents-autonomy/.github/workflows/tauri-release.yml` L142-164.

## Decisión: Windows

- **Elegido**: **fuera de esta entrega**. La US4 de 028 se difiere y el sitio de
  descarga declara Windows como «aún no servido».
- **Por qué**: la decisión vinculante es «Mac + Linux ahora, Windows después», y
  el envoltorio de máquina en Windows es una decisión distinta (WSL 250 MB frente
  a Hyper-V 1.110 MB) que arrastra requisitos de sistema propios.
- **Descartadas**: *entregar Windows sin resolver el envoltorio* — garantiza el
  fallo de usuario prohibido.
- **Links**: 028 US4, pregunta abierta 3.

## Decisión: 029 CL-001 — credenciales del vendor en la UI

- **Elegido**: **sí**, la UI recoge las credenciales de la app desarrolladora del
  vendor. Y **no se construye nada nuevo**: safent-ads **ya lo tiene**
  (`accounts/presentation/platform_apps_router.py` → `GET /platform-apps`,
  `PUT /platform-apps/{platform}`, `DELETE /platform-apps/{platform}`, con
  reautenticación, cifrado en el bróker y estado enmascarado de vuelta; panel en
  `panel/src/routes/ConexionesPage.tsx` + `components/connections/
  ConnectProviderCard.tsx`). El onboarding de 029 **encadena** ese panel, no lo
  duplica.
- **Por qué**: reutilizar antes de escribir. El camino de `vendor.env` que hoy
  documenta `provision.sh` queda relegado a operadores; la UI es el camino normal.
- **Corrección del 11-sep-2026**: la conclusión anterior sobre un token faltante era
  incorrecta. Google retiró ese mecanismo el 9-sep-2026. Las credenciales son
  `client_id`, `client_secret` y `login_customer_id` solo cuando se opera mediante
  una cuenta gestora. El acceso se gestiona en el proyecto Google Cloud del cliente
  OAuth; no añadir un token opcional ni obligatorio.
- **Descartadas**: *limitarse a conectar cuentas* — dejaría al dueño editando
  `vendor.env` a mano en el host, prohibido por A-3 · *recoger las credenciales en
  la UI de Safent y escribirlas al host por el agente* — ampliaría la superficie
  de escritura de secretos en el host justo donde el propio riesgo de 029 avisa.
- **Riesgos**: no confundir OAuth completado con acceso autorizado a cuentas reales.
  `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` debe tener un mensaje accionable.
- **Links**: 029 CL-001/FR-010/NFR-003;
  [migración oficial de Google](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).

## Decisión: 029 CL-002 — «Instalar» sin reiniciar Safent

- **Elegido**: el andamiaje del compañero (**red fija + estado + los cuatro binds
  de sólo lectura**) se aprovisiona **siempre**, incluso cuando el compañero no
  está instalado. Así «Instalar» es: bajar la imagen + levantar los servicios +
  un verbo del daemon que **relee** la presencia — **sin recrear Safent**. Sólo
  cae al camino de recreación una instalación heredada creada por un lanzador
  antiguo que no tenga los binds; la app lo declara como interrupción breve.
- **Por qué**: un bind no se añade a un contenedor vivo — ése era el riesgo mayor
  de 029. Si los binds están siempre, el riesgo desaparece en el caso normal.
  `companions.py` ya es tolerante: un fichero ausente o inválido degrada a «sin
  compañero», que es exactamente lo que debe ver el daemon con el andamiaje vacío.
- **Descartadas**: *recrear siempre* — interrupción evitable en el caso normal ·
  *no reiniciar nunca* — dejaría instalaciones heredadas sin poder llegar a
  `ready`, es decir, un «Instalar» que miente.
- **Riesgos**: unir Safent a la red `safent-companions` siempre cambia su red por
  defecto en equipos donde hoy no la lleva. Ya es el comportamiento cuando el
  compañero está aprovisionado (verificado en INST-01…INST-06), pero se cubre con
  un test de arranque con andamiaje vacío.
- **Links**: 029 CL-002, «Risk (el mayor)», FR-009.

## Decisión: 029 CL-003 — «Quitar» conserva los datos

- **Elegido**: «Quitar» **conserva** estado y credenciales (equivale a
  `safent companion remove`). Purgar exige una segunda confirmación explícita que
  nombra lo que se borra.
- **Por qué**: es la propuesta por defecto de la propia spec y la única reversible.
- **Hueco detectado**: la matriz 025 (CLI-12) documenta que `remove --purge` deja
  atrás los tres volúmenes del compañero — **incluida la base de datos con las
  campañas** — y la red. Purgar de verdad tiene que llevárselos; conservar tiene
  que conservarlos. Hoy ninguna de las dos cosas es cierta.
- **Links**: 029 CL-003/FR-012, matriz 025 CLI-11/CLI-12.

## Riesgos heredados que este plan tiene que atravesar (matriz 025)

- **ADS-02 (bloqueante)**: `/etc/hermes/companions/ads-sso.key` llega
  `-r-------- root root` y el daemon corre como `hermes` (uid 880) → el puente
  `/ads/` responde 503 y `ready` es hoy inalcanzable. El bearer sí tiene su
  escenificación root (`hermes-companion-bearer` → `/run/hermes/companions/`);
  la clave SSO **no**. Sin esto, 029 no puede declarar `ready` honestamente.
- **CLI-08**: `_companion_container_counts` llama a `compose ps` sin
  `_companion_env` → siempre «0/0 running». El estado que la UI va a mostrar no
  puede apoyarse en un contador que miente.
- **CLI-10 (grave)**: `companion rotate` mezcla `:latest` con `safent-ads:local`
  y rompe las migraciones. Cualquier verbo del compañero disparado desde la UI
  tiene que **fijar la imagen por digest**, no re-derivarla.
- **UPD-06**: `safent uninstall` no está acotado a la instancia (borra el agente
  de usuario y los CLI de otras instalaciones). Desinstalar desde la app tiene que
  acotarse a lo que esa app instaló.
