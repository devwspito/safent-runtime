# Feature Specification: Mando a distancia — manejar mi Safent desde fuera

**Feature Directory**: `specs/030-remote-control-manejar-safent/`
**Created**: 2026-09-10
**Status**: Draft — 3 preguntas abiertas
**Input**: User description: «Quiero un remote-control» — aclarado por el dueño como dos cosas distintas: **(a)** manejar SU Safent desde el móvil u otro equipo suyo a través de su red privada, como el Remote Control de Claude Code (ver el chat, aprobar señales y tarjetas, mirar el cuadro de mando de Anuncios, arrancar y parar trabajos desde fuera), y **(b)** que Safent maneje otras máquinas. **Esta especificación cubre solo (a).** El sentido saliente —Safent gobernando máquinas ajenas— es `specs/031-*` y no se trata aquí.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Desde el móvil, ver el chat y decidir una tarjeta (Priority: P1)

El dueño está fuera de casa. Su agente ha llegado a un punto en el que necesita permiso: hay una tarjeta esperando. Saca el móvil, que ya pertenece a su red privada, abre Safent y ve **lo mismo que vería sentado delante del equipo**: la conversación en vivo, el estado del motor y la cola de decisiones pendientes con su contexto entero —qué quiere hacer el agente, sobre qué, por qué y qué pasa si dice que no. Aprueba o deniega con la misma fricción que en casa: un gesto si la tarjeta es simple, su código de un solo uso si la decisión es soberana. El agente sigue. Nada de esto exigió abrir un puerto al mundo, ni pegar una dirección, ni una clave compartida: el móvil entra porque es un dispositivo de la red privada del dueño **y además** está emparejado con este Safent; cualquiera de las dos cosas por separado no basta.

**Why this priority**: es el enunciado del dueño y la tajada mínima con valor propio. Hoy, si el dueño no está delante del equipo, el trabajo del agente se queda parado hasta que vuelve — el producto se convierte en un empleado que solo trabaja cuando su jefe está sentado a su lado. Con esto, y solo con esto, el dueño ya no es el cuello de botella. Todo lo demás (Anuncios, avisos, segundos dispositivos) se apoya encima.
**Independent Test**: con el equipo en casa y el móvil del dueño en la calle por datos móviles pero dentro de la red privada, provocar una tarjeta pendiente y comprobar que el dueño la ve y la decide desde el móvil, que el efecto aparece en el equipo, y que en paralelo un barrido desde internet y desde la red local de casa no alcanza ni un servicio nuevo.

**Acceptance Scenarios**:
1. **Given** el móvil emparejado y dentro de la red privada, **When** el dueño abre Safent en él, **Then** ve el chat en vivo, el estado del motor y las tarjetas pendientes, sin volver a configurar nada y sin escribir ninguna dirección.
2. **Given** una tarjeta simple pendiente, **When** el dueño la aprueba desde el móvil, **Then** el agente continúa, el equipo refleja la decisión, y el registro guarda quién decidió, desde qué dispositivo y cuándo.
3. **Given** una tarjeta soberana pendiente, **When** el dueño la aprueba desde el móvil, **Then** se le exige su código de un solo uso antes de aplicarla; sin él, la acción no ocurre.
4. **Given** un dispositivo que está en la red privada pero **no** emparejado, **When** intenta abrir Safent, **Then** no obtiene ni un dato ni una acción: ve el estado «dispositivo no autorizado» y qué hacer, y el intento queda registrado.
5. **Given** un dispositivo emparejado que ha salido de la red privada, **When** intenta usar su sesión, **Then** no alcanza nada: no hay segunda vía, ni pública ni local.
6. **Given** el mando a distancia encendido, **When** se examina el equipo desde internet y desde otra máquina de la misma red local ajena a la red privada, **Then** no hay ni un puerto, ni un servicio, ni un anuncio de descubrimiento nuevo que responda.
7. **Given** la red privada caída o la credencial de red caducada, **When** el dueño mira desde el móvil o desde el equipo, **Then** ambos declaran ese estado exacto con su acción; el equipo sigue funcionando entero y nadie cae en una vía pública ni en un reintento sin fin.

