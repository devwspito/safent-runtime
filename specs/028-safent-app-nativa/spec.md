# Feature Specification: Safent es una app nativa, no un navegador

**Feature Directory**: `specs/028-safent-app-nativa/`
**Created**: 2026-09-10
**Status**: Resuelta — 5 preguntas cerradas el 10-sep-2026 (ver «Resolución de aclaraciones» al final)
**Input**: User description: «"Safent" es una app. No debe abrirme un http://localhost:18090/app/. Me instala la app nativa, y la interfaz que abre es solo la app, no un navegador.» · «la función "Actualizar" solo debe estar visible cuando realmente hay una actualización que hacer. Y debe ser efectiva: el proceso perfecto desde que el usuario pincha en actualizar, pasa todo lo que tiene que pasar y dejas al usuario con la app abierta y actualizada, aunque eso signifique que la app se cierre y vuelva a abrir»

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Descargo Safent, lo instalo y solo veo Safent (Priority: P1)

Una persona entra en el sitio, descarga Safent para su sistema, lo instala con el gesto de siempre (arrastrar, doble clic, siguiente) y lo abre desde el Dock o el lanzador. Aparece **una ventana de Safent**: nombre, icono y producto. Si al equipo le falta la base de ejecución que el motor necesita, la propia ventana lo dice con su nombre, lo que ocupa y lo que va a hacer, y ofrece la acción que lo resuelve. No hay terminal, no hay comando que copiar, no hay navegador, no hay dirección local a la vista. Cuando termina, el producto está listo en esa misma ventana.

**Why this priority**: es el enunciado del dueño y la promesa entera del producto. Sin esto, Safent es una herramienta de terminal con una web delante; con esto, es una app. Es la tajada mínima que ya entrega el resultado observable.
**Independent Test**: en un equipo limpio (macOS 15.5 arm64 sin base de ejecución y sin herramientas de desarrollo), descargar, instalar, abrir, y llegar al producto sin escribir un comando y sin que se abra una sola ventana de navegador.

**Acceptance Scenarios**:
1. **Given** un equipo con la base de ejecución ya lista y sin Safent instalado, **When** el dueño abre la app por primera vez, **Then** ve una única ventana de Safent con la preparación del motor por fases nombradas y acaba en el producto listo; ninguna ventana ni pestaña de navegador se abre en ningún momento.
2. **Given** un equipo **sin** base de ejecución, **When** abre la app, **Then** la ventana declara ese estado exacto, qué falta, cuánto ocupa, qué autorización pedirá el sistema y para qué, y ofrece **una** acción que lo resuelve — o, si la app no puede hacerse cargo en ese sistema, la instrucción exacta y la comprobación automática cuando esté resuelto; nunca una pantalla en blanco ni un error de navegador.
3. **Given** la preparación en curso, **When** el dueño mira, **Then** ve fase actual y progreso vivo, la ventana responde, y puede cancelar y reintentar sin dejar el equipo a medias.
4. **Given** la preparación falla (sin red, sin espacio, descarga bloqueada, motor que no arranca), **When** ocurre, **Then** la ventana nombra la causa en lenguaje del dueño, ofrece la acción y un diagnóstico exportable de un gesto, sin exponer el vale de arranque ni volcar jerga cruda.
5. **Given** la app ya preparada, **When** la abre otro día, **Then** llega al producto en segundos sin repetir la preparación y sin volver a pedir autorizaciones.
6. **Given** la ventana abierta en cualquier estado, **When** se inspecciona, **Then** no hay barra de direcciones, pestañas, botones de navegación, menú de navegador ni «abrir en el navegador»; el título, el icono y la entrada del lanzador son de Safent.
7. **Given** la interfaz se carga sin vale válido (recarga, sesión caducada, entrada directa), **When** ocurre, **Then** el dueño ve un solo estado de reconexión con su acción —nunca una avalancha de errores ni un reintento sin fin— y desde la app basta esa acción para volver al producto.

### User Story 2 — Actualizar de una pulsación, y acabar con la app abierta y al día (Priority: P2, la primera de su grupo)

Cuando hay versión nueva —y solo entonces— la app ofrece «Actualizar». El dueño pulsa una vez y la app hace todo lo que haya que hacer: trae la versión nueva, la verifica, sustituye el motor conservando los datos, migra el compañero si está instalado y, si hace falta cerrarse y volver a abrirse, lo hace ella sola. El recorrido acaba con la app abierta, funcionando y mostrando la versión nueva. Si algo falla por el camino, el dueño se queda con la versión anterior funcionando y con un mensaje que explica qué pasó. Cuando no hay nada que actualizar, el botón no está: no hay adorno permanente ni acción que no haga nada.

