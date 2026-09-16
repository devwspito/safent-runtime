# Feature Specification: Ads como cuadro de mando propio dentro de Safent

**Feature Directory**: `specs/026-ads-en-safent-cockpit/`
**Created**: 2026-09-10
**Status**: Draft — 2 preguntas abiertas
**Input**: User description: «Ads debería ser una opción completamente individual dentro del sidebar de opciones de la app, más allá de que también aparecerá en Herramientas como MCP. Quiero que tenga un dashboard interno donde poder ver y controlar todo lo referente a las campañas: un tablero similar a un cuadro de mando de bolsa de valores, que me dé indicadores de cuándo subir presupuesto, cuándo bajar, cuándo parar campañas. Todo orientado al ROI y leads capturados.»

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Abrir Ads y leer el mercado (Priority: P1)

**Ads** es entrada propia de primer nivel en la barra lateral. Una pulsación y el propietario aterriza en el cuadro de mando: una fila por campaña o conjunto, ordenada por dinero en juego, como un tablero de cotización. En un minuto sabe qué sube, qué baja y qué parar, sin escribir direcciones ni volver a identificarse.

**Why this priority**: el motivo declarado; aporta valor solo con lectura. Sin él, Ads es una pestaña más.
**Independent Test**: abrir Ads con cuentas conectadas: las cifras cuadran con la vista de cartera y no aparece formulario de acceso.

**Acceptance Scenarios**:
1. **Given** servicio operativo y cuentas conectadas, **When** pulsa «Ads», **Then** aterriza en el cuadro de mando, con cabecera de cartera y una fila por entidad, sin segundo inicio de sesión.
2. **Given** campaña con coste por lead sobre objetivo, **When** se dibuja su fila, **Then** muestra `BAJAR` con su fuerza, las cifras de FR-005 y la acción implicada.
3. **Given** métrica sin datos suficientes, **When** se dibuja su celda, **Then** dice «sin datos», «insuficiente», «en aprendizaje» o «no controlable»; nunca un cero ni una estimación.
4. **Given** cualquier estado de FR-003 (sin instalar, arrancando, incompatible, canal sin emparejar, sin cuentas), **When** abre Ads, **Then** ve ese estado exacto y la acción que lo desbloquea, no un vacío ni un error genérico.

### User Story 2 — Actuar sobre la señal sin salir del tablero (Priority: P2)

Cada fila lleva la acción que su señal implica: `SUBIR` abre aprobación en línea; `BAJAR` y `SALIR` se aplican solos si regla y guardarraíl lo permiten y, si no, pasan a propuesta.

**Why this priority**: convierte el tablero en cuadro de *mando*; se apoya en US1 sin bloquearlo.
**Independent Test**: forzar una `SALIR` y una `SUBIR`; medir pulsaciones y comprobar registro, deshacer y guardarraíles.

**Acceptance Scenarios**:
1. **Given** señal `SUBIR` pendiente, **When** aprueba en la fila, **Then** aplica la fricción del canal, abre ventana de gracia, confirma el estado real y queda registrada.
2. **Given** señal `BAJAR` con regla automática dentro de guardarraíl, **When** corre el ciclo, **Then** la fila declara el cambio aplicado, con antes/después y deshacer.
3. **Given** propuesta ya resuelta en el canal, **When** vuelve al tablero, **Then** aparece resuelta: sin doble efecto ni segunda pregunta.
4. **Given** freno activo, **When** llega una señal accionable, **Then** nada se aplica, todo pasa a propuesta y la franja de freno queda visible.

### User Story 3 — Qué ha cambiado desde ayer y hacia dónde bajar (Priority: P3)

Franja de cambios, filtros y descenso a las vistas existentes, que siguen siendo el detalle.

**Why this priority**: acelera el juicio; el tablero sirve sin ella.
**Independent Test**: comparar dos cierres y verificar que la franja lista lo que cambió; filtrar y descender sin perder contexto.

**Acceptance Scenarios**:
1. **Given** cambios desde ayer, **When** abre el tablero, **Then** una franja lista qué cambió de señal, qué entró o salió, qué se aplicó en autónomo y qué umbrales se cruzaron.
2. **Given** quiere mirar una cuenta, plataforma o señal, **When** filtra u ordena, **Then** el tablero responde y ese estado es compartible y restaurable.
3. **Given** una fila, **When** pide detalle, **Then** llega a esa campaña, sus señales, propuestas o registro sin perder el filtro.