### User Story 2 — Anuncios en el bolsillo: señal, aprobación y freno (Priority: P2, la primera de su grupo)

Desde el mismo móvil el dueño entra en el cuadro de mando de Anuncios (026): ve la cartera, qué campaña sube, cuál sangra, qué propone el sistema y con qué fuerza, y aprueba o rechaza una propuesta ahí mismo. Cuando ocurre algo que merece su atención, el dispositivo le avisa; el aviso **no cuenta lo que pasa** —quien mire la pantalla bloqueada no aprende nada del negocio— y con un gesto le deja exactamente en la tarjeta o en la fila que lo motivó. Y si algo huele mal, tiene el freno de emergencia a un gesto de distancia: activarlo desde el móvil basta con la sesión; liberarlo exige prueba soberana.

**Why this priority**: es donde el mando a distancia se paga solo — el dinero corre cuando el dueño no está delante. Va la primera de su grupo porque las señales de Anuncios son lo más caro de dejar esperando. No bloquea a US1 ni la necesita para probarse en su parte de avisos y freno, pero solo tiene sentido con US1 entregada.
**Independent Test**: con cuentas conectadas y una propuesta de subida pendiente, aprobarla desde el móvil y comprobar el cambio real, la ausencia de doble efecto en el canal de señales, y que un aviso lleva de un gesto al sitio exacto. Por separado: activar el freno desde el móvil y verificar que el agente deja de ejecutar y que liberarlo pide el código.

**Acceptance Scenarios**:
1. **Given** cuentas conectadas y señales del día, **When** el dueño abre Anuncios desde el móvil, **Then** lee la cartera y las filas con sus señales, sin segundo inicio de sesión y con la frescura del dato declarada.
2. **Given** una propuesta que aumenta gasto, **When** la aprueba desde el móvil, **Then** se le aplica la fricción que su gobierno exige, el cambio se confirma contra la plataforma y queda en el registro con el dispositivo que decidió.
3. **Given** la misma propuesta ya resuelta por el canal de señales de Anuncios, **When** el dueño llega a ella desde el móvil, **Then** aparece resuelta: ni doble efecto, ni segunda pregunta.
4. **Given** un suceso que merece atención, **When** llega el aviso al dispositivo, **Then** su previsualización no contiene contenido del dueño ni cifras del negocio, y al abrirlo aterriza en la tarjeta o fila exacta, no en una pantalla de inicio.
5. **Given** el dueño quiere parar todo, **When** activa el freno desde el móvil, **Then** el agente deja de ejecutar de inmediato, el equipo lo muestra activado y el registro lo recoge.
6. **Given** el freno activado, **When** el dueño intenta liberarlo desde el móvil, **Then** se le exige prueba soberana; la vía alternativa por contraseña del equipo no se ofrece a distancia.

### User Story 3 — Un segundo dispositivo, revocar de un gesto y quedarse sin red (Priority: P3)

El dueño empareja un segundo dispositivo —su tablet, o el equipo de una persona de confianza— con derechos recortados: puede mirar y decidir lo simple, nunca lo soberano. En Seguridad ve la lista de dispositivos emparejados y de sesiones vivas, con nombre y última vez visto, y puede cortar cualquiera —o todas de una vez— desde la app nativa. Cuando un dispositivo pierde la red, no se queda con una pantalla mintiendo: dice lo que sabe, cuándo lo supo, y no deja ninguna acción a medio aplicar.

**Why this priority**: sube el listón de confianza (móvil perdido, ayuda de otra persona) pero el producto ya cumple su promesa sin ello. Se apoya en US1 y no la rompe.
**Independent Test**: emparejar un segundo dispositivo con derechos recortados, comprobar que lo soberano no se le ofrece y que un intento directo se rechaza; revocarlo desde el equipo y verificar que su siguiente gesto ya no pasa; cortar la red a mitad de una decisión y comprobar el estado honesto y la ausencia de efecto parcial.