**Why this priority**: es la promesa de «app» sostenida en el tiempo; hoy actualizar exige terminal o un agente de fondo invisible. Va la primera del grupo P2 porque es lo que el dueño hará más veces después de instalar, y porque una actualización a medias destruye la confianza más rápido que no ofrecerla.
**Independent Test**: con una versión nueva publicada y la app funcionando, pulsar «Actualizar» una vez y comprobar que acaba abierta en la versión nueva, con los datos intactos, sin terminal, sin navegador y sin relanzarla a mano. Y con nada publicado: comprobar que la acción no aparece.

**Acceptance Scenarios**:
1. **Given** no hay versión nueva de ninguna pieza, **When** el dueño recorre la app, **Then** no existe acción «Actualizar» visible ni aviso pendiente en ninguna parte.
2. **Given** hay versión nueva realmente disponible, **When** el dueño abre la app o está trabajando en ella, **Then** aparece la acción con lo que trae y lo que va a tardar, y desaparece en cuanto deja de haber novedad.
3. **Given** la acción visible, **When** el dueño pulsa una vez y confirma, **Then** no se le pide nada más: la app trae, verifica, sustituye el motor, conserva los datos y migra el compañero, mostrando fases nombradas y progreso vivo.
4. **Given** la actualización requiere reiniciar la app, **When** llega ese paso, **Then** la app se cierra y se vuelve a abrir sola, avisando antes, y el recorrido acaba con la ventana abierta en el producto, no en un escritorio vacío.
5. **Given** la actualización terminó bien, **When** el dueño mira la versión, **Then** es la nueva, y coincide entre envoltorio, motor y compañero.
6. **Given** cualquier paso falla (sin red, sin espacio, paquete no verificable, motor nuevo que no arranca, migración del compañero fallida), **When** ocurre, **Then** el dueño se queda con la versión anterior funcionando, con un mensaje que dice qué falló y qué hacer, y con la acción disponible para reintentar; nunca a medias, nunca sin producto.
7. **Given** todo el recorrido, **When** se audita, **Then** no hubo un solo paso de terminal ni una sola ventana de navegador ni una dirección local a la vista.

### User Story 3 — Vivo dentro de la app: reiniciar, Anuncios, desinstalar (Priority: P2)

Lo demás que hoy exige el terminal ocurre también dentro de la ventana: levantar el motor cuando está parado, abrir Anuncios como una sección más de la barra lateral (026) y desinstalar Safent del equipo. El dueño ve el progreso, la app vuelve sola al producto y sus datos siguen ahí.

**Why this priority**: sostiene el uso diario y cierra el ciclo de vida, pero cada operación es rara comparada con abrir y actualizar. Se apoya en US1 sin bloquearla y no depende de US2.
**Independent Test**: con la app ya funcionando, provocar el motor parado, abrir Anuncios y desinstalar, midiendo pasos de terminal (deben ser cero) y ventanas de navegador (deben ser cero).

**Acceptance Scenarios**:
1. **Given** el motor está parado (equipo reiniciado, suspensión larga, parada manual), **When** el dueño abre la app, **Then** ésta lo levanta sola con progreso visible, sin pedir nada.
2. **Given** el compañero de Anuncios disponible, **When** el dueño pulsa «Anuncios», **Then** aparece dentro de la misma ventana como sección propia, con su preparación y sus estados declarados (026); nunca una ventana aparte ni una pestaña.
3. **Given** el dueño quiere irse, **When** desinstala desde la app y confirma de forma explícita, **Then** la app, el motor y los datos se eliminan; lo que era del equipo y sirve para otras cosas (la base de ejecución compartida) se queda, y la ventana dice exactamente qué se borró y qué no.
4. **Given** una instalación previa hecha por el camino de terminal, **When** el dueño instala y abre la app, **Then** ésta adopta ese motor y esos datos; no crea una segunda instalación ni pide migración manual.

### User Story 4 — Lo mismo en Windows (Priority: P3)

Una persona en Windows descarga Safent, lo instala con el instalador de siempre, lo abre y ve la misma ventana única con el mismo resultado. La preparación del entorno que Windows exige la conduce la app, declarando cada autorización y cada reinicio necesario antes de pedirlo.

