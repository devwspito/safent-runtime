# Feature Specification: Safent Ads se instala con un botón (sin URL, sin terminal)

**Feature Directory**: `specs/029-safent-ads-instalar-un-boton/`
**Created**: 2026-09-10
**Status**: Resuelta — CL-001/CL-002/CL-003 cerradas el 10-sep-2026 (ver «Resolución de aclaraciones» al final)
**Input**: User description (verbatim, 10-sep-2026): «este MCP de safent-ads debe ser "Instalar" y punto. Luego me pedirá lo que necesite de meta y de google, pero no puedes pedirle al usuario que levante el MCP aparte y te dé el enlace, eso no tiene sentido.» · «safent-ads viene native installed → un botón y debe levantar el MCP y todo, y el onboarding y la UI debe pedirte las credenciales de meta y el OAuth de Google Cloud.»

## Contexto (por qué existe esta spec)

Hoy, en Herramientas, Safent Ads se presenta como un "managed-remote": una tarjeta con un
campo de texto «URL del servidor de Safent Ads» (marcador `https://ads.tu-dominio/mcp`) y un
botón «Conectar». Ese camino obliga al propietario a levantar el servicio por su cuenta y pegar
un enlace. El propietario lo rechaza: Safent Ads ya *viene instalado* como companion (024) y,
cuando no lo esté, debe bastar **un botón "Instalar"** que lo levante entero. La conexión por
URL manual sobra del camino normal; queda solo como escotilla avanzada.

Esta spec reescribe **qué** debe ocurrir al pulsar ese botón y en el onboarding posterior.
No decide **cómo** (imágenes, redes, marcas de host, verbos de CLI): eso es del arquitecto y el
tech-lead. Los mecanismos de host descritos abajo son **restricciones dadas**, no decisiones
que esta spec deba re-abrir (ver Assumptions).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — «Instalar» y punto: un botón levanta el companion entero (Priority: P1)

Desde Herramientas o desde la entrada «Anuncios» de la barra lateral (cuando aún no está
instalado), el propietario ve **una sola** acción: «Instalar». La pulsa y no se le pide nada más
para arrancar: el companion se levanta completo — se descarga su imagen publicada, se prepara su
red, su base de datos y sus migraciones, arranca el servicio y quedan registradas las
herramientas de anuncios del agente. Mientras tanto ve progreso honesto por etapas reales; al
terminar, la entrada «Anuncios» pasa de «no instalado» a «listo». En ningún punto de este camino
aparece un campo de URL ni un enlace que pegar.

**Why this priority**: es el corazón del pedido del propietario y define el MVP. Con solo esto,
Safent Ads deja de exigir pasos manuales y «viene instalado» de verdad: un botón lo materializa.
**Independent Test**: en una máquina sin companion, pulsar «Instalar» y, sin escribir ni pegar
nada, observar el progreso por etapas hasta que «Anuncios» quede «listo» y las herramientas de
anuncios del agente aparezcan — todo sin abrir una terminal.

**Acceptance Scenarios**:
1. **Given** el companion no está instalado, **When** el propietario abre Herramientas o «Anuncios», **Then** ve una única acción «Instalar» y ningún campo de URL ni de conexión.
2. **Given** pulsa «Instalar» con conexión y disco normales, **When** avanza el proceso, **Then** ve etapas reales (descarga de imagen → red → base de datos y migraciones → arranque → registro de herramientas) con avance visible, no un progreso simulado.
3. **Given** el proceso termina bien, **When** vuelve a la barra lateral, **Then** «Anuncios» está «listo» y las herramientas de anuncios del agente están presentes y utilizables, sin haber tocado la terminal.
4. **Given** el proceso falla en cualquier etapa (imagen no descargable, red en conflicto, migración fallida, agente del host ausente, sin conexión), **When** se dibuja el estado, **Then** ve el motivo concreto y la acción que lo desbloquea, con la opción de reintentar, y nunca un «conectado» falso.
5. **Given** una instalación en curso, **When** vuelve a pulsar, **Then** no se lanza una segunda instalación: el control refleja «instalando» hasta que el estado real cambie o la solicitud caduque.
6. **Given** el companion ya está presente (o a medio provisionar), **When** pulsa «Instalar», **Then** la operación converge al mismo estado final sin duplicar red, datos ni credenciales (idempotencia).