**Acceptance Scenarios**:
1. **Given** un segundo dispositivo emparejado con derechos recortados, **When** su usuario abre Safent, **Then** ve lo que su nivel permite y las acciones soberanas ni se ofrecen ni se ejecutan si se piden por la vía que sea.
2. **Given** dispositivos emparejados y sesiones vivas, **When** el dueño abre Seguridad en el equipo, **Then** los ve listados con nombre, desde cuándo y última vez visto, y puede revocar uno o todos.
3. **Given** una sesión revocada, **When** ese dispositivo intenta cualquier gesto, **Then** no pasa —ni lectura ni acción— y su pantalla lo declara sin ambigüedad.
4. **Given** un dispositivo remoto que pierde la red a mitad de una decisión, **When** vuelve, **Then** retoma donde estaba, la decisión o se aplicó entera o no se aplicó, y nunca aparece aplicada dos veces.
5. **Given** dos dispositivos mirando la misma tarjeta, **When** ambos deciden casi a la vez, **Then** solo la primera decisión tiene efecto y el segundo ve «ya resuelta», con quién la resolvió.

### Edge Cases

**Red y credenciales**: credencial de la red privada caducada o rechazada por el servidor (se declara como tal, no como «sin conexión», y se renueva desde el equipo) · el nodo pierde la red mientras hay una sesión remota viva · el móvil salta de la red privada a una Wi-Fi ajena a mitad de gesto · el equipo se suspende o se apaga con sesiones remotas vivas · el motor se está reiniciando cuando llega el móvil · varias sesiones remotas a la vez desde el mismo dispositivo (dos pestañas, dos ventanas).

**Confianza**: móvil perdido o robado con sesión viva · dispositivo emparejado que cambia de dueño · alguien con el móvil desbloqueado en la mano · intento desde un dispositivo de la red privada nunca emparejado · reloj del dispositivo desfasado (el código de un solo uso deja de validar) · dueño sin MFA configurado que quiere hacer algo soberano desde fuera · intento de reutilizar el mismo código de un solo uso dos veces.

**Concurrencia y estado**: dos dispositivos aprobando la misma tarjeta · el dueño decide en el equipo mientras el móvil enseña la tarjeta abierta · una tarjeta que caduca mientras se mira desde el móvil · el agente cancela por su cuenta lo que estaba preguntando · el freno se activa desde el móvil mientras hay trabajo en vuelo.

**Ciclo de vida del producto**: actualización de Safent en marcha con una sesión remota abierta (el motor se cierra y vuelve — la sesión remota debe declararlo y retomar, nunca quedarse en blanco ni provocar un muro de errores) · versión del equipo más nueva que la que el dispositivo remoto tiene cargada · desinstalación o parada del motor con dispositivos emparejados · reinstalación del producto (los emparejamientos anteriores no deben resucitar solos).

## Functional Requirements *(mandatory)*

### El canal: solo por la red privada, nunca al mundo

- **FR-001**: El manejo a distancia MUST ocurrir exclusivamente a través de la red privada del dueño. El sistema MUST NO exponer ningún puerto, servicio, dirección ni túnel del producto a internet para lograrlo, ni exigir que el dueño lo haga.
- **FR-002**: Encender el manejo a distancia MUST NO encender, requerir ni reactivar ninguna vía de exposición pública existente en el producto; si alguna sigue disponible por otros motivos, su estado MUST ser visible al dueño en Seguridad.
- **FR-003**: Con el manejo a distancia encendido, un equipo de la misma red local que no pertenezca a la red privada del dueño MUST NO percibir ni alcanzar nada nuevo: ni servicio que responda, ni anuncio de descubrimiento, ni cambio observable respecto a tenerlo apagado.
- **FR-004**: El manejo a distancia MUST venir apagado y MUST encenderse y apagarse desde Seguridad, en el equipo, como decisión explícita del dueño.

### Quién entra: identidad, emparejamiento y prueba