**Why this priority**: hoy Windows no está servido de verdad; es crecimiento de mercado, no la promesa mínima. US1, US2 y US3 son válidas y demostrables sin él.
**Independent Test**: máquina Windows limpia: descargar, instalar, abrir, llegar al producto sin terminal, sin comandos pegados y sin navegador.

**Acceptance Scenarios**:
1. **Given** una máquina Windows sin preparar, **When** abre la app, **Then** ve los estados nombrados de US1 escenario 2, incluida la advertencia de reinicio si el sistema lo exige, y retoma donde estaba tras reiniciar.
2. **Given** el instalador firmado, **When** lo ejecuta, **Then** el sistema no muestra avisos de origen desconocido ni exige saltarse protecciones.
3. **Given** la app funcionando en Windows, **When** recorre US1, US2 y US3, **Then** el resultado observable es idéntico al de los demás sistemas: una ventana, cero terminal, cero navegador.

### Edge Cases

**Instalación y arranque**: equipo sin base de ejecución · base presente pero no apta para el motor (modo insuficiente, versión vieja, kernel sin lo que la jaula exige) · el dueño no tiene permisos de administrador · puerto local ya ocupado por otra cosa (se elige otro sin preguntar y sin enseñarlo) · descarga del motor bloqueada por red corporativa, proxy o registro caído · disco casi lleno antes o durante la descarga · segunda apertura de la app (una sola ventana, se enfoca la existente) · dos sesiones de usuario en el mismo equipo · Mac Intel, no servido: se declara antes de descargar, no a mitad de instalar · antivirus o control corporativo bloqueando la instalación · reloj del equipo desincronizado (firma y transporte fallan con un error incomprensible) · red que se cae a mitad de la preparación · el equipo se suspende durante la descarga · el dueño cierra la ventana durante la preparación · carga de la interfaz sin vale válido: recarga, sesión caducada, vuelta desde suspensión o entrada directa (hoy, en vivo, degenera en una ráfaga de peticiones rechazadas y un reintento sin fin en lugar de una pantalla) · instalación previa hecha por terminal, con nombre o puerto distintos.

**Actualización**: pulsar «Actualizar» con trabajo del motor en curso · sin red al pulsar, o red que se corta a mitad · disco lleno durante la descarga de la versión nueva (versiones viejas ocupando el sitio) · migración del compañero que falla con el motor ya sustituido · el motor nuevo no arranca · paquete o imagen que no verifican · el dueño cierra la ventana a mitad · el sistema impide el reinicio automático de la app · versión nueva publicada mientras la actualización corre · envoltorio y motor con versiones que no casan tras un intento fallido · dos actualizaciones lanzadas a la vez.

**Ciclo de vida**: desinstalar con trabajo del motor en curso · app más nueva que el motor instalado, o al revés · desinstalar tras una actualización fallida.

## Functional Requirements *(mandatory)*

### La app y su ventana

- **FR-001**: La app MUST NO abrir ninguna ventana ni pestaña de navegador del sistema en ningún recorrido: primera instalación, arranque, preparación, error, actualización, reinicio, compañeros, ayuda y desinstalación incluidos.
- **FR-002**: Ninguna dirección local del motor —equipo, puerto, ruta o vale— MUST ser visible, seleccionable, copiable ni pegable desde la interfaz, y la ventana MUST NO ofrecer barra de direcciones, pestañas, navegación ni recarga de navegador.
- **FR-003**: Safent MUST tener identidad de aplicación en el sistema: nombre, icono, entrada en el lanzador y una sola ventana; abrirla de nuevo enfoca la ventana existente en lugar de crear otra.
- **FR-004**: Instalar Safent MUST ser el gesto estándar del sistema (descargar, instalar, abrir), sin terminal, sin comandos que copiar y sin exigir privilegios de administrador para el gesto de instalar.
- **FR-005**: Los paquetes de instalación MUST distribuirse firmados y notarizados para cada sistema servido, de modo que instalar y abrir no exija al dueño saltarse ninguna protección del sistema.

### Preparación del motor