### User Story 2 — Onboarding: la UI pide lo de Meta y el OAuth de Google Cloud (Priority: P2)

Instalado el companion, el onboarding recoge **en la propia UI** lo que el servicio necesita de
Meta y el OAuth de Google Cloud, mediante OAuth y formularios dentro del panel del companion
(alcanzado por el puente same-origin `/ads/`, 026). Ninguna credencial se pide por variables de
entorno ni por línea de comandos, ni se pide al propietario editar ficheros. Con las credenciales
válidas y las cuentas conectadas, las herramientas de anuncios del agente quedan plenamente
utilizables; mientras falten, el estado lo dice con la acción que lo resuelve.

**Why this priority**: sin credenciales el companion está vivo pero ciego. Es lo segundo que pide
el propietario («me pedirá lo que necesite de meta y de google»), y se apoya en P1 sin bloquearlo:
P1 entrega un companion «listo» aunque todavía sin cuentas.
**Independent Test**: con el companion recién instalado y sin credenciales, seguir el onboarding
en la UI, introducir credenciales y conectar una cuenta por OAuth, y comprobar que el estado pasa
a «listo/con cuentas» y que las herramientas de anuncios del agente operan — sin env, sin argv,
sin terminal.

**Acceptance Scenarios**:
1. **Given** companion instalado sin credenciales, **When** el propietario entra al onboarding, **Then** la UI le pide, dentro del panel, lo de Meta y el OAuth de Google Cloud; nunca se le pide una variable de entorno ni un comando.
2. **Given** faltan credenciales o cuentas, **When** abre «Anuncios», **Then** ve `unauthorized` o `no_accounts` con la acción exacta que lo desbloquea, no un vacío ni un error genérico.
3. **Given** credenciales válidas y al menos una cuenta conectada, **When** el agente lista sus herramientas, **Then** las de anuncios están presentes y ejecutables.
4. **Given** credenciales introducidas por la UI, **When** se revisan registros y argumentos del proceso, **Then** ninguna credencial aparece en logs, argv ni variables de entorno visibles al propietario.

### User Story 3 — Re-levantar, reparar, quitar y la escotilla avanzada (Priority: P3)

El propietario puede, desde la UI y sin terminal: **re-levantar/reparar** el companion (cuando
está caído o a medio provisionar), y **quitarlo**. Y, solo tras una divulgación explícita
«avanzado» (desactivada por defecto), reaparece la vía heredada de auto-alojamiento: el viejo
campo de URL manual, para quien de verdad hospede el servicio por su cuenta.

**Why this priority**: cierra el ciclo de vida y preserva la salida de emergencia sin ensuciar el
camino por defecto. No es MVP: P1 ya deja el companion operativo.
**Independent Test**: con un companion caído, pulsar «Reparar» y verlo volver a «listo»; luego
«Quitar» y verlo desaparecer; comprobar que el campo de URL solo existe tras abrir «avanzado».

**Acceptance Scenarios**:
1. **Given** el companion está caído o incompleto, **When** el propietario pulsa «Reparar», **Then** se re-levanta y el estado vuelve a reflejar la salud real (idempotente, sin duplicar estado).
2. **Given** el companion instalado, **When** el propietario pulsa «Quitar», **Then** se retira y la entrada «Anuncios» vuelve a «no instalado», con la instalación disponible de nuevo.
3. **Given** el camino por defecto, **When** el propietario mira Herramientas, **Then** NO ve ningún campo de URL; solo aparece tras abrir explícitamente «avanzado».

### Edge Cases

