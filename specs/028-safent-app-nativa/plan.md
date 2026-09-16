# Implementation Plan — 028 «Safent es una app nativa» + 029 «Ads se instala con un botón»

**Ramas**: `feat/safent-next` · **Specs**: `028` (1eefa27), `029` (79ab85d)
**Artefactos**: `research.md` · `data-model.md` · `contracts/{app-engine,install-request,update}.md` · `quickstart.md` · `tasks.md`

## Principio rector (decisión vinculante del dueño)

> «Que el sistema funcione "bien" pero el usuario hizo algo mal es lo mismo que
> "la app no sirve". Por eso debe funcionar como Codex app o Claude Code app.»

Se traduce en cuatro reglas que **gobiernan cada decisión de este plan** y que
`code-reviewer` verifica pieza a pieza:

1. **No existe paso del usuario que pueda salir mal.** Cero comandos, cero
   elecciones sobre runtimes/puertos/máquinas/versiones, cero pegar nada. Los
   únicos avisos admitidos son los **obligatorios del sistema operativo**:
   Gatekeeper en macOS —que la notarización **elimina**, y por eso notarizar es
   requisito de producción, no adorno— y el prompt de privilegio del propio gestor
   de paquetes en Linux.
2. **Todo estado se auto-sana sin preguntar.** Podman o máquina preexistentes
   (rootless, de otro tamaño, de otra versión) → se adoptan o se ignoran; puerto
   ocupado → se elige otro y la app sigue; contenedor o compañero a medias → se
   reconcilia; descarga cortada → se reanuda; imagen envejecida → se re-baja
   verificada; segunda instancia → se enfoca la ventana existente.
3. **Lo que la app no pueda reparar muestra UNA pantalla honesta** con causa en
   lenguaje del dueño y **un** «Reintentar». **Jamás** una instrucción de terminal.
4. **Criterio de éxito**: una persona que nunca ha oído la palabra «contenedor»
   instala y usa Safent y Anuncios **sin leer nada**.

## Technical Context

Envoltorio: Tauri v2 (Rust) en `desktop/`, hoy 510 líneas que raspan texto del CLI
y ofrecen un botón «Instalar Podman» que sólo existe en macOS. Motor: contenedor
Playwright con systemd PID1, arrancado por `safent` / `ops/container/run-safent.sh`.
Compañero: `safent-ads` en red fija `10.201.0.10:8443`, puente same-origin `/ads/`.
Pipeline de firma: `agents-autonomy/.github/workflows/safent-desktop.yml`
(macos-14, ubuntu-22.04, ubuntu-22.04-arm, windows-latest; Developer ID + SignPath).
**Notarización de Apple: ya operativa** (acuerdo aceptado 10-sep-2026) → deja de
ser «mejor esfuerzo» y pasa a **gate duro** del build.

## Constitution Check — GATE PRE-DISEÑO

| Principio | Veredicto | Nota |
|---|---|---|
| **0 SUPREMO — somos un SO** | ⚠️ **PASS con condición** | Se añaden endpoints HTTP (`/system/requests`). Son **marca + supervisión**, no gobernanza ni razonamiento: escriben un enum en un fichero que cumple un proceso del host, exactamente el mecanismo que la constitución ya tolera para `/system/update`. Ninguna decisión del agente pasa por ahí. Condición: **cero lógica** en el shell-server más allá de validar el enum y escribir/leer la marca. |
| **I — contratos públicos inmutables** | PASS | No se toca `BrowserPort` ni la firma de `BrowserSession`. |
| **II — HITL para EXTERNA_IRREVERSIBLE** | PASS | Sin steps de navegador nuevos. |
| **III — tokenización PII** | PASS | No hay PII nueva hacia proveedores LLM. |
| **IV — fail-closed** | PASS | Manifiesto sin firma válida → sin botón. Digest que no casa → no se aplica. Marca con verbo desconocido → 400. |
| **V — tests base sin Chromium/red/contenedores** | PASS | El reconciliador es un **planificador puro**; sus casos son unitarios. El CLI se prueba contra un doble que emite NDJSON. |

**Veredicto: PASS** (una condición registrada, sin violación que llevar a Complexity Tracking).

## Module / layer design (input to plan.md)

### Bootstrap — envoltorio nativo (`desktop/src-tauri`)

- **Domain** (Rust puro, sin E/S): `EngineLifecycle` (máquina de estados de
  `data-model.md`), `HostFacts`, `DesiredState`, `RepairAction`, `VersionSet`,
  `UpdatePlan`, `FailureCause`. La pieza central es
  `reconcile(HostFacts, DesiredState) -> Vec<RepairAction>`: **función pura**,
  determinista, sin `Command` ni red. Aquí viven las reglas 2 y 3 del principio rector.