- **FR-006**: El primer arranque MUST preparar el motor por sí mismo, con fases nombradas y progreso real; queda prohibido el progreso ficticio y cualquier espera sin estado visible.
- **FR-007**: Cada estado de indisponibilidad MUST declararse con nombre propio y acción que lo desbloquea: falta base de ejecución, base no apta, sin autorización, sin espacio, sin red, descarga bloqueada, puerto ocupado, motor caído, versión incompatible, sistema no servido.
- **FR-008**: La app MUST comprobar los requisitos del equipo (espacio, memoria, sistema servido) antes de empezar a descargar nada y declarar el resultado.
- **FR-009**: La app MUST resolver por sí misma un conflicto de puerto local eligiendo otro, sin preguntar y sin exponerlo.
- **FR-010**: Cuando la app no pueda hacerse cargo de un requisito en ese sistema, MUST decir exactamente qué hacer y comprobar sola cuando esté resuelto, retomando sin reinstalar ni perder lo hecho.
- **FR-011**: El vale de arranque MUST permanecer dentro de la app: nunca en el portapapeles, en el historial del sistema, en mensajes de error, en el diagnóstico exportado, en registros ni entregado a ningún programa externo; se renueva en cada arranque del motor.
- **FR-012**: Una carga de la interfaz sin vale válido —reapertura, recarga, sesión caducada o entrada directa— MUST resolverse en **un único estado honesto** de reconexión, con su acción, y MUST NO disparar ráfagas de peticiones fallidas ni reintentos en bucle: el dueño ve una pantalla, no un muro de errores.
- **FR-013**: La app MUST llevar siempre su ventana a una entrada con vale válido, de modo que el estado de FR-012 no se alcance nunca por su culpa; ese estado existe como red de seguridad, no como recorrido normal.
- **FR-014**: La app MUST declarar cada autorización que el sistema vaya a pedir —administrador, red, carpetas, notificaciones, reinicio— antes de pedirla y diciendo para qué sirve.

### Actualización

- **FR-015**: La acción «Actualizar» MUST aparecer solo cuando existe versión nueva realmente disponible de alguna pieza (envoltorio, motor o compañero) y MUST desaparecer cuando no la hay: nunca un botón permanente, un aviso decorativo ni una acción que no haga nada.
- **FR-016**: Una sola pulsación, con una única confirmación, MUST desencadenar la actualización completa: traer la versión nueva, sustituir el motor y migrar el compañero si está instalado. El dueño no encadena pasos ni elige piezas.
- **FR-017**: La actualización MUST verificar procedencia e integridad de cada pieza antes de aplicarla y MUST mostrar fases nombradas con progreso vivo mientras dura.
- **FR-018**: La actualización MUST conservar íntegros los datos del dueño (configuración, identidad, memoria, habilidades, estado del compañero).
- **FR-019**: La actualización MUST terminar con la app abierta en el producto y en la versión nueva; si hace falta cerrarla y volver a abrirla, la app MUST hacerlo ella sola, avisando antes, y volver donde estaba. El dueño no la relanza a mano.
- **FR-020**: Ante el fallo de cualquier paso, el sistema MUST dejar la versión anterior funcionando, declarar qué falló y qué hacer, y permitir reintentar; nunca una instalación a medias ni un equipo sin producto.
- **FR-021**: La versión que la app muestra tras actualizar MUST ser la nueva, y envoltorio, motor y compañero MUST coincidir o declararse la discrepancia con su acción.
- **FR-022**: El recorrido de actualización MUST ocurrir entero dentro de la ventana: sin terminal, sin navegador, sin proceso de fondo que el dueño tenga que conocer o reparar.
- **FR-023**: Al pulsar «Actualizar» con trabajo del motor en curso, el sistema MUST declarar ese trabajo y comportarse de forma predecible antes de tocar nada. [NEEDS CLARIFICATION: ¿esperar a que el trabajo en curso acabe —con espera acotada y aviso—, o advertir y cortar bajo confirmación del dueño?]

### Ciclo de vida

- **FR-024**: Cerrar y reabrir la app MUST conservar todos los datos y MUST NO repetir la preparación ya hecha.
- **FR-025**: Reiniciar el motor y desinstalar MUST poder hacerse íntegramente desde la ventana, sin terminal.
- **FR-026**: Los compañeros del producto, empezando por Anuncios (026), MUST servirse dentro de la misma ventana como sección propia, con sus estados declarados; nunca en ventana aparte ni en navegador.
- **FR-027**: Desinstalar MUST exigir confirmación explícita, declarar qué se borra (app, motor, datos) y qué se conserva, y MUST NO eliminar herramientas del equipo que el dueño use para otras cosas.
- **FR-028**: La app MUST adoptar una instalación previa del motor y sus datos en el mismo equipo, sin duplicarla y sin pedir migración manual.
- **FR-029**: La app MUST ofrecer, de un gesto, un diagnóstico exportable útil para soporte y libre de secretos.
- **FR-030**: Al cerrar la ventana, el motor MUST comportarse de forma declarada y visible al dueño. [NEEDS CLARIFICATION: ¿cerrar la ventana detiene el motor —y con él cualquier trabajo en curso— o lo deja vivo en segundo plano con indicador en la barra de estado y salida explícita?]

