# Safent — revisión integral en curso

## Criterio del propietario

Rediseño profundo, posiblemente total, de la experiencia desktop. Referencia de
calidad: Codex desktop, incluidos chat, herramientas, elevación de permisos,
aprobaciones y recuperación de errores. No se considera suficiente un cambio de
colores, animaciones o radios. Guía principal: [Emil Kowalski](https://emilkowal.ski/skill).

El backend también está en alcance: simplificar complejidad demostrablemente
innecesaria, retirar código muerto confirmado y conservar código correcto.
No eliminar aislamiento, auditoría, MFA ni controles de aprobación por comodidad.

## Producto esperado

Hermes observa y propone autónomamente. El propietario aprueba, rechaza o edita y
aprueba desde Safent Ads. La ejecución debe corresponder exactamente a la propuesta
aprobada; cambiar la propuesta invalida la autorización previa. Google y Meta se
integran con herramientas oficiales donde cubran el caso y API/SDK oficiales para
el resto, siempre pasando por la jaula. No prometer cobertura total sin verificarla.

## Primera entrega técnica (no es el rediseño completo)

| Before | After | Why |
| --- | --- | --- |
| Tarjeta de permiso extensa con estados y estilos duplicados | Componente acotado con alcance, herramienta, parámetros completos y decisión única | Entender exactamente qué se autoriza |
| Solo los primeros ocho parámetros, con texto recortado | Todos los parámetros redacted disponibles, multilínea y desplegables | No ocultar parte de una operación |
| Aprobación presentada como ejecución terminada | Confirmación de decisión; resultado de herramienta separado | Evitar falsos éxitos |
| Reintento MFA que omite el código | Reintento vuelve a solicitar verificación | Conservar el flujo de seguridad |
| Un error de MFA renueva la sesión y reenvía la autorización | Errores de factor separados de sesión; sin reenvío automático | No repetir decisiones ni alterar una sesión válida |
| API acepta `always` pero solo consume una propuesta | Solo `once` y `deny` | No anunciar permisos permanentes inexistentes |
| Plazos de 11 y 31 minutos inventados por distintas vistas | Estado pendiente determinado por el gate | Una fuente de verdad |
| Consultas de políticas para un prop sin uso | Prop y consultas retirados | El servidor clasifica MFA por operación |
| Respuesta tardía de otra conversación puede aparecer en el chat actual | Filtrado por conversación en render, consultas sin solapamiento | Evitar aprobaciones fuera de contexto |
| Pestañas sin navegación con flechas | Foco con flechas/Home/End y activación explícita | Navegación accesible sin consultas por cada flecha |
| Filas de proveedores como tarjetas separadas a todo el ancho | Listas agrupadas y ancho de contenido acotado | Densidad y lectura |
| Overrides globales de cristal y brillos superpuestos | Retirados; superficies y controles comunes | Menos cascada accidental y coste de pintura |
| Animación de entrada de todo el historial | Historial inmediato; columna de lectura y compositor alineados | No retrasar el trabajo frecuente |

Estas mejoras no acreditan por sí solas calidad visual final. La muestra temporal
de componentes utiliza datos ficticios y no verifica conexiones ni ejecución real.

## Verificación

- Baseline backend runtime: 5321 pruebas pasaron, 19 omitidas, 38 deseleccionadas.
- Backend tras los primeros cambios: 5322 pruebas pasaron, 19 omitidas, 38 deseleccionadas.
- Frontend tras esta entrega: build/typecheck y 141 pruebas pasan.
- Ruta de aprobaciones: 6 pruebas pasan, incluidas indisponibilidad sin falso vacío y rechazo de `always`.
- Loader desktop baseline: 81 pruebas pasan.
- Revisión visual inicial de permisos y detalles desplegados: realizada en navegador.
- Pendiente: QA visual del chat real y todas las vistas, teclado completo, tamaño
  estrecho, accesibilidad y movimiento reducido, Rust y contenedor real.
- Los skips de dependencias y los gates de release no son acreditación de release.
- No publicar ni mover tags como consecuencia automática de esta revisión.

## Google Ads: corrección que no debe volver a deshacerse

Las specs 028 y 029 contenían instrucciones obsoletas para reintroducir developer
tokens. Se corrigen: el acceso depende del proyecto Cloud propietario del OAuth.
No añadir un campo opcional de compatibilidad. [Migración oficial](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).

El MCP oficial de Google es de lectura en la documentación comprobada el 11 de
septiembre de 2026. No sustituye a las operaciones de escritura del SDK/API.
[Documentación oficial](https://developers.google.com/google-ads/api/docs/developer-toolkit/mcp-server).

## Trabajo abierto, en orden

1. Chat completo: compositor, actividad de herramientas, estados de ejecución,
   panel contextual, scroll, foco y permisos junto a la acción que los solicita.
2. Aprobaciones: diferenciar autorización de capacidad y propuesta de Ads, mostrar
   impacto/diff, revisión antes de ejecutar y resultado persistente con trazabilidad.
3. Navegación, ajustes y catálogos: consolidar componentes; revisar ancho, densidad,
   jerarquía, vacíos, errores y carga en todas las superficies (incluido loader).
4. Safent Ads: catálogo actual de capacidades Google/Meta, autorización OAuth real,
   lectura, cambios de presupuesto y creación de campañas mediante propuestas
   aprobadas, errores y reconciliación. Cobertura end-to-end aún no acreditada.
5. Autonomía: observar, proponer sin petición manual, evitar duplicados, aplicar
   política y autorización exacta, recuperar reinicios sin reejecutar gastos.
6. Backend completo: inventario de imports/entrypoints/configuración/servicios antes
   de borrar; revisar aislamiento, contratos, errores, almacenamiento y despliegue.
7. Validación integrada en contenedor y desktop antes de cualquier publicación.

## Enterprise y sistemas del cliente (alcance añadido por el propietario)

La revisión también cubre el tablero Enterprise y la lógica de negocio y control.
La prueba de referencia es una organización con múltiples instancias Community
asociadas, permisos distintos y aislamiento frente a otra organización.

Enterprise debe disponer de un módulo para conectar CRMs y sistemas propios por
API, webhook o SSH, heredable por sus instancias con permisos individuales.
Comprobar infraestructura existente antes de crear otra capa. No confundir heredar
una conexión con otorgar todas sus operaciones o entregar sus secretos al agente.

Criterios: autenticación y rotación de credenciales, autorización tenant/instancia/
operación, revocación, URLs y destinos restringidos, claves de host SSH verificadas,
firmas y antirreplay de webhooks, idempotencia, reintentos acotados, cola duradera,
límites del proveedor, auditoría redacted y ausencia de duplicados tras reinicios.
La ejecución aprobada debe seguir siendo exactamente la operación revisada.

Repositorio encontrado: `lumen-control-enterprise`; rama `ee-cure` en `8b16278`
contiene correcciones de seguridad que aún no están en `master` (`547d1c1`).
Se revisa desde esa rama en una worktree aislada; no se ha desplegado ni integrado
esa rama a ciegas. Sus pruebas interoperables apuntaban a un checkout antiguo del
runtime por ruta absoluta; se está corrigiendo para seleccionar explícitamente
`SAFENT_RUNTIME_SRC` y validar el runtime actual, no otro código.

Los informes históricos y sus etiquetas PASS/FAIL no sustituyen al código actual.
Las worktrees ajenas con cambios deben conservarse. Integrar commits verificados
en la rama compartida DGX, sin sobrescribir trabajo concurrente.