- **Agente del host no corriendo**: la solicitud de instalación nunca se recoge. El estado no puede quedar en «instalando» eterno: caduca y vuelve a ofrecer «Instalar», diciendo honestamente que nadie la atendió.
- **Imagen no descargable (privada/bloqueada/sin red)**: fallo claro en la etapa de descarga, reintentable; nunca un «listo» falso.
- **Red ya existe de una instalación previa**: si coincide con la esperada, se reutiliza; si choca con otra distinta, falla fuerte y lo dice (nunca se elige otra a escondidas).
- **Companion a medio provisionar** (mitad de artefactos presentes): «Instalar»/«Reparar» converge al estado completo sin duplicar nada.
- **Fallo de migración de la base de datos**: se reporta como fallo de esa etapa, reintentable; el servicio no se anuncia «listo».
- **Segunda pulsación mientras instala**: no dispara una segunda instalación.
- **Sin conexión**: fallo honesto en descarga/arranque, con reintento cuando vuelva la red.
- **Safent arrancó sin capacidad de companion** (arranque con la opción de omitir): reflejar honestamente que dejarlo «listo» puede requerir que Safent vuelva a leer la presencia del companion (ver Dependencies & Risks y CL-002).

## Functional Requirements *(mandatory)*

- **FR-001**: El sistema DEBE ofrecer **una única** acción «Instalar» para el companion de anuncios, compartida por la tarjeta de Herramientas y por el estado `not_installed` de la entrada «Anuncios» de la barra lateral; ambas disparan exactamente el mismo flujo.
- **FR-002**: En el camino por defecto, el sistema NO DEBE solicitar en ningún momento una URL de servidor ni ningún dato de conexión para instalar o conectar Safent Ads.
- **FR-003**: Al pulsar «Instalar», el sistema DEBE dejar una **solicitud de instalación** que el **agente del host** recoge y cumple, del mismo modo que las actualizaciones disparadas desde la UI; el contenedor sandbox nunca crea contenedores hermanos por sí mismo.
- **FR-004**: El sistema DEBE mostrar **progreso honesto por etapas reales** (descarga de la imagen publicada, preparación de la red, base de datos y migraciones, arranque del servicio, registro de las herramientas de anuncios) y no un progreso simulado.
- **FR-005**: La operación DEBE ser **idempotente**: repetirla sobre un companion ya presente o a medio provisionar converge al mismo estado final sin duplicar red, datos ni credenciales.
- **FR-006**: Ante cualquier fallo, el sistema DEBE dejar un **estado limpio y reintentable**, con el motivo concreto y la acción que lo desbloquea, y nunca un «conectado» falso ni un estado a medias.
- **FR-007**: El botón y la entrada de la barra lateral DEBEN reflejar la **salud real** del companion (`not_installed` | `unreachable` | `unauthorized` | `no_accounts` | `ready`), derivada de su estado observado, nunca un estado fabricado.
- **FR-008**: Una segunda pulsación mientras la instalación está en curso NO DEBE lanzar una segunda instalación; el control refleja «instalando» hasta que el estado real cambie o la solicitud **caduque**.
- **FR-009**: Al completarse la instalación, el sistema DEBE registrar las **herramientas de anuncios nativas del agente** y hacer que «Anuncios» pase de `not_installed` a `ready` sin intervención por terminal.
- **FR-010**: Tras la instalación, el onboarding DEBE recoger **en la UI** lo que el servicio necesita de **Meta** y el **OAuth de Google Cloud**, mediante OAuth/formularios dentro del panel del companion; ninguna credencial se introduce por variables de entorno ni por línea de comandos. [NEEDS CLARIFICATION: ¿P2 incluye recoger en la UI las credenciales de la *app desarrolladora* del vendor (app de Meta + cliente OAuth de Google Cloud), que hoy se editan a mano en el host, o P2 se limita a conectar *cuentas* por OAuth dentro del panel ya existente?]
- **FR-011**: Con credenciales o cuentas ausentes, el estado DEBE ser `unauthorized`/`no_accounts` con la acción que lo resuelve; con credenciales válidas y cuenta conectada, las herramientas de anuncios del agente quedan **utilizables**.
- **FR-012**: El sistema DEBE ofrecer, desde la UI y sin terminal, **re-levantar/reparar** y **quitar** el companion, reflejando siempre el estado real.
- **FR-013**: La **vía avanzada de auto-alojamiento** (el campo de URL heredado) DEBE sobrevivir únicamente tras una divulgación explícita «avanzado», desactivada por defecto y ausente del camino por defecto.

### Non-Functional Requirements