### Non-Functional Requirements

- **NFR-001**: De doble clic a producto listo en equipo ya preparado: ≤ 10 s p95. Primera vez con red doméstica típica: ≤ 20 min p95, con el estado cambiando al menos cada 5 s.
- **NFR-002**: Actualización completa con red doméstica típica: ≤ 15 min p95, con el estado cambiando al menos cada 5 s y sin ninguna ventana de tiempo en la que el dueño no sepa qué está pasando.
- **NFR-003**: La ventana nunca se congela durante la preparación ni durante la actualización: siempre responde y siempre se puede cancelar antes del punto de no retorno, que se declara.
- **NFR-004**: Vocabulario del dueño en toda la superficie; la jerga de infraestructura vive solo en el diagnóstico exportable. Un dueño que no sabe qué es un contenedor debe poder completar US1 y US2.
- **NFR-005**: Teclado completo, foco visible, contraste AA y estados anunciables por lector de pantalla, incluidas las pantallas de preparación y de actualización.
- **NFR-006**: Interfaz en español, identificadores en inglés.
- **NFR-007**: La app declara los requisitos mínimos de equipo en el sitio de descarga, antes de descargar.

### Key Entities *(lenguaje ubicuo)*

- **App (Safent)**: lo que el dueño descarga, instala, abre, actualiza y desinstala. Tiene nombre, icono y una ventana. Invariante: es lo único que el dueño nombra.
- **Ventana**: la única superficie del producto. Invariante: no parece ni se comporta como un navegador; no hay dirección, ni pestañas, ni navegación.
- **Motor**: el servicio local enjaulado que ejecuta el producto y custodia los datos. Invariante: el dueño nunca lo arranca, lo para, lo sustituye ni lo nombra a mano; la app se ocupa.
- **Base de ejecución**: la pieza del sistema que el motor necesita para correr y que hoy no viene con la app. Invariante: es del equipo, no de Safent; se comparte con otras herramientas y no se destruye al desinstalar.
- **Preparación**: el trabajo entre abrir la app por primera vez y el producto listo. Invariante: siempre tiene fase, progreso y salida honesta.
- **Actualización**: el paso de la versión instalada a la publicada, en todas sus piezas a la vez. Invariante: o termina en la versión nueva con la app abierta, o deja la anterior funcionando. No hay tercer resultado.
- **Versión de Safent**: lo que el dueño lee y compara; una sola aunque por dentro sean piezas. Invariante: nunca se muestra una versión que no sea la que está corriendo.
- **Vale de arranque**: la credencial de un arranque que autoriza la ventana ante el motor. Invariante: nace y muere dentro de la app.
- **Compañero**: capacidad adicional servida dentro de la misma ventana (Anuncios, 026). Invariante: sección, nunca ventana; se actualiza y migra con el resto.
- **Camino de terminal**: la vía de instalación y control por comandos que existe hoy. Invariante: sigue disponible para operadores, deja de ser condición para usar el producto.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Una persona sin conocimientos técnicos, en un equipo limpio y sin base de ejecución, pasa de la descarga al producto listo sin ayuda y sin escribir un solo comando: 5 de 5 intentos.
- **SC-002**: En 20 recorridos completos (primer arranque, reapertura, actualización, motor parado, Anuncios, desinstalación) se abren **0** ventanas o pestañas de navegador.
- **SC-003**: En esos mismos 20 recorridos hay **0** pasos de terminal y **0** direcciones locales visibles o copiables.
- **SC-004**: Descarga → listo ≤ 20 min p95 la primera vez (base de ejecución incluida) y ≤ 10 s p95 en reaperturas posteriores.
- **SC-005**: De 10 fallos provocados en la preparación (sin base, base no apta, sin espacio, puerto ocupado, descarga bloqueada, sin red, motor caído, sin permisos, reloj desfasado, sistema no servido), 10 muestran estado nombrado y acción; 0 pantallas en blanco y 0 volcados técnicos crudos.
- **SC-006**: Con todo al día, la acción «Actualizar» no aparece en ninguna pantalla: 10 comprobaciones, 0 apariciones. Con versión nueva publicada, aparece en las 10.
- **SC-007**: 10 actualizaciones lanzadas con una sola pulsación acaban con la app abierta, funcionando y mostrando la versión nueva, con los datos intactos: 10 de 10, y 0 relanzamientos manuales.
- **SC-008**: De 5 fallos provocados durante la actualización (sin red, disco lleno, migración del compañero fallida, corte a mitad, pieza no verificable), 5 dejan la versión anterior funcionando con mensaje claro; 0 instalaciones a medias.
- **SC-009**: El vale de arranque no aparece en ningún artefacto observable (portapapeles, diagnóstico exportado, registros, mensajes): 0 coincidencias buscando su valor exacto.
- **SC-010**: Abrir la app dos veces deja una sola ventana enfocada: 10 de 10.
- **SC-011**: 10 instalaciones sobre 10 equipos servidos no muestran ni un aviso de origen desconocido del sistema.
- **SC-012**: En todas las vías de entrada (app, reapertura, recarga, sesión caducada, entrada directa) el dueño ve **0** ráfagas de errores de autorización: como mucho un estado de reconexión, y **0** reintentos en bucle.
- **SC-013**: Tras 30 días de uso normal, incluidas al menos dos actualizaciones, el dueño no ha necesitado el camino de terminal ninguna vez.

