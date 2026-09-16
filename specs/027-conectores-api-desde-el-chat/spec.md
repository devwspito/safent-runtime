# Feature Specification: Conectores de API desde el chat

**Feature Directory**: `specs/027-conectores-api-desde-el-chat/` · **Created**: 2026-09-10 · **Status**: Draft
**Input**: Palabras del dueño: «Quiero que Safent, de forma nativa, tenga un módulo para consumir APIs de forma súper ágil. Que yo le pueda decir por texto en el chat normal: "necesitamos consumir esta API o este webhook, aquí están todos los datos", y lo configure todo de forma automática; y que desde ese momento pueda pedirle cosas como "conecta todo el sistema de datos de mi CRM con la API 'nombre que le puse' a la parte de ads, para saber exactamente la conversión, ROAS, ROI, rentabilidad a largo plazo". Nosotros no medimos solo en venta única: vemos como CLIENTE. Quiero venir al chat, explicarle todo y que lo configure en el entorno sin posibilidad de fallo alguno, y si falla, que lo autoarregle o lo que haga falta.»

## Resumen

Integrar un origen externo es hoy trabajo de ingeniería; el dueño quiere hacerlo **desde el chat**. El fin no es «llamar a una API»: es **medir al cliente entero** — un negocio de servicios gana en el ingreso recurrente neto, no en la primera venta.

**Riesgo existencial: el falso «listo»**. «Sin fallo alguno» se traduce en: **nada se anuncia sano sin haber funcionado de verdad, nada se degrada en silencio**.

**Actores**: Dueño (única autoridad) · Agente (configura, verifica, sincroniza, repara) · Origen externo (entrada **no confiable**) · Compañero de anuncios.

## User Scenarios & Testing *(obligatorio)*

### User Story 1 — De una frase en el chat a un conector verificado (P1)

El dueño describe un origen en el chat. El agente pregunta solo lo que falta, guarda la credencial sin repetirla, pide autorización de salida con la tarjeta, **ejecuta de verdad una operación de solo lectura** y reporta qué funciona y qué no. El conector queda en Herramientas bajo su nombre.

**Prioridad**: rebanada mínima con valor observable. **Test independiente**: describir un origen y verlo `listo` con datos reales.

1. **Given** descripción completa, **When** se envía, **Then** se crea el conector, se ejecuta una llamada real y se reporta lo probado y lo no probado, sin preguntas.
2. **Given** dominio no autorizado, **When** va a llamar, **Then** presenta la tarjeta con el dominio exacto y no llama hasta la concesión.
3. **Given** credencial sin permiso, **When** falla la prueba, **Then** queda `sin verificar`, no publica operaciones y dice qué permiso falta.
4. **Given** credencial pegada en el chat, **When** se guarda, **Then** no reaparece en chat, bitácora, panel ni modelo.

### User Story 2 — Enlazar el CRM con anuncios: cliente, no venta única (P2)

El agente propone un mapa: qué es un cliente, la primera conversión de pago, los ingresos recurrentes, las devoluciones y bajas, con importes y fechas. El dueño lo confirma **una vez** en tarjeta y lo puede editar; la sincronización corre sola, con informe por ciclo.

**Prioridad**: el motivo económico; se apoya en un conector verificado. **Test independiente**: confirmar el enlace y ver que tras un ciclo valor de cliente y ROAS cuadran con el origen.

1. **Given** conector de CRM verificado, **When** se pide el enlace, **Then** se propone el mapa con ejemplos anonimizados y nada se sincroniza sin confirmación.
2. **Given** un cliente con primera compra y tres cobros recurrentes, **When** corre el ciclo, **Then** los cuatro hechos se agrupan bajo la **misma identidad** y el valor refleja la suma.
3. **Given** devolución o baja posterior, **When** llega, **Then** resta del valor de cliente y del ROAS de su campaña sin reescribir la historia.
4. **Given** ciclo terminado, **When** se consulta, **Then** muestra leídos, enviados, rechazados, duplicados y divergencia.

### User Story 3 — Webhook entrante configurado desde el chat (P2)

El agente entrega dirección y secreto —visible una sola vez— para pegar en el origen, valida la primera entrega, guarda muestras y propone el mapeo.

**Prioridad**: cubre los orígenes que no se dejan consultar. **Test independiente**: crear el webhook, enviar una entrega y verlo `listo` con campos mapeables.

