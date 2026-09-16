# Feature Specification: Safent controla otros equipos (el escritorio del dueño y los equipos del tailnet)

**Feature Directory**: `specs/031-remote-control-safent-controla-equipos/`
**Created**: 2026-09-10
**Status**: Draft — 3 preguntas abiertas
**Input**: User description (verbatim, 10-sep-2026): «remote-control» · «Safent tiene que poder controlar otros equipos» · sobre el control de escritorio nativo que vio en Codex para macOS: «es una locura cómo maneja los permisos».

El dueño usó «remote-control» para dos cosas distintas. Esta spec cubre **solo la segunda**:
**Safent actúa sobre equipos** — el suyo propio y otros del tailnet. Manejar Safent *desde
fuera* (mando a distancia sobre el producto) es la spec 030 y no se toca aquí.

## Contexto (por qué existe esta spec)

Hoy Safent ya tiene tools de «Pantalla y control» (ver la pantalla, mover el ratón, escribir,
grabar) y sabe usarlas en bucle. Pero el motor vive **enjaulado**, así que esas tools solo
alcanzan el escritorio de la propia jaula: una pantalla vacía que no es la del dueño. Desde el
chat, «ábreme esto en Numbers y rellena la columna C» es hoy imposible, y no por falta de
inteligencia del modelo, sino porque el motor no llega al escritorio real.

Para equipos remotos sí hay camino: el tailnet ya sirve comandos y ficheros por SSH con
gobierno propio (tarjeta la primera vez por equipo, lista de equipos aprobados, revocación con
segundo factor en Seguridad, registro sellado de cada ejecución) — spec 022. Lo que no existe
es **escritorio**: ver y operar la pantalla de otra máquina.

Y ha cambiado algo que lo vuelve viable: con la app nativa (spec 028) Safent ya **vive en el
equipo del dueño**, fuera de la jaula. La app puede pedir al sistema los permisos de accesibilidad
y de grabación de pantalla en su propio nombre, sostenerlos y ser la única puerta por la que el
motor alcanza un escritorio real. Sin la app, esto exigiría inventar una puerta nueva; con la
app, la puerta ya está y solo hay que gobernarla.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Desde el chat, Safent hace algo en una app de mi propio Mac (Priority: P1)

El dueño escribe en el chat de Safent: «abre Numbers, coge el presupuesto del trimestre y pásame
los totales por partida». Safent responde que para eso necesita ver la pantalla y usar ratón y
teclado de **este** equipo, dice qué permisos del sistema va a pedir y para qué, y los pide **una
sola vez** desde la propia app, con el diálogo del sistema de siempre. Concedidos, aparece una
tarjeta: *«Voy a operar Numbers en este equipo para: sacar los totales por partida. Durante esta
sesión.»* El dueño aprueba. A partir de ahí ve un indicador permanente de que Safent tiene el
control, ve el cursor moverse y ve, paso a paso, qué está haciendo. En cualquier momento toca el
ratón o pulsa la parada: Safent suelta el control en el acto y no vuelve a tocar nada sin una
tarjeta nueva. Al terminar, la sesión se cierra sola y el dueño puede leer, en el registro, cada
acción con su captura.

**Why this priority**: es el enunciado del dueño y la tajada mínima que entrega el resultado
observable. Todo lo demás (modo acotado, equipos remotos, desatendido) es extensión de este
recorrido. Además es la parte que hoy es *imposible*, no solo incómoda.
**Independent Test**: en un Mac con la app instalada y sin permisos concedidos, pedir por chat una
tarea en una app de escritorio y comprobar: los permisos se piden una vez y desde la app, hay
tarjeta antes de la primera acción, hay indicador visible mientras dura, la parada corta la
entrada en el acto y el registro contiene cada acción. Sin tocar nada de US2, US3 ni US4.

**Acceptance Scenarios**:
1. **Given** Safent recién instalado y el control de escritorio nunca activado, **When** el dueño
   mira lo que Safent puede hacer, **Then** el control de escritorio está **apagado**, se declara
   como tal, y ninguna petición del modelo puede encenderlo: solo el dueño lo enciende.
2. **Given** el control encendido pero sin permisos del sistema, **When** el modelo necesita ver la
   pantalla o mover el ratón, **Then** la **app** declara qué permiso hace falta, para qué sirve y
   qué pasa si no se concede, y lo pide ella; en ningún caso lo pide el motor ni aparece un
   diálogo que el dueño no pueda relacionar con Safent.