- **FR-005**: Abrir una sesión remota MUST exigir **las dos cosas a la vez**: que el dispositivo pertenezca a la red privada del dueño y que ese dispositivo esté emparejado con este Safent. Pertenecer a la red privada por sí solo MUST NO conceder acceso.
- **FR-006**: Emparejar un dispositivo nuevo MUST ser una decisión soberana: se autoriza desde la app nativa en el equipo, o desde una sesión remota ya emparejada aportando prueba soberana, y queda registrada con nombre del dispositivo y momento.
- **FR-007**: La credencial de una sesión remota MUST ser propia de ese dispositivo —distinta de la del equipo y de la de cualquier otro dispositivo—, MUST NO ser visible, copiable ni compartible por el dueño, MUST caducar por inactividad y MUST poder revocarse en cualquier momento. Perder un dispositivo MUST NO significar perder Safent.
- **FR-008**: Toda acción soberana ejecutada desde un dispositivo remoto MUST exigir prueba de segundo factor en el momento de ejecutarla. La vía alternativa por contraseña del equipo MUST NO ofrecerse a distancia. Si el dueño no tiene segundo factor configurado, las acciones soberanas MUST NO ofrecerse en remoto y el sistema MUST decir por qué y cómo habilitarlas.
- **FR-009**: Decidir una tarjeta desde un dispositivo remoto MUST exigir exactamente la misma fricción que en el equipo para esa misma tarjeta —nunca menos—: un gesto para las simples, prueba soberana para las que la exigen.
- **FR-010**: Activar el freno de emergencia desde un dispositivo remoto MUST bastar con la sesión remota, de un gesto. Liberarlo MUST exigir prueba soberana. La asimetría es deliberada: frenar solo resta autoridad, liberar la devuelve.
- **FR-011**: Desde un dispositivo remoto MUST NO poder ampliarse el perímetro ni debilitarse el gobierno: exponer el producto públicamente, retirar el segundo factor, cambiar quién es el soberano y desemparejarse a sí mismo del gobierno quedan reservados a la app nativa en el equipo. El alcance exacto de lo prohibido lo fija la pregunta abierta 1.

### Qué se puede hacer desde fuera

- **FR-012**: Desde un dispositivo remoto el dueño MUST poder leer la conversación, seguirla en vivo mientras el agente trabaja y escribir en ella.
- **FR-013**: MUST poder ver las tarjetas pendientes con el mismo contexto que en el equipo —qué se pide, sobre qué, por qué, y qué ocurre si se deniega— y decidirlas.
- **FR-014**: MUST poder ver el estado del sistema: motor, trabajo en curso, freno, estado de la red privada y avisos sin leer.
- **FR-015**: MUST poder arrancar y detener trabajos del agente desde fuera, con el mismo gobierno que en el equipo.
- **FR-016**: MUST poder abrir el cuadro de mando de Anuncios (026) y decidir sus propuestas, sin un segundo inicio de sesión y con la frescura del dato declarada.
- **FR-017**: El dispositivo remoto MUST recibir aviso de lo que requiere al dueño. Si el aviso necesita el mecanismo de notificación del propio dispositivo, MUST viajar **sin contenido** —ni texto del chat, ni cifras, ni nombres de campaña o de cuenta—, y el contenido MUST obtenerse por la red privada al abrirlo.
- **FR-018**: Un aviso MUST llevar al dueño, de un gesto, al sitio exacto que lo motivó; MUST NO aterrizar en una pantalla de inicio ni obligarle a buscar.
- **FR-019**: Las señales de Anuncios MUST seguir entregándose por su canal actual (`safent-ads/specs/001`, ticker de Telegram). Esta entrega MUST NO duplicar ese canal: aporta el cuadro de mando y la decisión, no un segundo emisor de señales.

### La app nativa sigue mandando

- **FR-020**: El producto MUST funcionar por completo sin ningún dispositivo remoto emparejado y sin red privada. Ninguna función local MUST depender del canal remoto ni degradarse por su ausencia.
- **FR-021**: El equipo MUST ser la fuente de verdad: lo que ve el dispositivo remoto y lo que ve el equipo MUST ser el mismo estado, y una decisión tomada en cualquiera de los dos MUST reflejarse en el otro sin que el dueño recargue nada.
- **FR-022**: Una misma decisión MUST tener efecto **una sola vez**, aunque se tome desde dos sitios casi a la vez; el segundo MUST ver «ya resuelta» y quién la resolvió, nunca un doble efecto ni una segunda pregunta.
- **FR-023**: Si el dispositivo remoto se va —sin red, apagado, lejos—, el equipo MUST seguir igual: sin sesión colgada, sin trabajo bloqueado esperando a un dispositivo ausente.