- **NFR-001**: Cero pasos de terminal en los caminos por defecto de P1 y P2.
- **NFR-002**: Durante la instalación, el progreso DEBE mostrar avance visible al menos cada 15 s; una instalación estancada se distingue de una en marcha.
- **NFR-003**: Las credenciales del vendor y los tokens OAuth de cuentas son datos sensibles: nunca aparecen en logs, argv ni en variables de entorno visibles al propietario, y viven cifrados en el almacén del companion.
- **NFR-004**: El estado del companion DEBE distinguirse por algo más que color (texto/estado accesible), como ya exige la entrada «Anuncios» (026).

### Key Entities *(data involved)*

- **Companion de anuncios (safent-ads)**: infraestructura hermana **opcional** que Safent levanta en el HOST — una imagen publicada, red fija, base de datos con migraciones, panel propio y puente de herramientas. Invariante: su destino lo fija el instalador local, **nunca** una URL escrita por el propietario. Safent arranca y funciona aunque el companion esté ausente.
- **Solicitud de instalación (install-request)**: la marca que la UI deja para que el agente del host la cumpla. Se consume **una sola vez**; **caduca** si nadie la recoge (para no colgar la UI en «instalando» eterno).
- **Agente del host**: proceso del propio host (fuera del sandbox) que observa la marca y ejecuta la acción de host. **Único** autorizado a crear/levantar contenedores hermanos.
- **Estado de disponibilidad de Anuncios**: uno de `not_installed` | `unreachable` | `unauthorized` | `no_accounts` | `ready`. Reflejo honesto de la salud observada, nunca fabricado.
- **Herramientas de anuncios del agente**: las capacidades que el agente adquiere cuando el companion queda registrado y accesible.
- **Credenciales**: (a) de la **app desarrolladora del vendor** (app de Meta; cliente OAuth de Google Cloud) y (b) de **cuentas del cliente** (OAuth de Google/Meta). Ambas por la UI, nunca por env/argv. Distíngase de la **URL self-host heredada** (dato de conexión, no credencial).
- **Vía avanzada / self-host (heredada)**: el campo de URL manual del camino «managed-remote», superviviente solo tras la divulgación «avanzado».

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: **Cero** campos de URL o de conexión en el camino por defecto para dejar Safent Ads operativo (verificable por inspección del flujo: 0 inputs de conexión antes de `ready`).
- **SC-002**: **Cero** pasos de terminal para instalar y dejar operativo el companion desde la UI.
- **SC-003**: Desde «Instalar» hasta que «Anuncios» muestra `ready`, en banda ancha típica y con la imagen no cacheada, el tiempo p50 ≤ 5 min y p95 ≤ 10 min (objetivo revisable, ver Assumptions).
- **SC-004**: Tras la instalación, la entrada «Anuncios» aparece y las **herramientas de anuncios del agente están presentes y ejecutables** (verificable listando las herramientas del agente).
- **SC-005**: En una batería de instalaciones sobre estados diversos (limpio, con red previa, a medio provisionar, agente del host caído), **ninguna** deja un «conectado» falso; **todo** fallo es reintentable y termina en `ready` al reintentar cuando la causa se resuelve.

## Out of Scope

- Publicar campañas reales o cambiar presupuestos/pujas (lo cubren 024/026, no esta spec).
- Dar de alta la app en el lado de Google/Meta (crear el proyecto de Google Cloud, la app de Meta, sus permisos y revisión): eso es trabajo del propietario en las consolas de esos proveedores.
- El cuadro de mando de anuncios y sus señales (026).
- Rediseñar el panel interno del companion o su modelo de datos de campañas.

## Assumptions