3. **Given** los permisos concedidos, **When** el modelo va a actuar por primera vez sobre una
   aplicación, **Then** el dueño ve **una** tarjeta que nombra la aplicación, el objetivo en su
   idioma y el alcance temporal, y el trabajo se reanuda solo si aprueba; si rechaza, no se ejecuta
   ninguna acción y el chat lo dice.
4. **Given** una sesión de control aprobada sobre una aplicación, **When** el modelo intenta actuar
   sobre **otra** aplicación, **Then** hace falta una tarjeta nueva: una aprobación jamás se
   extiende a aplicaciones que el dueño no vio en la tarjeta.
5. **Given** Safent operando, **When** el dueño mira su pantalla, **Then** hay un indicador visible
   e inequívoco de que Safent tiene el control, con la aplicación en curso y la parada a un gesto.
6. **Given** Safent en mitad de una secuencia, **When** el dueño mueve el ratón o teclea,
   **Then** Safent cede el control inmediatamente, deja de enviar entrada y pide confirmación
   antes de retomar; el dueño siempre gana.
7. **Given** Safent en mitad de una secuencia, **When** el dueño acciona la parada, **Then** la
   entrada cesa dentro del límite de NFR-001, la sesión queda cerrada y hace falta tarjeta nueva
   para volver a actuar; nada queda «a medio pulsar» sin declararlo.
8. **Given** el modelo encuentra un campo de contraseña, un cobro, un ajuste del sistema o un
   borrado fuera del espacio de trabajo, **When** intenta actuar ahí, **Then** queda **bloqueado**
   y se lo dice al dueño; no hay escritura ciega, ni siquiera con la sesión aprobada.
9. **Given** una sesión terminada, **When** el dueño abre el registro, **Then** ve cada acción
   (qué, dónde, cuándo, con qué tarjeta) con su captura, en orden y sin huecos.

### User Story 2 — Modo acotado: yo reviso de antemano lo que Safent podrá hacer (Priority: P2, la primera de su grupo)

El dueño no quiere aprobar tarjeta tras tarjeta para un trabajo largo y repetitivo. Antes de
empezar, Safent le presenta un **cuadro de límites** en su idioma: estas aplicaciones y no otras,
estas acciones y no otras, esta ventana de tiempo, este número máximo de acciones, y lo que jamás
podrá hacer. El dueño lo lee, lo ajusta y lo aprueba. Dentro de ese cuadro Safent trabaja seguido,
sin interrumpir; en cuanto algo se sale del cuadro, se **bloquea** y lo cuenta — no pide permiso
por sorpresa a mitad de faena. El dueño puede ver el cuadro vigente y retirarlo en cualquier
momento desde Seguridad.

**Why this priority**: es lo que convierte una demo en trabajo útil sin degradar el gobierno.
Va la primera de su grupo porque es el prerrequisito de confianza para US3 y US4: sin un cuadro
revisado y acotado, ni el escritorio remoto ni lo desatendido deberían existir.
**Independent Test**: con US1 funcionando, definir un cuadro de límites para dos aplicaciones,
lanzar una tarea larga y comprobar: cero interrupciones dentro del cuadro, bloqueo declarado en
el primer intento fuera de él, y que retirar el cuadro devuelve al régimen de tarjeta por
aplicación.

**Acceptance Scenarios**:
1. **Given** el dueño quiere trabajo seguido, **When** Safent propone el modo acotado, **Then** el
   cuadro de límites se muestra entero y en lenguaje del dueño antes de aprobar nada: aplicaciones,
   acciones, duración, tope de acciones y lista de lo prohibido.
2. **Given** un cuadro aprobado, **When** Safent trabaja dentro de él, **Then** no interrumpe con
   tarjetas por cada acción y el indicador sigue visible en todo momento.
3. **Given** un cuadro aprobado, **When** Safent intenta algo fuera del cuadro (otra aplicación,
   otra acción, pasada la ventana, superado el tope), **Then** queda bloqueado y se lo dice al
   dueño; no se convierte en una tarjeta más ni se amplía solo.
4. **Given** un cuadro vigente, **When** el dueño lo mira en Seguridad, **Then** ve qué autoriza,
   cuánto le queda y cuántas acciones lleva, y puede retirarlo de un gesto con efecto inmediato
   sobre el trabajo en curso.
5. **Given** el cuadro no fue revisado por el dueño (propuesto por el modelo, importado, heredado),
   **When** se intenta usar, **Then** no sirve: solo un cuadro aprobado explícitamente por el dueño
   habilita el modo acotado.

### User Story 3 — El escritorio de otra máquina mía, con el mismo gobierno (Priority: P2)