### Edge Cases
Dato fuera de frescura (obsoleto; sin escritura, con el motivo) · sesión de Safent caducada con el tablero abierto (se vuelve donde estaba) · el servicio cae mientras se mira (último dato marcado, nunca vaciado) · una cuenta falla y las demás no (total incompleto, declarado) · entidad en aprendizaje, con presupuesto compartido o no controlable (sin señal accionable) · sin fuente de clientes (ROI sobre venta) · propuesta que caduca a la vista · más de diez pendientes (se declara lo diferido) · cuenta suspendida · varias divisas · cero campañas.

## Functional Requirements *(mandatory)*

- **FR-001**: Ads MUST ser entrada de primer nivel en la barra lateral, no pestaña de otra sección, y MUST seguir en el catálogo de herramientas: dos superficies, un producto.
- **FR-002**: Abrir Ads MUST NO pedir un segundo inicio de sesión: la sesión del propietario en Safent basta para entrar y leer. Si no autoriza, se explica; nunca un formulario embebido.
- **FR-003**: El sistema MUST declarar cada estado de disponibilidad —sin instalar, arrancando, versión incompatible, canal sin emparejar, sin cuentas, sin datos— con la acción que lo desbloquea.
- **FR-004**: El cuadro de mando MUST ser el aterrizaje de Ads.
- **FR-005**: Cada fila MUST mostrar su entidad (campaña o conjunto), señal y fuerza, ROI, ROAS, leads, clientes y valor de cliente cuando existan, coste por lead frente a objetivo, gasto frente a tope y ritmo, tendencia, frescura y acción implicada.
- **FR-006**: El orden por defecto MUST ser dinero en juego descendente, con ordenación por cualquier columna y filtros por plataforma, cuenta y señal, en estado compartible y restaurable.
- **FR-007**: La cabecera de cartera MUST mostrar gasto de hoy y del mes frente a topes, leads de hoy y de la semana, ROI, proyección a fin de mes y freno.
- **FR-008**: El tablero MUST ofrecer la franja «qué ha cambiado desde ayer».
- **FR-009**: El sistema MUST NO fabricar cifras: sin volumen, sin ventana madura o sin palanca, declara el estado honesto en lugar de un número.
- **FR-010**: Cada señal MUST llevar una sola acción: `SUBIR` pide aprobación en línea; `BAJAR` y `SALIR` se aplican en autónomo si regla y guardarraíl lo permiten y, si no, pasan a propuesta.
- **FR-011**: La fricción MUST igualar la del canal de mensajería: evidencia en riesgo alto, confirmación tecleada en lo irreversible, ventana de gracia para deshacer.
- **FR-012**: Una decisión tomada en una superficie MUST cerrar la propuesta en la otra, sin doble efecto ni doble pregunta.
- **FR-013**: El tablero MUST NO abrir escritura nueva: dispara capacidades existentes con sus guardarraíles, topes y registro.
- **FR-014**: El freno MUST alcanzarse en dos pulsaciones, con su estado siempre visible.
- **FR-015**: Toda acción del tablero MUST registrarse con autor, entidad, antes/después, momento y superficie.
- **FR-016**: El tablero MUST descender a las vistas existentes (campaña, señales, propuestas, creatividades, reglas, registro, conexiones), que no se duplican.
- **FR-017**: El ROI MUST calcularse sobre cliente y su valor cuando esa fuente está conectada; si no, se declara sobre venta. [NEEDS CLARIFICATION: ¿qué fuente da cliente y valor, y con qué rezago aceptable?]

### Non-Functional Requirements

- **NFR-001**: Refresco dentro de la ventana de frescura, con sello visible; nunca vacía lo leído.
- **NFR-002**: De pulsar «Ads» a tablero utilizable ≤ 2 s p95; respuesta visible < 100 ms.
- **NFR-003**: Ningún secreto, credencial ni dato personal de lead; solo agregados.
- **NFR-004**: Teclado completo, foco visible, ningún estado solo por color, ticker pausable, movimiento reducible, contraste AA.
- **NFR-005**: Utilizable a 390 px: ticker, tres indicadores, cola de propuestas y freno.
- **NFR-006**: El fallo de una cuenta o plataforma no tumba el tablero; el total se declara incompleto.
- **NFR-007**: Interfaz en español, identificadores en inglés.

### Key Entities *(lenguaje ubicuo)*