- **A-1 (dado, no re-decidir)**: el runtime es un contenedor sandbox que **no puede** crear contenedores hermanos; levantar el companion se hace en el HOST vía el mecanismo de marca del agente de actualización — la UI deja una solicitud que el agente del host cumple, igual que las actualizaciones disparadas desde la UI.
- **A-2 (dado)**: una instalación normal **ya** provisiona el companion («viene instalado»); el botón cubre el caso **no instalado** y el de **re-levantar/reparar**.
- **A-3 (dado)**: las credenciales de Meta/Google se introducen en la UI (OAuth/formularios dentro del companion), **nunca** en env ni en argv.
- **A-4 (dado)**: la imagen del companion es `ghcr.io/devwspito/safent-ads` (ya pública), en la red interna fija `10.201.0.10:8443`.
- **A-5 (dado)**: el panel se alcanza por el puente same-origin `/ads/` (026) — sin URL externa.
- **A-6**: el mecanismo de marca+agente del host existente hoy atiende actualización/desinstalación de *Safent*, y re-provisiona el companion solo como **efecto colateral** de recrear Safent. Instalar/levantar **solo el companion** requiere una marca de solicitud nueva y una rama nueva del agente del host (verbos de host tipo «levantar companion»). Esto es implementación (arquitecto/tech-lead); la spec solo fija el comportamiento observable.
- **A-7**: instalación es un **único propietario** (instalación de escritorio de un solo dueño); no hay multi-tenant de credenciales del vendor.
- **A-8** (SC-003): el objetivo de tiempo asume banda ancha doméstica típica y una imagen de tamaño ordinario; se ajustará al medir contra la imagen real.

## Dependencies & Risks

- **Dependency**: agente del host instalado y vivo (launchd/systemd de usuario). Sin él, ninguna marca se cumple (ver edge case «agente del host no corriendo»).
- **Dependency**: imagen pública `ghcr.io/devwspito/safent-ads` descargable desde la máquina del propietario.
- **Dependency**: puente same-origin `/ads/` (026) y estados de disponibilidad de «Anuncios» (026) como fuente del estado mostrado.
- **Risk (el mayor)**: Safent lee la **presencia del companion** al arrancar (enlaces de solo lectura montados al iniciar el contenedor). Si Safent arrancó **sin** companion, levantarlo después puede **no** hacerlo visible al agente hasta que Safent vuelva a leer esa presencia — lo que en la práctica exige **recrear/reiniciar** el contenedor de Safent, con una breve interrupción. «Instalar» debe conseguir `ready` de verdad, así que el diseño tiene que resolver esta reconexión honestamente (ver CL-002). *Mitigación propuesta*: que la solicitud de instalación, al cumplirse en el host, deje a Safent capaz de ver el companion (recreación controlada con los enlaces), reflejando la interrupción en el progreso — nunca fingir `ready`.
- **Risk**: recoger credenciales del **vendor** en la UI (FR-010/CL-001) implicaría una nueva vía de escritura de secretos en el estado del host a través del agente; amplía superficie y debe pasar por `security-engineer`.
- **Risk**: marca de solicitud que caduca — calibrar el tiempo de caducidad para no cortar instalaciones lentas (imagen grande) ni colgar la UI si el agente está muerto.

## Security / Privacy / Compliance Notes

- **Datos sensibles**: credenciales de la app desarrolladora del vendor (app de Meta, cliente OAuth de Google Cloud) y tokens OAuth de cuentas de cliente. Viven cifrados en el almacén del companion; nunca en logs, argv ni env visibles (NFR-003).
- **Frontera de confianza**: el destino del companion lo fija el instalador local (config validada: esquema/host/ip/puerto/huella de CA fijos), **nunca** una URL escrita por el propietario en el camino por defecto. La solicitud de instalación es un disparador de escritura en el host: solo el agente del host actúa, el sandbox nunca crea hermanos.
- **Superficie de ataque a revisar por `security-engineer`**: (1) la nueva marca/rama del agente del host que levanta el companion (evitar que un proceso comprometido del contenedor la use para ejecutar acciones de host arbitrarias); (2) si FR-010 recoge credenciales del vendor, la vía de persistirlas al estado del host; (3) la escotilla avanzada de URL (P3) reintroduce la confianza en un destino escrito por el propietario — mantener su validación estricta y su desactivación por defecto.

## Ready for next step?

READY — CL-001, CL-002 y CL-003 resueltas el 10-sep-2026 (§«Resolución de aclaraciones»). El plan conjunto con 028 vive en `../028-safent-app-nativa/{plan,research,data-model,tasks}.md` y `../028-safent-app-nativa/contracts/`.

---

## Resolución de aclaraciones (10-sep-2026)

Decisiones del dueño y del coordinador. **Vinculantes.** El detalle y las
alternativas rechazadas viven en `../028-safent-app-nativa/research.md`.

### El principio rector de 028 aplica también aquí

- **FR-014**: Instalar Anuncios **NO DEBE tener ningún paso del usuario que pueda
  salir mal**: una pulsación y nada más. Cero comandos, cero URL, cero elecciones,
  nada que pegar.