El dueño tiene otro Mac y un par de servidores en su tailnet. Hoy Safent ya les ejecuta comandos y
mueve ficheros con tarjeta por equipo. Ahora quiere lo mismo con la **pantalla**: «entra en el Mac
del salón y arregla esa app que se quedó colgada». Safent declara a qué equipo va, pide tarjeta
para **ese** equipo y **esa** aplicación, muestra lo que ve mientras actúa y responde a la misma
parada. Aprobar SSH en un equipo no aprueba su escritorio: son permisos distintos y el escritorio
es el más fuerte.

**Why this priority**: multiplica el valor sin cambiar el contrato de gobierno, pero no es la
promesa mínima; US1 es demostrable y valiosa sin esto. Se apoya en el gobierno por equipo que ya
existe, no lo reinventa.
**Independent Test**: con dos equipos en el tailnet, pedir una tarea de escritorio en el remoto y
comprobar: tarjeta propia por equipo, tarjeta propia por aplicación, misma parada, mismo registro;
y que un equipo con SSH ya aprobado **sigue** pidiendo tarjeta para el escritorio.

**Acceptance Scenarios**:
1. **Given** un equipo del tailnet con SSH ya aprobado, **When** Safent quiere operar su
   escritorio, **Then** pide una aprobación **nueva y distinta**, que nombra el equipo y declara
   que se trata de ver la pantalla y usar ratón y teclado de esa máquina.
2. **Given** un equipo remoto sin la app instalada o sin permisos concedidos allí, **When** se
   intenta el control, **Then** el estado se declara con su nombre y su acción; nunca un fallo
   mudo ni un intento a ciegas.
3. **Given** control aprobado sobre un equipo remoto, **When** Safent actúa, **Then** el dueño ve
   lo que Safent ve, ve qué máquina es en todo momento y puede parar con el mismo gesto que en el
   equipo propio.
4. **Given** el equipo remoto se desconecta, se suspende o queda bloqueado a mitad, **When**
   ocurre, **Then** la sesión se cierra declarando el estado, no se reintenta a ciegas y el
   registro refleja hasta dónde se llegó.
5. **Given** cualquier equipo con control de escritorio aprobado, **When** el dueño lo revoca en
   Seguridad, **Then** el trabajo en curso se corta y la siguiente vez vuelve a pedir tarjeta.

### User Story 4 — Trabajo programado y sin mirar, con la correa más corta (Priority: P3)

El dueño quiere que algo ocurra a las 7:00 sin él delante: un informe que solo se saca de una app
de escritorio. Safent lo permite **solo** dentro de un cuadro acotado vigente, solo sobre las
aplicaciones y equipos que el dueño puso en su lista, con tope de acciones y de tiempo, y sin
poder aprobar nada por su cuenta: lo que en presencia sería una tarjeta, sin dueño delante es un
**bloqueo**. Al volver, el dueño encuentra el resultado y el registro completo con capturas, y
puede revocar cualquier permiso de una vez.

**Why this priority**: es el uso más útil a largo plazo y también el más peligroso; llega cuando
el gobierno de US1–US3 esté demostrado. US1, US2 y US3 son válidas y demostrables sin esto.
**Independent Test**: programar una tarea de escritorio con el dueño ausente y comprobar: se
ejecuta solo lo del cuadro, cualquier cosa fuera queda bloqueada y anotada (no pendiente de
aprobación eterna), y el registro reconstruye la sesión entera.

**Acceptance Scenarios**:
1. **Given** una tarea programada sin dueño delante, **When** llega a algo que exigiría tarjeta,
   **Then** se **bloquea** y lo anota; jamás se auto-aprueba ni se queda esperando indefinidamente
   ocupando el equipo.
2. **Given** una tarea programada, **When** se lanza sin un cuadro acotado vigente para esas
   aplicaciones y equipos, **Then** no arranca y lo declara.
3. **Given** una tarea desatendida en curso, **When** el dueño vuelve y toca el equipo, **Then**
   Safent cede el control de inmediato (US1 escenario 6) y la tarea queda en pausa declarada.
4. **Given** cualquier sesión desatendida terminada, **When** el dueño la revisa, **Then** el
   registro reconstruye qué se hizo, sobre qué equipo y aplicación, con capturas y con el cuadro
   que lo autorizaba.

### Edge Cases

**Permisos y sistema**: el dueño revoca el permiso del sistema a mitad de una tarea · el permiso
existe pero el sistema lo ignora tras una actualización del sistema operativo · la app se actualiza
y el sistema considera que es «otra» y exige volver a conceder · la pantalla se bloquea o entra el
salvapantallas a mitad · el portátil se cierra · varias pantallas, o una pantalla que se desconecta
en mitad de la secuencia · escalado y resoluciones distintas entre lo que se ve y lo que se toca ·
dos sesiones de usuario abiertas en el mismo Mac · el dueño está compartiendo pantalla o grabando
una reunión mientras Safent actúa · el equipo remoto está en la pantalla de inicio de sesión.