- **Cuadro de mando**: hogar de Ads; el mercado de la inversión viva. Invariante: refleja la verdad del servicio, no calcula la suya.
- **Servicio de anuncios**: lo que ya observa, decide y ejecuta; fuente única de cifras, señales, propuestas y registro.
- **Fila de cotización**: una entidad con su señal, sus cifras y su acción. Si no hay dato, no hay número.
- **Señal**: `SUBIR` (subir presupuesto), `MANTENER`, `BAJAR`, `SALIR` (parar), con fuerza y causa en una frase. Solo `BAJAR` y `SALIR` se ejecutan sin permiso.
- **Dinero en juego**: gasto expuesto a la decisión en la ventana; el orden por defecto.
- **Lead** (contacto captado) ≠ **Cliente** (lead que ya compró) ≠ **Valor de cliente** (margen acumulado esperado). Se mide por cliente, no por venta.
- **ROI** (retorno en margen) ≠ **ROAS** (ingreso sobre gasto). Se muestran ambos, sin confundirlos.
- **Frescura**: antigüedad del dato; pasado el umbral, todo es lectura.
- **Freno**: interruptor que detiene toda actuación autónoma.
- **Canal de mensajería**: aviso y aprobación ya existentes. El tablero es su gemelo, no su sustituto.

## Success Criteria *(mandatory)*

- **SC-001**: De abrir Ads a resolver una `SALIR` o `BAJAR`: ≤ 3 pulsaciones y ≤ 30 s.
- **SC-002**: De 20 aperturas de Ads, ninguna pide credenciales.
- **SC-003**: Sobre la misma ventana, el tablero coincide con la vista de cartera en el 100 % de las comprobaciones.
- **SC-004**: En 30 s nombra las tres entidades con más dinero en juego y su señal, 5 sesiones de 5.
- **SC-005**: 100 % de las acciones del tablero dejan registro con antes/después.
- **SC-006**: De 50 celdas sin dato suficiente, 50 muestran el estado honesto: cero cifras fabricadas.
- **SC-007**: A 30 días, abre el panel del servicio por separado ≤ 1 vez por semana.
- **SC-008**: A 390 px, aprobar, rechazar, deshacer y freno se alcanzan sin desplazar en horizontal.

## Out of Scope
Generación de creatividades · campañas nuevas · editar reglas y guardarraíles desde el tablero · sustituir el canal · reescribir las vistas existentes · nuevas plataformas · roles · cambiar el motor de señales, la atribución o los umbrales.

## Assumptions
1. Un único propietario y aprobador.
2. El servicio ya expone cartera, señales, propuestas, ejecución y registro; esto no añade negocio.
3. `SUBIR`/`MANTENER`/`BAJAR`/`SALIR` = comprar, mantener, vender, salir en boca del propietario.
4. Ventana por defecto: 7 días con rezago declarado; cabecera, hoy y mes.
5. El tablero sustituye a la vista embebida actual; las demás quedan de detalle.
6. Filas de campaña y conjunto; anuncio y creatividad, en el descenso.
7. Sin cuentas conectadas, Ads sigue visible y explica cómo empezar, no se oculta.

## Dependencies & Risks
- **Dependency**: el servicio de anuncios (cifras, señales, propuestas) y la fuente de clientes (su valor).
- **Risk**: dos superficies aprobando lo mismo → FR-012.
- **Risk**: divergencia entre tablero y cartera → SC-003 como regresión permanente.
- **Risk**: entrar sin fricción rebaja el listón para autorizar gasto → pregunta abierta de seguridad.
- **Risk**: un tablero bonito con datos pobres destruye la confianza más rápido que no tenerlo, y traslada intacta la fatiga de aprobación → FR-009, SC-006, declarar lo diferido.

## Security / Privacy / Compliance Notes
- **Datos sensibles**: credenciales de plataformas y del canal (nunca mostradas), datos de leads (solo agregados), cifras económicas.
- **Frontera de confianza**: la autoridad de la sesión de Safent y la del servicio son hoy distintas; unirlas para leer es el requisito, extenderla al gasto es decisión de postura. [NEEDS CLARIFICATION: ¿aprobar subidas, desactivar el freno o activar autonomía exigen segundo factor fresco, como hoy, o basta la sesión?]
- **Superficie**: consumidor privilegiado, sin escritura fuera de los guardarraíles (FR-013). Traspaso a `security-engineer`: STRIDE del puente de sesión, la aprobación en línea y el cierre cruzado.
- **Cumplimiento**: RGPD por agregación; todo cambio auditable.

## Ready for next step?
BLOCKED — resolver los dos `[NEEDS CLARIFICATION]` antes de planificar. El resto está cerrado.
