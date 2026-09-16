# Friendog: aceptación real y cierre pendiente

Actualizado: 2026-09-13 23:48 UTC (14 de septiembre, Madrid).

El objetivo sigue activo: Community nativa, Anuncios operativo, propuestas de
Friendog revisables y configuración/operación desde Safent. No confundir
autenticación, compilación o pruebas unitarias con entrega funcional completa.

## Restricciones del propietario

- Sólo Friendog, nunca Magister; usar las cuentas EUR designadas por el propietario.
- Presupuesto y URL de reserva pendientes: los dará al chat después.
- Sin campañas publicadas, activadas, aprobadas ni gasto en esta aceptación.
- Preservar datos, OAuth y claves. No incluir secretos ni snapshots de cuentas en git.
- Se autorizó la carpeta local Friendog-Marketing-Kit; dar acceso por la UI nativa.

## Verificado en la app instalada 0.9.23

- DMG notarizado, firma y checksum verificados; arranque correcto, datos preservados.
- Cambio Astra → Terra desde el selector, reutilizando el OAuth de ChatGPT.
- Qwen conservado pero no activo. No se verificó una respuesta Qwen en esta revisión.
- Primer turno real: Terra descubrió y llamó herramientas MCP y respondió con cuentas.
- Segundo turno en la misma conversación: consulta de campañas, respuesta visible;
  una campaña Google pausada y ninguna campaña Meta en la cuenta EUR elegida.
- Meta conectada desde Safent/Composio y lista de cuentas actualizada tras callback.
- Google respondió al acceso real de lectura. Esto NO acredita verificaciones de
  anunciante, límites, facturación ni aptitud de entrega de campañas.

## Regresiones halladas al usar datos reales

| Pantalla/contrato | Causa comprobada | Corrección |
| --- | --- | --- |
| Cuadro de mando | Python serie de 14 números, UI esperaba objeto; referencia de cambio objeto frente a string | Normalización Zod y fixture Python/TS compartido |
| Campañas/cartera | Conversiones sólo incluían canales medidos; UI exigía cuatro canales | Canales ausentes se conservan como desconocidos/null, no cero |
| Frescura | API devuelve items por cuenta; UI esperaba una sola fila | Se toma la cuenta más antigua; inventario vacío sigue desconocido |
| Estadísticas iniciales | Sentinel de no ingesta se presentaba como edad enorme | Texto «Estadísticas pendientes» |
| Freno | DTO GET/POST incompleto y estado por cuenta ausente | En trabajo por el carril backend; requiere aceptación live |
| Capacidades del agente | Escritura directa y reglas AUTO se confundían con preparar propuestas | Alcance explícito y claves opcionales separadas |

Commits Ads de correcciones del root: `6b4e6c9` y `e06bcf9`.
Se validaron respuestas HTTP reales de cockpit, portfolio, freshness y signals
contra los nuevos schemas: 4/4. El panel de la app 0.9.23 todavía tiene los schemas
antiguos: NO se ha hecho un parche oculto a la app instalada.

Prueba reproducible opt-in: `panel/src/api/live-read-contracts.test.ts`, variable
`SAFENT_LIVE_CONTRACTS_DIR` apunta a snapshots autenticados fuera del repositorio.
No subir los snapshots. Tras actualizar se deben volver a capturar y verificar
todos los endpoints, incluido kill-switch, y abrir sus pantallas en la app.

## Entregas en preparación

- 0.9.24 / Ads 0.2.17: borradores persistentes incompletos y carpeta nativa;
  imágenes verificadas, compilación de escritorio en curso.
- Siguiente candidato: límites nativos por cuenta con confirmación humana,
  escritura atómica y ACK del hash cargado por broker; correcciones del panel.
- Canal de imagen local: volumen persistente de marca, propuesta de upload
  acotada a PNG/cuenta/hash y readback Meta. No está aceptado live todavía.

## Pendiente para cerrar el recorrido Friendog

1. Instalar y verificar 0.9.24 preservando el estado.
2. Dar acceso real a la carpeta mediante el picker nativo; comprobar lectura en chat.
3. Crear desde el chat dos borradores persistentes, Google consulta veterinaria y
   Meta primera consulta gratis, sin inventar presupuesto, URL ni condiciones.
4. Verlos y editarlos desde Anuncios → Propuestas. La promoción requiere todos
   los campos y sólo genera una propuesta pendiente, nunca una aprobación.
5. Publicar/verificar el siguiente candidato con contratos y límites corregidos.
6. Subir una creatividad de revisión desde la UI con confirmación y verificar hash
   de proveedor. Crear un anuncio sigue requiriendo otra aprobación explícita.
7. Comprobar la revisión periódica de métricas y generación de propuestas sin
   confundirla con reglas de ejecución AUTO. Mantener gasto bajo aprobación.
8. Auditoría de autoconfiguración: el texto de connect_integration menciona
   configure_native_provider aunque ese tool no está registrado; corregir la
   superficie real, no prometer capacidades inexistentes.

La equivalencia total con Codex y la auditoría Enterprise completa no quedan
certificadas por estas verificaciones. El objetivo no está cerrado.