**Aplicaciones**: la aplicación no responde o abre un diálogo modal que se come la entrada · la
aplicación está a pantalla completa · la ventana aprobada se cierra, se renombra o se duplica ·
aparece un campo de contraseña, un cobro o un ajuste del sistema en mitad de un formulario ·
teclados y acentos (idioma del teclado distinto del texto a escribir, entrada por composición) ·
textos muy largos · portapapeles con contenido del dueño.

**Concurrencia y control**: el dueño y Safent moviendo el ratón a la vez · dos tareas de Safent
queriendo el escritorio al mismo tiempo · una tarea desatendida arrancando mientras el dueño
trabaja · la parada accionada justo entre «ver» y «hacer clic».

**Equipos remotos**: equipo apagado, dormido o fuera del tailnet · el equipo remoto tiene una
versión de Safent distinta de la del equipo del dueño · el nombre del equipo cambia · latencia alta
que hace que lo que se ve ya no sea lo que hay.

## Functional Requirements *(mandatory)*

### Quién puede tocar un escritorio real

- **FR-001**: El control de un escritorio real MUST llegar siempre a través de la **app instalada
  en ese equipo**. El motor enjaulado MUST NO poder actuar por su cuenta sobre ningún escritorio
  real, ni el del dueño ni el de un equipo remoto.
- **FR-002**: Los permisos del sistema (ver la pantalla, controlar ratón y teclado) MUST pedirlos
  la app en su propio nombre y en el equipo donde se van a usar; MUST NO pedirse desde dentro de la
  jaula ni presentarse al dueño como un diálogo que no pueda relacionar con Safent.
- **FR-003**: La app MUST declarar, antes de pedir cada permiso, para qué sirve, qué podrá hacer
  Safent con él y qué deja de funcionar si no se concede.
- **FR-004**: Los permisos MUST pedirse **una sola vez** por equipo; una vez concedidos, el uso
  diario MUST NO repetir el diálogo del sistema.
- **FR-005**: MUST existir **un único paso gobernado** entre el motor y el escritorio real: toda
  acción pasa por ahí, se autoriza ahí y se registra ahí. Nada dentro de la jaula MUST poder
  alcanzar el escritorio por otra vía.
- **FR-006**: El estado de los permisos de cada equipo MUST ser visible en Seguridad, con su fecha
  y su camino para retirarlos; si el sistema los revoca por fuera, Safent MUST detectarlo y
  declararlo en vez de fallar en silencio.

### Encendido, tarjetas y alcance

- **FR-007**: El control de escritorio MUST venir **apagado** de fábrica y MUST encenderlo solo el
  dueño; ninguna petición del modelo, habilidad, compañero, cuadro importado ni tarea programada
  MUST poder encenderlo.
- **FR-008**: Antes de la primera acción sobre una aplicación en una sesión, el dueño MUST ver
  **una** tarjeta que nombre equipo, aplicación, objetivo y alcance temporal, y el trabajo MUST
  reanudarse solo si aprueba — una tarjeta, no una cadena de confirmaciones.
- **FR-009**: Una aprobación MUST valer solo para lo que la tarjeta nombró: otra aplicación, otro
  equipo u otra sesión exigen tarjeta nueva. Si esa aprobación se recuerda entre sesiones o muere
  con la sesión está sin decidir: ver pregunta abierta 1.
- **FR-010**: Sin dueño delante (tarea programada, ciclo autónomo), lo que exigiría tarjeta MUST
  **bloquearse**, nunca auto-aprobarse ni quedar pendiente de forma indefinida.
- **FR-011**: El modo acotado MUST exigir un cuadro de límites aprobado explícitamente por el
  dueño, expresado en su idioma, con aplicaciones, acciones permitidas, ventana de tiempo, tope de
  acciones y lista de lo prohibido; lo que se sale del cuadro MUST bloquearse, no convertirse en
  una tarjeta.
- **FR-012**: Los cuadros vigentes y los equipos con control aprobado MUST poder consultarse y
  revocarse desde Seguridad, con efecto **inmediato** sobre cualquier trabajo en curso; revocar
  MUST ser una acción soberana del dueño (segundo factor), igual que revocar equipos de SSH.
- **FR-013**: Aprobar comandos remotos en un equipo MUST NOT aprobar su escritorio: son permisos
  distintos y el de escritorio es el más fuerte.

### Lo que nunca ocurre sin que el dueño lo vea