- **FR-015**: Todo estado adverso DEBE **auto-sanarse sin preguntar**: red previa que
  coincide ⇒ se reutiliza; compañero a medio aprovisionar ⇒ se converge; descarga
  cortada ⇒ se reanuda; imagen envejecida ⇒ se re-baja verificada por **digest**;
  segunda pulsación ⇒ no dispara una segunda instalación.
- **FR-016**: Lo que no se pueda reparar DEBE mostrarse en **una sola pantalla** con
  la causa y **un** «Reintentar». **Prohibida toda instrucción de terminal.**
- **SC-006**: Una persona que **nunca ha oído la palabra «contenedor»** deja Anuncios
  operativo (instalado, con credenciales y con una cuenta conectada) **sin leer
  nada**: 5 de 5 intentos.
- **SC-007**: En una batería de 6 estados adversos (limpio · con red previa · a medio
  aprovisionar · agente del host caído · sin red · imagen envejecida), **0** piden
  algo al propietario más allá de pulsar «Reintentar», y **0** muestran un comando.

### CL-001 — alcance del onboarding: **incluye las credenciales del vendor**

La UI recoge **las credenciales de la app desarrolladora del vendor** (cliente OAuth
de Google Cloud y app de Meta con `app_id`/`app_secret`)
**y** conecta las cuentas por OAuth. Nunca por `vendor.env`, nunca por entorno,
nunca por línea de comandos.

**No se construye nada nuevo**: safent-ads **ya tiene** ese camino
(`GET/PUT/DELETE /platform-apps` con reautenticación, cifrado en el bróker y estado
enmascarado de vuelta; panel en `ConexionesPage` + `ConnectProviderCard`). El
onboarding lo **encadena**, no lo duplica.

- **FR-010 (revisado)**: el onboarding DEBE recoger en la UI las credenciales del
  vendor **y** conectar las cuentas, reutilizando el panel existente del compañero.
- **Corrección del 11-sep-2026**: Google retiró los developer tokens el 9-sep-2026.
  No se piden, almacenan ni envían, ni siquiera como campo opcional. El nivel de
  acceso depende del proyecto propietario del cliente OAuth. T021 debe comprobar
  este contrato y explicar el bloqueo de acceso a cuentas reales, no añadir secretos.
  Fuente: [migración oficial](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).

### CL-002 — «Instalar» **no** reinicia Safent en el caso normal

Sí, «Instalar» **puede** reiniciar Safent de forma transparente si hiciera falta —
pero el diseño hace que **no haga falta**: el **andamiaje del compañero** (red fija,
estado y los cuatro ficheros de sólo lectura) se aprovisiona **siempre**, esté o no
instalado el compañero. Así «Instalar» es bajar la imagen, levantar los servicios y
un verbo del daemon que **relee** la presencia — sin interrupción.

- **FR-017**: el andamiaje del compañero DEBE existir siempre, de modo que instalar
  Anuncios NO exija recrear Safent.
- **FR-018**: sólo una instalación **heredada** sin esos ficheros cae al camino de
  recreación; en ese caso la app DEBE declarar la interrupción breve en el progreso y
  devolver la ventana al producto. Nunca fingir `ready`.

Esto neutraliza el «Risk (el mayor)» de esta spec.

### CL-003 — «Quitar» **conserva** los datos

- **FR-019**: «Quitar» DEBE conservar estado y credenciales del compañero. Purgar
  exige una **segunda confirmación explícita** que nombre lo que se borra.
- **FR-020**: purgar DEBE llevarse **todo** lo purgable — incluidos los tres volúmenes
  del compañero (con la base de datos de campañas) y la red — y conservar DEBE
  conservarlos. Hoy ninguna de las dos cosas es cierta (matriz 025, CLI-11/CLI-12).

### Condición previa de esta spec

`ready` es hoy **inalcanzable** por el bloqueante **ADS-02** de la matriz 025: la
clave SSO del compañero llega ilegible para el daemon y el puente `/ads/` responde
503. Se corrige antes que nada (tarea T001). **`ready` no se declara nunca por
contadores de contenedores**: sólo cuando el puente responde de verdad.