- **Application**: `BootstrapService` (observa → planifica → aplica una acción →
  vuelve a observar, hasta converger o detectar ausencia de progreso),
  `UpdateService` (orquesta `contracts/update.md` §4), `WindowPolicy` (instancia
  única, navegación permitida sólo al destino con vale, bandeja/barra de menús).
  Los **puertos** se declaran aquí: `EngineDriver`, `UpdateManifestSource`,
  `PrivilegeHelper`, `StateStore`, `ProgressSink`.
- **Infrastructure**: `EmbeddedCliDriver` (implementa `EngineDriver` sobre el CLI
  embebido, consumiendo NDJSON — `contracts/app-engine.md`), `TauriUpdaterSource`,
  `LaunchdHelper`/`PkexecHelper`, `JsonStateStore` (`~/.safent/app/state.json`).
- **Presentation**: la pantalla de preparación (etapas, progreso, cancelar,
  «Reintentar», diagnóstico), el elemento de bandeja y la ventana del producto.

### Compañeros — CLI anfitrión + daemon + frontend

- **Domain**: `CompanionInstallation`, `CompanionScaffold`, `InstallRequest`
  (vocabulario cerrado de verbos).
- **Application**: consumo de la marca (una implementación, dos lectores: `safent
  agent` y la app abierta, con reclamación mutuamente excluyente).
- **Infrastructure**: `safent companion install|repair|remove` con imagen **por
  digest**; andamiaje del compañero **siempre presente** (red + estado + los
  cuatro binds) para que instalar no exija recrear Safent.
- **Presentation**: un único componente `CompanionInstallAction` compartido por la
  tarjeta de Herramientas y por el estado `not_installed` de la barra lateral; el
  campo de URL heredado se repliega tras «avanzado».

### Onboarding de Ads (`safent-ads`, contexto ajeno)

Se **reutiliza** lo que ya existe: `platform_apps_router` (`GET/PUT/DELETE
/platform-apps`, con reautenticación y cifrado en el bróker) y el panel
`ConexionesPage` + `ConnectProviderCard`. Google utiliza el proyecto Cloud del
cliente OAuth para asignar acceso; no se añade ningún developer token (retirado
el 9-sep-2026). El onboarding debe explicar los bloqueos de acceso. El puente `/ads/` es la
capa anticorrupción: Safent no modela credenciales de plataformas.

## Cross-cutting concerns

| Preocupación | Dónde vive |
|---|---|
| **Autorización** | Bearer de la instalación en el borde HTTP del daemon. El envoltorio **no** expone IPC de instalación a la página remota: sólo el estado de lectura de `app-engine.md` §7. |
| **Vale de arranque** | Sólo en memoria del envoltorio; llega por descriptor dedicado, nunca por stdout/argv/entorno/fichero. |
| **Registro y trazas** | Eventos de dominio → sumidero de progreso. `detail` saneado en origen: el CLI enmascara el vale antes de emitir. |
| **Validación** | Enum cerrado en el daemon antes de escribir la marca; sha256 de cada binario empaquetado antes de ejecutarlo; digest de cada imagen antes de aplicarla; firma minisign de los dos manifiestos. |
| **Errores** | `FailureCode` cerrado y estable, traducido a una frase del dueño en presentación. Nunca se filtra jerga cruda. |
| **Transacciones** | Copia previa al punto de no retorno + reversión. «O la nueva funcionando, o la anterior funcionando.» |
| **Orquestación** | Explícita y en proceso, dentro del envoltorio. Sin cola, sin saga, sin motor de flujos: es un solo equipo, un solo dueño, pasos secuenciales reanudables. |

## Governance table — pasos privilegiados

| Paso | Quién lo ejecuta | Privilegio | Cuándo | Qué ve el dueño | Si se deniega |
|---|---|---|---|---|---|
| Instalar la app (.dmg / .deb / .AppImage) | El sistema | ninguno (dmg) · admin del gestor (.deb) | una vez | El gesto de siempre. **Con la app notarizada, sin aviso de Gatekeeper** | No hay app; nada roto |
| Desplegar el runtime empaquetado a `~/.safent/runtime/` | La app | ninguno | primer arranque y cada actualización | Etapa «Preparando la base de ejecución» | — |
| Crear/arrancar la máquina (macOS) | podman empaquetado | ninguno — el «rootful» es **dentro** de la VM | primer arranque | Etapa con tamaño y tiempo | — |
| Adoptar una máquina preexistente | La app | ninguno, **sólo lectura y uso** | si existe y sirve | Nada: es transparente | Se crea la nuestra; la ajena **no se toca** |
| Ayudante privilegiado en Linux (perfil AppArmor `userns` para el podman empaquetado · capacidades en `newuidmap`/`newgidmap` · rangos `subuid`/`subgid`) | postinst del .deb, o **un** `pkexec` declarado en AppImage | root, **una sola vez** | sólo si el kernel lo exige (Ubuntu 24.04+/Debian 13) | El prompt del propio sistema, precedido de qué es y para qué | Pantalla honesta + «Reintentar». Nunca un comando |
| Descargar imágenes por digest | podman empaquetado | ninguno | primer arranque y actualización | Etapas con bytes reales | Reintento con espera creciente |
| Sustituir el envoltorio | Actualizador de Tauri (firma verificada) | ninguno | al actualizar | «Se va a cerrar y volver a abrir» | Se queda en la versión anterior, viva |
| Desinstalar | La app → marca → CLI con `--scope this-install` | ninguno | explícito | Qué se borra y qué se conserva | — |