- **FR-014**: Safent MUST NO escribir en campos de contraseña ni en campos ocultos por el sistema,
  MUST NO confirmar pagos ni compras, MUST NO tocar ajustes del sistema o de seguridad del equipo y
  MUST NO borrar ni mover ficheros fuera del espacio de trabajo. Estas acciones quedan bloqueadas
  aunque haya sesión aprobada o cuadro vigente; solo el dueño, presente y con segundo factor, puede
  autorizarlas caso por caso, y nunca en modo desatendido.
- **FR-015**: Mientras Safent tenga el control MUST haber un indicador visible e inequívoco en la
  pantalla afectada, con la aplicación en curso y la parada a un gesto.
- **FR-016**: MUST existir una **parada** alcanzable siempre, aunque el modelo esté en mitad de un
  bucle, que corta la entrada, cierra la sesión y exige tarjeta nueva para retomar.
- **FR-017**: La entrada del dueño MUST tener prioridad absoluta: si el dueño mueve el ratón o
  teclea, Safent cede el control en el acto y MUST pedir confirmación para retomar.
- **FR-018**: Cada sesión de control MUST caducar sola por inactividad y por duración máxima, sin
  que el dueño tenga que acordarse de cerrarla.

### Registro y verdad

- **FR-019**: Cada acción sobre un escritorio (qué se hizo, sobre qué equipo, aplicación y ventana,
  cuándo, y qué tarjeta o cuadro la autorizaba) MUST quedar en el **registro sellado**, en orden,
  sin huecos y sin poder alterarse a posteriori.
- **FR-020**: Cada acción MUST guardar la captura de lo que había en pantalla al decidirla, con
  retención declarada y purga a un gesto del dueño; el plazo concreto está sin decidir: ver pregunta
  abierta 2.
- **FR-021**: El contenido tecleado en campos de contraseña u ocultos MUST NO registrarse jamás, ni
  en el registro, ni en las capturas, ni en el diagnóstico exportable.
- **FR-022**: Todo estado de indisponibilidad MUST declararse con nombre propio y acción: permiso
  no concedido, permiso retirado, pantalla bloqueada, aplicación que no responde, equipo remoto
  fuera de línea, versiones que no casan, control en manos del dueño.
- **FR-023**: Si las versiones de Safent del equipo que manda y del equipo controlado no casan,
  Safent MUST declararlo y negarse a actuar, en vez de operar a medias entendiéndose mal.
- **FR-024**: Con varias pantallas, Safent MUST declarar sobre cuál actúa y MUST NO actuar sobre
  coordenadas que no ha visto en la captura que sostiene la decisión.

### Independencia del modelo

- **FR-025**: El control de escritorio MUST funcionar con cualquier modelo que el dueño configure;
  las tarjetas, los límites, la parada, el registro y los bloqueos MUST vivir en Safent y MUST NO
  depender de qué modelo esté detrás. Safent es el entorno, no aloja modelos.
- **FR-026**: Cambiar de modelo MUST NO conceder permisos, MUST NO ampliar cuadros vigentes y MUST
  NO saltarse ninguna tarjeta.

### Non-Functional Requirements

- **NFR-001**: Desde el gesto de parada hasta que no se envía ni una acción más al escritorio:
  **≤ 300 ms p99**, y ≤ 1 s p99 para un equipo remoto del tailnet.
- **NFR-002**: Desde que el dueño toca ratón o teclado hasta que Safent deja de enviar entrada:
  **≤ 300 ms p99**.
- **NFR-003**: El indicador de control visible MUST aparecer antes de la primera acción de la
  sesión, no después.
- **NFR-004**: Caducidad por defecto de una sesión de control: 15 minutos sin actividad y 60
  minutos de duración máxima, ambas declaradas en la tarjeta.
- **NFR-005**: Vocabulario del dueño en toda la superficie: la tarjeta y el cuadro de límites deben
  entenderse sin saber qué es un permiso del sistema ni una jaula.
- **NFR-006**: Teclado completo, foco visible, contraste AA y estados anunciables por lector de
  pantalla en tarjeta, indicador, parada y registro.
- **NFR-007**: Interfaz en español, identificadores en inglés.
- **NFR-008**: El registro y las capturas MUST quedarse en el equipo del dueño; ningún fotograma de
  su pantalla sale del equipo por defecto.

### Key Entities *(lenguaje ubicuo)*

- **Equipo del dueño (host)**: la máquina donde el dueño instaló y abre la app. Invariante: es el
  único sitio desde el que Safent puede alcanzar un escritorio real sin pasar por el tailnet.