1. **Given** webhook nuevo, **When** se entrega, **Then** da dirección y secreto, avisa de que no se repite y queda `esperando primera entrega`.
2. **Given** entrega sin firma o secreto válidos, **When** llega, **Then** se rechaza, se registra y el estado no cambia.
3. **Given** primera entrega válida, **When** llega, **Then** se avisa en el chat, pasa a `listo` y se muestra la forma de la carga.

### User Story 4 — Autorreparación y salud honesta (P3)

Cada conector se vigila solo: credencial caducada, cambio de forma, límite de ritmo, fallos del origen. El agente repara dentro de límites; si no, escala con precisión.

**Prioridad**: convierte la integración en infraestructura fiable. **Test independiente**: invalidar una credencial y ver detección en un ciclo, reparación intentada y derivados incompletos.

1. **Given** credencial revocada, **When** corre el chequeo, **Then** pasa a `degradado` con causa y último dato bueno, y lo derivado se declara incompleto.
2. **Given** cambio de forma en un campo de dinero, **When** se detecta, **Then** la sincronización se detiene y nada se recalcula hasta confirmar el mapa nuevo.
3. **Given** reparación imposible sin el dueño, **When** se agota lo permitido, **Then** el mensaje dice qué falló, desde cuándo, qué se intentó y qué se necesita.

### Edge Cases

Origen sin descripción formal · autenticación con canje previo · error dentro de una respuesta correcta · subdominio fuera de lo concedido · límite de ritmo a mitad de ciclo · husos y divisas distintos · mismo cliente por dos vías · cliente borrado en el origen · entrega repetida o gigante · secreto filtrado · conector eliminado con enlace vivo.

## Functional Requirements *(obligatorio)*

**Creación**: **FR-001** crear conector desde el chat en lenguaje libre, por comprensión, no por palabras clave · **FR-002** aceptar dirección base, autenticación (clave, portador, básica, credenciales de cliente), credencial, descripción formal o ejemplos y nombre · **FR-003** preguntar solo lo que falte, agrupado · **FR-004** nombre amistoso único; renombrar conserva la historia · **FR-005** derivar operaciones de la descripción formal o, si no hay, de los ejemplos.

**Verificación**: **FR-006** nada alcanza `listo` sin una llamada real correcta de solo lectura · **FR-007** declarar lo probado y lo no probado; prohibido anunciar lo no demostrado · **FR-008** lo no verificado no publica operaciones: ausentes, no presentes-y-rotas.

**Superficie**: **FR-009** operaciones en Herramientas bajo el nombre amistoso; lista de Conectores con estado, última comprobación, dominio y autenticación · **FR-010** editar, desactivar y eliminar; eliminar revoca credencial y autorización.

**Webhook**: **FR-011** crear desde el chat con dirección y secreto mostrado una sola vez · **FR-012** rechazar entregas sin secreto o firma válidos · **FR-013** no alcanzar `listo` sin primera entrega válida · **FR-014** guardar muestras caducas · **FR-015** entregas repetidas no duplican hechos; rotar el secreto invalida el anterior.

**Enlace**: **FR-016** enlazar conector y anuncios desde el chat · **FR-017** proponer el mapa entre clientes, operaciones y cobros del origen y las conversiones del compañero · **FR-018** identidad de cliente estable que agrupa primera conversión e ingresos recurrentes · **FR-019** devoluciones y bajas restan · **FR-020** el mapa de dinero exige confirmación única y editable; cambiarlo versiona con fecha de efecto, nunca reescribe historia · **FR-021** sincronizar programado y a demanda, reanudable e idempotente, con informe de conciliación · **FR-022** el enlace solo lee.

**Salud**: **FR-023** chequeos periódicos de credencial, forma de respuesta, ritmo y errores · **FR-024** reparación acotada y registrada: espera creciente, ritmo reducido, renovación si el tipo lo permite · **FR-025** escalar con mensaje accionable lo que exija criterio del dueño · **FR-026** nada degrada en silencio: estado visible y derivados marcados incompletos.

**Gobernanza**: **FR-027** exigen tarjeta el dominio nuevo, guardar o rotar credencial y el mapa de dinero · **FR-028** lo que modifica datos externos nunca es auto-ejecutable · **FR-029** ninguna respuesta ni carga externa amplía permisos ni cambia mapas · **FR-030** bitácora solo-anexable de cada llamada y entrega, sin secretos ni datos personales · **FR-031** límite de ritmo y tope por conector.

### Non-Functional Requirements

**NFR-001** ningún secreto en chat, panel, bitácora, notificación ni contexto del modelo · **NFR-002** ningún dato personal en bruto sale de la jaula: identidades irreversibles en el borde · **NFR-003** un conector caído no afecta a los demás ni al agente · **NFR-004** toda cifra derivada reconstruible desde bitácora y mapa vigente · **NFR-005** interfaz en castellano, identificadores en inglés.