## Out of Scope
Cambiar el motor o su jaula (sigue siendo el servicio local enjaulado de hoy) · rediseñar la interfaz del producto (026 y 027 van por su cuenta) · versión web hospedada o móvil · varios dueños en el mismo equipo · sincronización o copia en la nube · actualizaciones parciales elegidas por el dueño (pieza a pieza) · volver a una versión anterior a voluntad una vez la actualización ha ido bien · sustituir el camino de terminal para operadores · Mac Intel · cambiar el emparejamiento empresarial · construir desde cero una cadena de firma nueva (se reutiliza la existente).

## Assumptions
1. El motor sigue siendo el mismo servicio local enjaulado; esta entrega cambia cómo se instala, se abre, se actualiza y se presenta, no lo que hace.
2. Un dueño por equipo y una instalación por sesión de usuario.
3. Sistemas servidos hoy: macOS Apple Silicon y Linux de escritorio; Windows entra según la pregunta abierta 3; Mac Intel no.
4. La base de ejecución es pesada (máquina virtual en macOS y Windows) y su instalación puede exigir una autorización de administrador una sola vez.
5. El dueño acepta esperar minutos la primera vez y en cada actualización si el progreso es honesto; no acepta un terminal ni un navegador.
6. Envoltorio, motor y compañero se presentan al dueño como **una sola versión de Safent**; por dentro son piezas, por fuera una acción y un número.
7. El dueño autoriza expresamente que la app se cierre y se vuelva a abrir sola durante la actualización; el requisito es acabar abierta, no evitar el reinicio.
8. La firma y notarización se apoyan en la cadena que ya existe en otra tubería del grupo; aquí se reutiliza.
9. La ventana muestra el mismo producto que hoy; no se bifurca la interfaz por tener envoltorio nativo.
10. Las instalaciones previas hechas por terminal son pocas y conocidas; adoptarlas es requisito (FR-028), migrar datos entre equipos no lo es.

## Dependencies & Risks
- **Dependency**: la base de ejecución de contenedores del equipo, que la app no controla y que el sistema del dueño puede bloquear.
- **Dependency**: la cadena de firma y notarización (Apple, Windows, Linux) que hoy vive en otra tubería y este producto aún no usa.
- **Dependency**: el canal desde el que se anuncian y se traen las versiones nuevas del envoltorio, del motor y del compañero.
- **Risk**: prometer «app» mientras el motor exige una máquina virtual con varios gigas de disco y memoria; en equipos modestos la promesa se rompe. Mitigación: FR-008 y NFR-007, declarar requisitos antes de descargar.
- **Risk**: si la app instala la base de ejecución, nos hacemos responsables de mantenerla y de no romper otras herramientas del dueño. Mitigación: pregunta abierta 1 y no tocar una base existente que ya funcione.
- **Risk**: la ventana nativa muestra la misma página; basta un gesto de navegador que se escape (menú contextual, atajo, enlace externo) para romper la promesa en un clic. Mitigación: FR-001 y FR-002 como regresión permanente en cada entrega.
- **Risk**: la actualización toca a la vez envoltorio, motor y compañero con datos que migrar; el punto de fallo natural es «motor nuevo con datos viejos» y ahí es donde se pierde la confianza del dueño. Mitigación: FR-018, FR-020 y SC-008 como regresión permanente.
- **Risk**: ocultar el botón cuando no hay novedad exige saber de verdad si la hay; una comprobación que miente deja al dueño sin actualizar o le ofrece una actualización vacía. Mitigación: FR-015 con SC-006 en ambos sentidos.
- **Risk**: la interfaz también se alcanza fuera de la app (camino de terminal, reapertura, recarga); si el estado honesto de FR-012 vive solo en el envoltorio, el muro de errores observado hoy reaparece en cuanto alguien entra por otra puerta. Mitigación: FR-012 en la interfaz servida, FR-013 en la app, SC-012 como regresión permanente.
- **Risk**: dos caminos de instalación conviviendo dejan al dueño con dos motores y datos partidos. Mitigación: FR-028.
- **Risk**: una actualización de un solo clic sobre un motor con privilegios es un canal directo a la máquina del dueño si no se verifica procedencia. Mitigación: FR-017 y traspaso a `security-engineer`.
- **Risk**: elegir tienda como canal puede chocar con una app que instala un motor local y que se actualiza sola; decidirlo tarde obliga a rehacer la actualización entera. Mitigación: pregunta abierta 4 antes de planificar.