- **Escritorio**: pantalla, ratón, teclado y aplicaciones **reales** de un equipo. Invariante: el
  escritorio de la jaula no es el escritorio de nadie; lo que esta spec gobierna es el real.
- **Motor enjaulado**: el servicio que razona y decide, encerrado. Invariante: nunca toca un
  escritorio real por su cuenta; solo propone acciones que otro ejecuta bajo gobierno.
- **Conductor de escritorio (desktop driver)**: lo que ejecuta de verdad la acción en un equipo, con
  los permisos del sistema de ese equipo. Invariante: vive fuera de la jaula, dentro de la app del
  equipo que controla, y nunca acepta órdenes que no vengan del paso gobernado.
- **Paso gobernado**: la única vía por la que una decisión del motor se convierte en acción sobre un
  escritorio. Invariante: uno solo, siempre auditado, apagado por defecto.
- **Permiso del sistema**: la autorización que concede el sistema operativo del equipo para ver la
  pantalla y para controlar ratón y teclado. Invariante: la pide y la sostiene la app del equipo, no
  Safent «en general»; el dueño la retira cuando quiere y Safent lo nota.
- **Sesión de control**: el tramo acotado en que Safent puede actuar sobre una aplicación de un
  equipo. Invariante: nace de una tarjeta, caduca sola, muere con la parada.
- **Tarjeta**: la aprobación del dueño que desbloquea el trabajo en el momento y lo reanuda.
  Invariante: una por alcance, nunca una cadena; sin dueño delante no hay tarjeta, hay bloqueo.
- **Cuadro de límites (bounded manifest)**: lo que el dueño revisa y aprueba de antemano para
  trabajar seguido. Invariante: lo aprueba el dueño, nunca el modelo; fuera del cuadro se bloquea.
- **Acción prohibida**: contraseñas, pagos, ajustes del sistema, borrados fuera del espacio de
  trabajo. Invariante: bloqueadas por defecto; solo el dueño presente y con segundo factor las
  levanta, caso por caso.
- **Parada**: el gesto que corta la entrada al instante. Invariante: alcanzable siempre, gane quien
  gane la carrera.
- **Testigo del control**: quién manda ahora sobre ratón y teclado, el dueño o Safent. Invariante:
  el dueño lo recupera con solo tocar el equipo.
- **Registro sellado**: la memoria inalterable de lo que se hizo. Invariante: sin huecos y sin
  secretos dentro.
- **Equipo del tailnet**: otra máquina del dueño, alcanzable por su red privada. Invariante:
  aprobar comandos ahí no aprueba su escritorio.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Un dueño sin conocimientos técnicos pide por chat una tarea en una app de escritorio y
  la ve hecha, concediendo permisos una sola vez y aprobando una sola tarjeta: 5 de 5 intentos, sin
  ayuda y sin terminal.
- **SC-002**: En 50 acciones de escritorio ejecutadas, **0** ocurren sin tarjeta o cuadro vigente
  que las autorice, y las 50 aparecen en el registro sellado con su captura: 0 huecos.
- **SC-003**: La parada corta la entrada en ≤ 300 ms p99 en el equipo propio y ≤ 1 s p99 en un
  equipo remoto: 20 de 20 mediciones.
- **SC-004**: El dueño recupera el control tocando el ratón en ≤ 300 ms p99: 20 de 20.
- **SC-005**: En 20 intentos provocados contra campos de contraseña, cobros, ajustes del sistema y
  borrados fuera del espacio de trabajo, **20 quedan bloqueados**; 0 caracteres tecleados en campos
  ocultos y 0 apariciones de esos caracteres en registro, capturas o diagnóstico.
- **SC-006**: Instalación nueva: sin que el dueño lo encienda expresamente, **0** acciones de
  escritorio posibles en 10 de 10 equipos, incluso pidiéndolo por chat de forma insistente.
- **SC-007**: Los permisos del sistema se piden **una vez**: en 30 días de uso normal, 0 diálogos
  del sistema repetidos y 0 tareas fallidas por permiso perdido sin declararlo.
- **SC-008**: De 12 fallos provocados (permiso retirado a mitad, pantalla bloqueada, app que no
  responde, ventana cerrada, pantalla desconectada, dos pantallas, equipo remoto apagado, versiones
  que no casan, dueño tomando el control, tope de acciones alcanzado, cuadro caducado, sin dueño
  delante), 12 declaran estado con nombre y acción; 0 cuelgues mudos y 0 reintentos a ciegas.
- **SC-009**: Trabajo largo en modo acotado: **0** interrupciones dentro del cuadro y **100 %** de
  bloqueos declarados al primer intento fuera de él, en 10 tareas.