**Nunca** se pide: contraseña de administrador en macOS, instalar software de
terceros globalmente, tocar el PATH del dueño, ni una autorización sin declararla antes.

## Decisions & trade-offs

1. **Empaquetar podman + imagen de máquina, no las imágenes de contenedor.**
   Alternativas: empaquetar todo · depender del podman del usuario · instalar el
   .pkg oficial con contraseña de administrador. Elegido por una medida dura: el
   runtime pesa **2,50 GB comprimido** y el límite de GitHub Releases es **2 GiB
   por fichero** — no cabe. La imagen de máquina (932 MB) sí cabe y es justo la
   pieza que hoy obliga al dueño a hacer algo. **Se cede**: DMG de ~1,05 GB.
2. **El CLI embebido es el motor de instalación.** Alternativa: reimplementar la
   jaula en Rust. Elegido para no duplicar una superficie de seguridad ya
   verificada en vivo. **Se cede**: hay que darle al CLI un modo NDJSON.
3. **Linux rootless por defecto; ayudante acotado a lo que exige el kernel.**
   Alternativa: rootful siempre. La fila **DIST-06** de la matriz 025 demuestra el
   cage entero funcionando rootless. **Se cede**: la sonda de salud del compañero
   desde el host no funciona rootless (CLI-08) → el estado se lee **desde dentro**,
   por el puente `/ads/`, que además es lo que la UI ya usa.
4. **El andamiaje del compañero existe siempre.** Alternativa: recrear Safent al
   instalar. Elegido porque un bind no se añade a un contenedor vivo: con los binds
   siempre presentes, «Instalar» no interrumpe nada. **Se cede**: Safent se une
   siempre a la red del compañero.
5. **Descarga directa firmada + notarización obligatoria; nada de tiendas.**
   Alternativa: App Store. Incompatible con instalar y gobernar un motor local.
   **Se cede**: la distribución y la reputación del binario son nuestras.
6. **Actualizar con trabajo en curso: pausar la cola, esperar acotado, re-encolar.**
   Alternativa (que la propia spec ofrecía): advertir y cortar bajo confirmación.
   Rechazada porque introduce una decisión del usuario. **Se cede**: sólo se
   re-encolan ítems que el daemon marca reanudables.

**Complexity Tracking**: sin violaciones de la constitución que justificar. La
única desviación registrada es la condición del Principio 0 sobre
`/system/requests`, aceptada por ser marca + supervisión.

## Constitution Check — GATE POST-DISEÑO

| Principio | Veredicto | Evidencia en el diseño |
|---|---|---|
| **0 SUPREMO** | PASS | `/system/requests` valida un enum y escribe/lee un fichero. Toda la orquestación vive en el envoltorio (host) y en el CLI, no en el shell-server. Ningún razonamiento del agente se dispara por HTTP. |
| **I** | PASS | Contratos públicos intactos. |
| **II** | PASS | Sin cambios en el gate HITL. |
| **III** | PASS | Sin PII nueva. |
| **IV** | PASS | Firma no verificable → sin botón. Digest que no casa → no se aplica. Binario que no verifica → no se ejecuta. Verbo desconocido → 400. |
| **V** | PASS | `reconcile` es puro; el driver se dobla con un binario falso que emite NDJSON; los tests del frontend no tocan red. |

**Veredicto final: PASS.** Listo para `tasks.md`.

## Open questions for the user

1. ¿`safent-desktop.yml` reutiliza el `TAURI_SIGNING_PRIVATE_KEY` que ya existe en
   `agents-autonomy`, o Safent estrena par propio? (Irreversible: la clave pública
   queda embebida en cada instalador publicado.)
2. ¿Desde qué URL pública leerán las instalaciones `latest.json` y
   `runtime-manifest.json`? (Contrato externo observable.)