## Security / Privacy / Compliance Notes

- **Datos sensibles**: el vale de arranque (autoridad plena sobre el motor), las credenciales de proveedores y compañeros que viven en los datos del motor, y el diagnóstico exportable (debe nacer sin secretos).
- **Fronteras de confianza**: ventana ↔ motor local (autoridad plena, hoy sostenida por el vale) · app ↔ internet (paquete de instalación, anuncio y descarga de versiones nuevas, imagen del motor, instalador de terceros para la base de ejecución) · app ↔ sistema operativo (elevación a administrador durante la preparación y, si procede, durante la actualización).
- **Superficie**: cualquiera con acceso físico al equipo abre la app y opera el motor sin credencial propia; convertir el producto en app lo hace más alcanzable que un comando. Decidir si la app exige desbloqueo propio es postura de seguridad, no detalle: traspaso a `security-engineer`.
- **Traspaso STRIDE a `security-engineer`**: cadena de suministro de la actualización de un clic (anuncio de versión, descarga, verificación, sustitución del motor, migración del compañero) · elevación durante la instalación de la base de ejecución · custodia y ciclo de vida del vale de arranque · superficie del envoltorio nativo (navegación a orígenes externos, puentes con el sistema, portapapeles) · borrado real en la desinstalación.
- **Cumplimiento**: notarización y firma obligatorias por sistema; distribuir software de terceros con privilegios acarrea licencia y responsabilidad; los datos siguen siendo locales, sin cambio en la postura RGPD.

## Preguntas abiertas para el dueño

1. [NEEDS CLARIFICATION: ¿puede la app instalar por su cuenta la base de ejecución con una sola autorización de administrador, o debe traerla dentro del propio paquete —más peso y más responsabilidad de mantenimiento—, o se queda en instrucción guiada?]
2. [NEEDS CLARIFICATION: ver FR-030 — ¿cerrar la ventana detiene el motor o lo deja vivo en segundo plano con indicador y salida explícita?]
3. [NEEDS CLARIFICATION: ¿Windows entra en esta entrega como P3, o se declara no servido hasta la siguiente y se retira del sitio de descarga?]
4. [NEEDS CLARIFICATION: canal de distribución — ¿descarga directa firmada desde el sitio, o tiendas (App Store / Microsoft Store) con sus reglas sobre software que instala un motor local y se actualiza solo?]
5. [NEEDS CLARIFICATION: ver FR-023 — al actualizar con trabajo del motor en curso, ¿esperar a que acabe con espera acotada, o advertir y cortar bajo confirmación?]

## Ready for next step?
READY — las cinco preguntas quedaron resueltas el 10-sep-2026 (§«Resolución de aclaraciones»). Plan, contratos y tareas viven en `plan.md`, `contracts/` y `tasks.md` de este mismo directorio.

---

## Resolución de aclaraciones (10-sep-2026)

Decisiones del dueño y del coordinador. **Vinculantes.** El detalle y las
alternativas rechazadas viven en `research.md`; el diseño, en `plan.md`.

### Principio rector que gobierna toda la entrega

> «Que el sistema funcione "bien" pero el usuario hizo algo mal es lo mismo que
> "la app no sirve". Por eso debe funcionar como Codex app o Claude Code app.»