- **SC-010**: Revocar desde Seguridad corta el trabajo en curso y obliga a tarjeta nueva: 10 de 10,
  con 0 acciones ejecutadas después de la revocación.
- **SC-011**: Un equipo con comandos remotos ya aprobados sigue exigiendo aprobación propia para el
  escritorio: 10 de 10.
- **SC-012**: El mismo recorrido de US1 se completa con **3 modelos distintos** configurados por el
  dueño, con idénticas tarjetas, bloqueos y registro: 3 de 3.
- **SC-013**: En 10 sesiones desatendidas, **0** auto-aprobaciones, 0 sesiones colgadas esperando a
  un dueño ausente y 10 registros que reconstruyen la sesión entera.
- **SC-014**: 0 fotogramas de la pantalla del dueño salen del equipo en ninguna de las pruebas
  anteriores.

## Out of Scope

Manejar Safent **desde fuera** (mando a distancia sobre el producto: spec 030) · Windows en esta
entrega · iOS y Android, tanto de mando como de controlado · sustituir el navegador automatizado
que ya existe para tareas web · sustituir los comandos y ficheros remotos por SSH que ya existen
(022): esta spec añade escritorio, no los reemplaza · un producto de escritorio remoto para que el
dueño mire y trabaje él a mano en otra máquina · controlar equipos fuera del tailnet del dueño ·
equipos de terceros, empleados o flotas · retransmitir o guardar la pantalla del dueño fuera de su
equipo · cambiar el motor, la jaula o las tools de pantalla que hoy operan el escritorio de la
propia jaula (siguen funcionando igual para lo suyo) · grabar la pantalla como producto (ya existe)
· accesibilidad asistida como funcionalidad para terceros.

## Assumptions

1. La app nativa (028) existe en el equipo del dueño antes de esta entrega; sin ella no hay a quién
   colgar los permisos del sistema y este trabajo no arranca.
2. Sistemas servidos en esta ronda: macOS Apple Silicon y Linux de escritorio, tanto de mando como
   de controlados. Windows queda fuera y se declara como no servido.
3. En un equipo remoto, controlar su escritorio exige que el dueño haya instalado allí la app y
   concedido allí los permisos; no hay control de escritorio sobre una máquina «pelada».
4. El gobierno por equipo que ya existe para comandos remotos (tarjeta la primera vez, lista de
   equipos aprobados, revocación con segundo factor en Seguridad, registro sellado) es el suelo
   sobre el que se apoya el escritorio remoto; aquí se refuerza, no se reinventa.
5. En US1 el dueño está delante y ve la pantalla; el gobierno se apoya en eso. Lo desatendido es
   US4 y tiene reglas más estrictas por eso mismo.
6. Las capturas del escritorio contienen lo que hubiera en pantalla, incluidos datos personales del
   dueño; se tratan como dato sensible propio y se quedan en su equipo.
7. El dueño acepta esperar unos segundos por acción a cambio de ver qué se hace; lo que no acepta
   es no poder parar.
8. «Espacio de trabajo» significa las carpetas que el dueño ya declaró como de Safent; todo lo
   demás del disco es «fuera» a efectos de FR-014.
9. Un dueño por equipo. Varias sesiones de usuario simultáneas en el mismo Mac no se sirven.
10. Los modelos son elección del dueño y pueden cambiar en cualquier momento; ninguna garantía de
    esta spec descansa en el comportamiento de un modelo concreto.
11. El escritorio de la jaula sigue existiendo y las tools actuales de pantalla siguen sirviendo
    para él; esta spec no las retira ni las amplía a escondidas.

## Dependencies & Risks

- **Dependency**: la app nativa (028) — es quien sostiene los permisos del sistema y hospeda el
  conductor. Si 028 se retrasa, esta spec no tiene suelo.
- **Dependency**: el gobierno de tailnet y equipos aprobados (022) para US3.
- **Dependency**: los permisos del sistema operativo de cada equipo, que Safent no controla y que el
  dueño o una actualización del sistema pueden retirar en cualquier momento.
- **Dependency**: la firma de la app: los permisos del sistema se atan a la app firmada; un cambio
  en la cadena de firma obliga a volver a conceder.
- **Risk**: control de ratón y teclado sobre el equipo del dueño es la capacidad más peligrosa del
  producto: quien mueva el cursor puede hacer casi cualquier cosa que el dueño puede hacer.
  Mitigación: apagado por defecto (FR-007), acciones prohibidas duras (FR-014), parada (FR-016),
  prioridad del dueño (FR-017) y traspaso obligatorio a `security-engineer`.