### Sesiones, revocación y registro

- **FR-024**: Seguridad MUST listar los dispositivos emparejados y las sesiones remotas vivas, con nombre, desde cuándo y última vez visto.
- **FR-025**: El dueño MUST poder revocar un dispositivo o cerrar una sesión desde la app nativa, y MUST poder revocarlas **todas de un gesto**. La revocación MUST cortar el acceso de inmediato: el siguiente gesto del dispositivo revocado ya no pasa, ni para leer.
- **FR-026**: Toda acción ejecutada desde un dispositivo remoto MUST quedar en el registro inmutable atribuida al dispositivo que la ejecutó, y las tarjetas decididas a distancia MUST declararlo en su historia.
- **FR-027**: Apertura, cierre, caducidad, revocación e **intento rechazado** de sesión remota MUST quedar registrados, y un intento rechazado MUST ser visible para el dueño sin tener que bucear en el registro.

### Degradación honesta

- **FR-028**: Cuando la red privada no esté disponible —caída, nodo desconectado, dispositivo fuera—, el sistema MUST declararlo con nombre y acción en ambos extremos, MUST NO reintentar en bucle, MUST NO recurrir a ninguna vía pública y MUST NO fingir conexión ni servir estado caducado sin marcarlo.
- **FR-029**: Si la credencial de acceso a la red privada caduca o es rechazada, el sistema MUST declararlo como tal —distinguiéndolo de «sin conexión»— y ofrecer su renovación desde el equipo.
- **FR-030**: Una sesión remota interrumpida MUST poder retomarse donde estaba, y ninguna acción MUST quedar aplicada a medias por la interrupción.
- **FR-031**: Durante una actualización del producto (028), la sesión remota MUST declarar ese estado y retomar sola al terminar; MUST NO degenerar en pantalla en blanco ni en ráfaga de errores de autorización.

### Non-Functional Requirements

- **NFR-001**: Desde desbloquear el dispositivo hasta tener la tarjeta pendiente delante: ≤ 5 s p95 con el dispositivo ya en la red privada.
- **NFR-002**: El chat en vivo visto a distancia MUST ir con ≤ 2 s de retraso p95 respecto al equipo; una decisión tomada a distancia MUST verse reflejada en el equipo en ≤ 3 s p95, y al revés.
- **NFR-003**: Revocación efectiva en ≤ 10 s p95 desde que el dueño la ordena, y en todo caso antes del siguiente gesto del dispositivo revocado.
- **NFR-004**: El estado de indisponibilidad (red caída, credencial caducada, motor parado) MUST aparecer en ≤ 5 s desde que ocurre, sin que el dueño recargue.
- **NFR-005**: La superficie remota MUST ser usable en pantalla de móvil sujeto con una mano: objetivos táctiles cómodos, teclado y lector de pantalla completos, contraste AA, y las acciones destructivas o soberanas nunca al alcance de un roce accidental.
- **NFR-006**: Español en toda la superficie, identificadores en inglés.
- **NFR-007**: Encender el manejo a distancia MUST NO empeorar de forma perceptible el rendimiento del producto en el equipo.

### Key Entities *(lenguaje ubicuo)*