- **FR-031**: **NO DEBE existir ningún paso del usuario que pueda salir mal**: cero
  comandos, cero elecciones sobre bases de ejecución, puertos, máquinas o versiones,
  y nada que pegar. Los únicos avisos admitidos son los **obligatorios del sistema
  operativo**: Gatekeeper en macOS (que la notarización elimina) y el prompt de
  privilegio del gestor de paquetes en Linux.
- **FR-032**: Todo estado del equipo DEBE **auto-sanarse sin preguntar**: base de
  ejecución o máquina preexistentes (rootless, de otro tamaño, de otra versión) se
  adoptan o se ignoran sin tocarlas; puerto ocupado ⇒ se elige otro y la app sigue;
  contenedor o compañero a medio aprovisionar ⇒ se reconcilia; descarga interrumpida
  ⇒ se reanuda; imagen envejecida ⇒ se re-baja verificada; segunda apertura ⇒ se
  enfoca la ventana existente.
- **FR-033**: Lo que la app no pueda reparar DEBE mostrarse en **una sola pantalla
  honesta** con la causa en lenguaje del dueño y **un** «Reintentar». **Queda
  prohibida toda instrucción de terminal en la interfaz.**
- **FR-034**: La app DEBE traer dentro su propia base de ejecución (motor de
  contenedores fijado y, en macOS, la imagen de máquina). **No** puede depender de
  lo que el dueño tenga instalado, de su PATH, de su gestor de paquetes ni de
  versiones ajenas.
- **FR-035**: Los paquetes de macOS DEBEN distribuirse **notarizados y grapados**.
  Un aviso de origen desconocido es un fallo de usuario, y por tanto un fallo del
  producto. *(El acuerdo del Programa de Desarrolladores de Apple está aceptado
  desde el 10-sep-2026: la notarización pasa de «mejor esfuerzo» a requisito.)*
- **SC-014**: Una persona que **nunca ha oído la palabra «contenedor»** instala y usa
  Safent y Anuncios **sin leer nada**: 5 de 5 intentos.
- **SC-015**: En 20 recorridos completos, número de preguntas al dueño que **no** sean
  autorizaciones obligatorias del sistema operativo: **0**.
- **SC-016**: De 12 estados adversos provocados (máquina preexistente rootless ·
  máquina de otro tamaño · máquina ajena en uso · puerto ocupado · contenedor a
  medias · compañero a medias · descarga cortada · imagen envejecida · estado local
  ausente · estado local corrupto · sin espacio · espacios de nombres bloqueados),
  **12** se resuelven solos o terminan en **una** pantalla con «Reintentar»; **0**
  piden algo al dueño y **0** muestran un comando.

### Preguntas abiertas — resueltas

1. **¿Base de ejecución instalada, empaquetada o guiada?** → **Empaquetada.** La app
   trae el motor de contenedores fijado y, en macOS, la imagen de máquina; crea la
   máquina **sin red** y con el tamaño correcto. Las imágenes de Safent y del
   compañero **no** se empaquetan (2,50 GB comprimidos > el límite de 2 GiB por
   fichero de GitHub Releases): se traen **por digest**, con reintentos, reanudación
   por capa y progreso honesto. Instrucción guiada: **descartada** (es el paso de
   usuario prohibido).
2. **¿Cerrar la ventana detiene el motor?** → **No.** El motor sigue vivo, con
   elemento en la barra de menús / bandeja que declara el estado y ofrece «Abrir»,
   «Reiniciar el motor» y **«Salir»** explícito. Salir sí lo detiene de forma
   ordenada. Resuelve FR-030.
3. **¿Windows entra?** → **No en esta entrega.** Mac y Linux ahora; Windows después.
   El sitio de descarga lo declara «aún no servido». La US4 se difiere.
4. **¿Canal de distribución?** → **Descarga directa firmada** desde el sitio, con
   actualizador propio de manifiestos firmados. **Tiendas descartadas**: sus reglas
   son incompatibles con una app que instala y gobierna un motor local y se
   autoactualiza.
5. **¿Actualizar con trabajo del motor en curso?** → **Ni preguntar ni cortar.** La
   app declara el trabajo, pausa la cola, espera de forma acotada (≤ 10 min con
   progreso) y, si excede, avisa y **re-encola** el ítem para después del reinicio.
   Resuelve FR-023 sin introducir una decisión del usuario.

### Alcance confirmado

- El motor de instalación es el **CLI que ya existe**, embebido en la app y dirigido
  por ella (una sola implementación de la jaula). El camino de terminal sobrevive
  para operadores.
- El control remoto queda **fuera** de esta entrega (specs 030/031).
- Anuncios viene instalado y se cubre en la spec **029**.