### Key Entities

**Conector**: origen consumible con nombre, dominio, autenticación, operaciones y estado (`configurando`, `esperando autorización`, `sin verificar`, `listo`, `degradado`, `suspendido`) · **Operación**: capacidad invocable; una es la prueba de vida · **Credencial**: secreto cifrado; solo viaja su referencia · **Autorización de salida**: dominio exacto, denegar por defecto · **Tarjeta**: decisión del dueño sobre seguridad o dinero · **Webhook entrante** y **muestra de carga** caduca · **Mapa de campos**: traducción versionada · **Enlace**: unión viva con mapa y ritmo · **Identidad de cliente**: identificador irreversible que agrupa los hechos económicos de una persona · **Hecho económico**: primera conversión, cobro recurrente, devolución o baja · **Valor de cliente**: contribución acumulada.

## Success Criteria *(obligatorio)*

**SC-001** de la primera frase a conector `listo`: ≤ 5 min y ≤ 3 preguntas, en 8 de 10 orígenes de prueba documentados · **SC-002** el 100 % de los `listo` tiene llamada real correcta en bitácora; cero degradaciones silenciosas · **SC-003** credencial caducada detectada y comunicada dentro de un ciclo (≤ 60 min), nunca antes por el dueño · **SC-004** valor de cliente y ROAS a largo plazo cuadran con el origen dentro de ±2 %; divergencia mayor, nombrada en el informe · **SC-005** ≥ 70 % de incidencias resueltas sin el dueño; el resto escaladas en ≤ 1 ciclo · **SC-006** cero secretos y cero datos personales fuera de la jaula · **SC-007** webhook validado sin salir del chat en ≤ 10 min · **SC-008** a 30 días, cero formularios técnicos.

## Out of Scope

Construir interfaces de CRM · escribir en el origen desde el enlace · sustituir el panel del compañero · modelos de atribución nuevos · orígenes no consultables ni empujados · compartir conectores entre instalaciones · multiusuario y roles · migración masiva de histórico.

## Assumptions

1. Único dueño aprobador, con credenciales válidas y derecho de uso. 2. Se prefiere una pieza abierta existente para derivar operaciones desde una descripción formal; solo se construye el hueco que no cubra. 3. El compañero de anuncios ya ingiere conversiones con identidades irreversibles. 4. Ciclo horario. 5. Una moneda por negocio. 6. Sin operación de solo lectura, el dueño indica una segura. 7. Muestras a 30 días. 8. Tolerancia ±2 %.

## Dependencies & Risks

**Dependencias**: almacén de secretos, autorización de salida, catálogo de herramientas y bitácora de la jaula; compañero de anuncios (US2).

**Riesgos**: «sin fallo alguno» es inalcanzable contra terceros — se sustituye por honestidad de estado (FR-026), reparación acotada (FR-024) y escalada precisa (FR-025) · un campo de dinero mal mapeado envenena toda decisión de gasto → confirmación única, versionado, conciliación · diputado confundido: respuesta externa que pide permisos → FR-029 · inyección desde cargas · dirección base hacia red interna → guarda única compartida · límites del proveedor → FR-031.

## Security / Privacy / Compliance Notes

**Sensibles**: credenciales, secretos de webhook, cargas con datos personales. **Fronteras**: lo externo no es confiable; la autoridad vive en el chat del dueño. **Cumplimiento**: minimización, identidades irreversibles en el borde, retención acotada, borrado propagado por decidir. **Para `security-engineer`**: firmas, dominio nuevo como exfiltración, inyección desde cargas, vida de la credencial.

## Preguntas abiertas

- **[NEEDS CLARIFICATION: escritura]** ¿Los conectores modifican datos en el origen, con tarjeta, o la v1 es solo lectura? *Defecto: solo lectura.*
- **[NEEDS CLARIFICATION: derecho al olvido]** Si un cliente se borra en el origen, ¿se propaga a lo derivado? *Defecto: sí, al ciclo siguiente.*
- **[NEEDS CLARIFICATION: degradado y gasto]** ¿Un CRM caído congela el presupuesto o basta marcar los datos incompletos? *Defecto: marcar y bloquear subidas.*

## ¿Listo para el siguiente paso?

**BLOCKED** — los defectos permiten empezar `/team-plan`; las dos primeras preguntas se confirman **antes de activar un enlace contra datos reales**.