- **Dueño soberano**: la única persona con autoridad plena sobre este Safent. Invariante: sigue siendo uno solo; ningún dispositivo remoto crea un segundo soberano.
- **Mando a distancia**: manejar el propio Safent desde otro dispositivo del dueño. Invariante: es sentido **entrante** hacia el equipo del dueño. No confundir con el **alcance saliente** de `031` (Safent manejando otras máquinas) ni con el **espejo remoto** que ya existe en la edición de sistema (túnel público + pantalla del escritorio), que es otra cosa y otra postura de seguridad.
- **Red privada del dueño**: la red propia y cerrada a la que pertenecen su equipo y sus dispositivos (022). Invariante: es la única puerta del mando a distancia; fuera de ella no hay entrada.
- **Dispositivo remoto**: un aparato del dueño —o de alguien en quien confía— desde el que se maneja Safent. Invariante: existe para Safent solo si está **emparejado**; pertenecer a la red privada no lo convierte en dispositivo remoto.
- **Emparejamiento**: el acto soberano de admitir un dispositivo, y el vínculo que deja. Invariante: nace en el equipo (o con prueba soberana), se lista, y se deshace de un gesto.
- **Sesión remota**: un periodo de uso de un dispositivo emparejado. Invariante: tiene credencial propia, caduca por inactividad y es revocable; nunca sobrevive a la revocación de su dispositivo.
- **Tarjeta**: la petición del agente que espera un sí o un no del dueño, con su contexto. Invariante: se decide una vez, decida quien decida y desde donde decida.
- **Acción soberana**: la que solo el dueño puede autorizar aportando prueba de segundo factor. Invariante: a distancia se exige el segundo factor siempre, sin vía alternativa.
- **Freno de emergencia**: el alto total al agente. Invariante: frenar es barato y accesible; liberar es soberano.
- **Registro inmutable**: la bitácora solo-anexable de lo que ocurre. Invariante: todo lo hecho a distancia entra en él con su dispositivo.
- **Canal de señales de Anuncios**: el ticker de Telegram que ya entrega señales y aprobaciones (`safent-ads/specs/001`). Invariante: sigue siendo suyo; aquí no se replica.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: El dueño, fuera de casa, decide una tarjeta pendiente desde el móvil en ≤ 30 s desde que desbloquea el dispositivo: 10 de 10 intentos.
- **SC-002**: Con el mando a distancia encendido, un barrido desde internet y desde otra máquina de la red local ajena a la red privada encuentra **0** servicios, puertos o anuncios nuevos: 10 de 10 barridos.
- **SC-003**: Un dispositivo dentro de la red privada pero no emparejado obtiene **0** datos y **0** acciones: 20 de 20 intentos, todos registrados.
- **SC-004**: Acciones soberanas ejecutadas a distancia sin segundo factor válido: **0** de 20 intentos (incluidos reintento del mismo código y reloj desfasado).
- **SC-005**: Tras revocar, el dispositivo revocado ejecuta **0** lecturas y **0** acciones, en ≤ 10 s p95: 10 de 10.
- **SC-006**: Dos dispositivos decidiendo la misma tarjeta producen **1** efecto y **1** entrada de registro: 10 de 10; el segundo ve «ya resuelta» en las 10.
- **SC-007**: El 100 % de las acciones ejecutadas a distancia aparecen en el registro con dispositivo, momento y resultado; una muestra de 20 se reconstruye entera sin huecos.
- **SC-008**: Con la red privada caída, el equipo conserva el 100 % de su funcionalidad y ambos extremos declaran el estado con su acción en ≤ 5 s: **0** pantallas en blanco, **0** reintentos en bucle, **0** caídas a una vía pública.
- **SC-009**: De 8 fallos provocados (red caída, credencial caducada, dispositivo fuera de la red privada, sesión revocada, motor parado, actualización en curso, reloj desfasado, tarjeta caducada mientras se mira), los 8 muestran estado nombrado y acción; **0** volcados técnicos crudos.
- **SC-010**: De 10 avisos entregados, los 10 llevan al sitio exacto de un gesto y en **0** de ellos la previsualización contiene contenido del dueño o cifras del negocio.
- **SC-011**: La credencial de sesión remota no aparece en ningún artefacto observable (pantalla, historial, captura, diagnóstico exportado, registro): **0** coincidencias buscando su valor exacto.
- **SC-012**: Tras 30 días de uso normal, el dueño no ha tenido que acercarse al equipo para desbloquear ni una sola decisión del agente.
- **SC-013**: Encender el mando a distancia no cambia ninguna cifra de rendimiento del producto en el equipo por encima del 5 % respecto a tenerlo apagado.

## Out of Scope