- **Risk**: un modelo con capturas de pantalla del dueño es un canal directo de fuga de datos
  personales (correo, banca, conversaciones). Mitigación: NFR-008, FR-020, FR-021 y decisión de
  retención por el dueño.
- **Risk**: las capturas son también la superficie de un ataque por lo que se ve en pantalla: un
  contenido malicioso en la propia pantalla puede intentar dirigir al modelo. Mitigación: cuadro
  acotado (FR-011), acciones prohibidas (FR-014) y traspaso a `security-engineer`.
- **Risk**: la fatiga de tarjetas empuja al dueño a aprobar sin leer o a exigir un «aprobar todo»;
  ahí muere el gobierno. Mitigación: US2 como válvula legítima con límites revisados, y FR-008
  «una tarjeta, no una cadena».
- **Risk**: la parada es la promesa central; si llega tarde una sola vez, el producto pierde la
  confianza entera. Mitigación: NFR-001 y NFR-002 como regresión permanente medida, no como
  intención.
- **Risk**: versiones distintas entre el equipo que manda y el controlado producen acciones
  interpretadas a medias. Mitigación: FR-023, negarse antes que improvisar.
- **Risk**: el escritorio remoto puede confundirse con «entrar en la máquina de otro»; el alcance
  debe quedar atado a equipos del propio dueño. Mitigación: Out of Scope y FR-013.
- **Risk**: si el modo acotado se puede proponer o rellenar desde el propio modelo, el gobierno se
  vuelve decorativo. Mitigación: FR-011 y US2 escenario 5.

## Security / Privacy / Compliance Notes

- **Datos sensibles**: capturas del escritorio del dueño (pueden contener correo, banca,
  conversaciones, datos de clientes y de terceros) · lo tecleado durante una sesión · nombres de
  aplicaciones, ventanas y equipos · los permisos del sistema en sí, que son una llave del equipo.
- **Fronteras de confianza**: motor enjaulado ↔ paso gobernado (aquí se decide qué sale de la
  jaula) · paso gobernado ↔ conductor del equipo (autoridad plena sobre ratón, teclado y pantalla)
  · app ↔ sistema operativo (concesión y retirada de permisos) · equipo del dueño ↔ equipo remoto
  del tailnet · **pantalla ↔ modelo**: lo que se ve es entrada no confiable que puede intentar
  dirigir al agente.
- **Superficie**: quien alcance el paso gobernado alcanza el equipo entero del dueño; quien alcance
  las capturas alcanza su vida digital; quien consiga colar una acción sin tarjeta ha roto la
  promesa del producto. El modo desatendido amplía la ventana temporal de todo lo anterior.
- **Traspaso STRIDE a `security-engineer`**: autorización del paso gobernado (quién puede pedir una
  acción y con qué prueba) · imposibilidad de saltarse la tarjeta desde una habilidad, un compañero,
  un cuadro importado o un ciclo autónomo · custodia y retención de capturas · lista dura de
  acciones prohibidas y su resistencia a que el contenido de la pantalla las induzca · el escritorio
  remoto como escalada desde un equipo ya comprometido · la parada como camino que debe funcionar
  incluso con el motor saturado o caído.
- **Cumplimiento**: capturas del escritorio pueden contener datos personales de terceros (RGPD);
  hace falta retención declarada, purga a un gesto y ninguna salida del equipo por defecto (FR-020,
  NFR-008). Grabar la pantalla de un equipo compartido puede afectar a terceros que no consintieron;
  se limita a equipos del propio dueño.

## Preguntas abiertas para el dueño

1. [NEEDS CLARIFICATION: ver FR-009 — ¿la aprobación de una aplicación vale **solo para la sesión**
   (más fricción, más control) o se **recuerda por aplicación y equipo** hasta revocarla, como hoy
   se recuerdan los equipos con SSH aprobado (menos fricción, ventana abierta más ancha)?]
2. [NEEDS CLARIFICATION: ver FR-020 — retención de las capturas de la pantalla del dueño: ¿cuánto
   tiempo se guardan, quién puede verlas dentro del producto y dónde se purgan de un gesto?]
3. [NEEDS CLARIFICATION: ver US4 — ¿se permite el control **desatendido sobre el propio equipo del
   dueño**, o lo programado se limita a equipos remotos dedicados, dejando el Mac del dueño solo
   para trabajo con él delante?]

## Ready for next step?

BLOCKED — las tres preguntas condicionan el alcance del permiso (1), la postura de privacidad (2) y
el alcance del modo más peligroso (3); deben resolverse en `/team-clarify` antes de planificar. El
resto del contrato está cerrado y es planificable: US1 puede diseñarse ya, y su gobierno no cambia
con ninguna de las tres respuestas.