Acceso desde internet público, túneles públicos o funnel de cualquier clase · exponer el producto a la red local · compartirlo con quien no está en la red privada del dueño · que Safent maneje otras máquinas (eso es `031`) · escritorio remoto o espejo de pantalla del compositor (vía distinta, ya existente en la edición de sistema) · app móvil publicada en tienda · instalar, actualizar o desinstalar Safent desde un dispositivo remoto (sigue siendo local, 028) · unir el móvil a la red privada (lo hace el dueño con sus medios) · varios dueños soberanos o multi-inquilino · segundo canal de señales de Anuncios · sincronización o copia en la nube · uso sin red del dispositivo remoto más allá de declarar honestamente lo que sabe y cuándo lo supo.

## Assumptions

1. La red privada del dueño ya existe y este Safent pertenece a ella (022). Esta entrega no cambia cómo se une ni quién la administra.
2. **Que el nodo pueda RECIBIR conexiones tal como está montado hoy es una pregunta de diseño, no un requisito de esta especificación**: hoy el camino es solo de salida. El requisito es «solo por la red privada y sin exponer nada»; **el mecanismo lo decide el plan** y arranca como punto de investigación de Fase 0 (ver Riesgos).
3. Sigue habiendo **un solo dueño soberano**; los derechos recortados de US3 no crean un segundo soberano.
4. La superficie a distancia en v1 es la misma interfaz del producto adaptada a pantalla pequeña, no una interfaz nueva. La promesa de 028 («ni navegador ni dirección a la vista») rige **en el equipo del dueño**; mientras no exista app propia para el móvil, se acepta la superficie del móvil siempre que ninguna dirección conceda acceso por sí sola (FR-005).
5. El dueño mantiene su segundo factor configurado y con el reloj en hora; sin él, a distancia solo hay lo no soberano (FR-008).
6. Se espera un puñado de dispositivos (≤ 5), no una flota; la gestión se diseña para ese orden de magnitud.
7. Si el aviso al dispositivo necesita el mecanismo de notificación de su sistema, ese intermediario ve **cuándo** hay algo, nunca **qué** (FR-017). Es un tercero en el camino y se declara como tal.
8. Las señales de Anuncios siguen llegando por Telegram; el dueño quiere el cuadro de mando y la decisión a distancia, no un segundo emisor.
9. El registro inmutable, las tarjetas, el freno y el segundo factor existentes se reutilizan tal cual; esta entrega añade el **dispositivo** como atributo de lo registrado, no un gobierno nuevo.

## Dependencies & Risks

- **Dependency**: `022-tailnet-connectivity` — la red privada del dueño y su estado.
- **Dependency**: `028-safent-app-nativa` — la app nativa como fuente de verdad, su credencial de arranque y su recorrido de actualización.
- **Dependency**: `026-ads-en-safent-cockpit` — el cuadro de mando que se mira desde fuera.
- **Dependency**: `safent-ads/specs/001` — el canal de señales que aquí no se duplica.
- **Dependency**: el gobierno vigente — tarjetas, segundo factor para lo soberano, freno de emergencia, registro inmutable.
- **Dependency**: el mecanismo de notificación del dispositivo del dueño, que Safent no controla.
- **Riesgo mayor**: hoy el nodo **solo sale**. Que pueda **recibir** sin tocar el aislamiento que protege al agente es la incógnita que sostiene toda la P1; si la respuesta es que no, P1 necesita otro mecanismo y el calendario cambia. Mitigación: investigarlo **antes** de diseñar nada más, con el aislamiento del agente como invariante no negociable (traspaso a `software-architect` + `security-engineer`).
- **Riesgo**: la credencial de la interfaz es hoy estable y se inyecta al abrir; llevar **esa misma** a un segundo dispositivo la convierte en una llave copiable y un móvil perdido pasa a ser Safent perdido. Mitigación: FR-007 (credencial por dispositivo, caducidad, revocación) como requisito, no como mejora.
- **Riesgo**: ya existe en el producto una vía de acceso remoto por túnel público con espejo de pantalla; si esta entrega no la retira ni la declara, el dueño creerá que «remoto = red privada» mientras hay una puerta pública abierta a un gesto de distancia. Mitigación: FR-002 y la pregunta abierta 3.
- **Riesgo**: aprobar desde el móvil abarata el acto de gobernar — decidir en dos segundos en una cola no es deliberar. Mitigación: FR-009 (misma fricción), FR-013 (contexto entero) y NFR-005 (lo soberano lejos del roce accidental).
- **Riesgo**: fatiga de avisos; si el dueño empieza a ignorarlos, el mando a distancia deja de servir para lo único que importa. Mitigación: distinguir lo que interrumpe de lo que espera, y medir SC-012.
- **Riesgo**: dos superficies decidiendo lo mismo (móvil y equipo) invitan al doble efecto. Mitigación: FR-022 y SC-006 como regresión permanente.
- **Riesgo**: la actualización del producto cierra y reabre el motor; una sesión remota viva es justo el escenario donde reaparece el muro de errores que 028 ya documenta. Mitigación: FR-031.

## Security / Privacy / Compliance Notes

- **Datos sensibles**: el contenido del chat (puede llevar datos personales y del negocio), las cifras de Anuncios, el registro inmutable, el secreto del segundo factor, la credencial de sesión remota y la credencial de acceso a la red privada.
- **Fronteras de confianza**: dispositivo remoto ↔ producto (nueva, la que abre esta entrega) · red privada como perímetro (pertenecer a ella es condición, no autorización — FR-005) · dispositivo remoto ↔ mecanismo de notificación de su sistema (tercero en el camino, sin contenido — FR-017) · equipo ↔ red local, que esta entrega debe dejar **exactamente igual** (FR-003).
- **Superficie**: un teléfono desbloqueado en manos ajenas equivale a una sesión viva; de ahí FR-007 (caducidad), FR-008 (segundo factor para lo soberano) y FR-025 (revocación de un gesto). Un dispositivo emparejado con derechos recortados es una autoridad menor que hoy no existe y hay que definirla sin agrietar el modelo de un solo soberano.
- **Traspaso STRIDE a `security-engineer`**: el emparejamiento (cómo se admite un dispositivo sin crear un secreto compartido reutilizable) · el ciclo de vida y la propagación de la revocación · las acciones soberanas a distancia y la resistencia a repetición del segundo factor · el contenido y el intermediario de los avisos · la invariante de no ampliar superficie en la red local · el efecto único de una decisión tomada desde dos sitios · la relación con la vía de acceso remoto público preexistente · el aislamiento del agente frente a cualquier camino entrante nuevo.
- **Cumplimiento**: los datos siguen viviendo en el equipo del dueño y no salen de su red privada; sin cambio de postura RGPD salvo por el intermediario de notificación, que por FR-017 solo aprende **cuándo** hay algo. Si alguna respuesta a las preguntas abiertas mete contenido en ese camino, la postura cambia y hay que revisarla.

## Preguntas abiertas para el dueño

1. [NEEDS CLARIFICATION: ver FR-011 — ¿qué queda **prohibido** hacer a distancia por mucha prueba soberana que se aporte (instalar habilidades o conectores, tocar credenciales de proveedores, subir el nivel de autonomía, desinstalar), o a distancia se puede todo lo que se puede en local con el segundo factor por delante?]
2. [NEEDS CLARIFICATION: ver US3 — ¿el segundo dispositivo es solo **otro aparato tuyo**, o entra de verdad **otra persona** con derechos recortados? Si entra otra persona: ¿qué puede ver del chat y de las cifras, y qué puede decidir sin ser soberana?]
3. [NEEDS CLARIFICATION: la vía de acceso remoto por túnel público con espejo de pantalla que ya existe en el producto — ¿se retira al entregar esto, se deja apagada y declarada en Seguridad, o convive como alternativa cuando no hay red privada? Convivir significa mantener abierta una puerta pública.]

## Ready for next step?
BLOCKED — las tres preguntas condicionan alcance de autoridad, modelo de confianza y postura de seguridad; deben resolverse en `/team-clarify`. Además, el plan MUST abrir con la investigación del supuesto 2 (¿puede el nodo recibir sin tocar el aislamiento del agente?) antes de diseñar nada: de su respuesta depende que la P1 sea entregable tal como está escrita.
