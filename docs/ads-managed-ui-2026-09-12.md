# Community · Anuncios administrados (12 septiembre 2026)

## Corte entregado

`/anuncios` resuelve la política firmada antes de abrir el companion local. En
managed presenta asignaciones explícitas, campañas paginadas, métricas almacenadas
de 7 días, propuestas de pausa/presupuesto y enlace de revisión humana Enterprise.
No muestra ni transporta assertions humanas, no aprueba desde Community y no
habilita el despliegue central. Error de política nunca recurre al iframe libre.

Las propuestas envían el importe decimal escrito, sin convertirlo a float. Los
importes de lectura conservan el contrato numérico existente; no son usados como
payload de escritura. Se valida el DTO de respuesta y se descartan campos extra.

`expected_binding` es una precondición pública opcional del bridge/transporte,
obligatoria en este cliente UI. Se compara estrictamente con la asignación firmada
bajo el lock de configuración, antes de revelar la credencial de bootstrap.
No concede autoridad. MCP conserva resolución fresca sin snapshot UI. Cambiar de
cuenta/política aborta y descarta respuestas anteriores; cada llamada UI comprueba
la política antes y después. Revocación remota se verifica por el transporte
existente, sin cache positiva. No hay retry automático de propuestas/revisiones.
Un fallo posterior a una escritura informa resultado incierto, no rollback.

## Evidencia

- Frontend completo: **358 PASS**, `NODE_OPTIONS=--no-experimental-webstorage npm test`.
  Node local 26.0.0 necesita ese ajuste para que jsdom suministre localStorage;
  sin él el primer intento tuvo fallos de entorno, no se cuenta como PASS.
- `npm run build`: **PASS** tras integrar el ajuste MFA ajeno del fixture
  `ApprovalCard.test.tsx` (commit raíz `1179db5`).
- DGX scratch propio `/tmp/safent-runtime-managed-ads.VeHNeY`: **60 PASS**
  (`test_managed_ads_transport.py`, `test_managed_ads_policy.py`,
  `shell_server/test_ads_bridge.py`). Incluye HTTP real ASGI con precondición
  inválida→403/cero requests upstream, snapshot correcto→misma ruta existente.
- Ruff: transporte y test; mypy: transporte (`--follow-imports=silent
  --ignore-missing-imports`). No se afirma typing global.
- QA Chrome: `npm run dev -- --host 127.0.0.1 --port 5176 --strictPort`,
  `/app/qa/managed-ads.html`. Recorrido cuenta→campaña→10.00→propuesta→enlace EE,
  foco de teclado y revocación. Viewport estrecho observado **433 px CSS**
  (override 390 con zoom del navegador): scrollWidth=clientWidth, cero iframes.
  El fixture utiliza datos sintéticos, no un backend/proveedor real; el E2E
  crossrepo del transporte pertenece al corte backend anterior.

## Revisión Emil

| Before | After | Why |
| --- | --- | --- |
| Managed terminaba en error de companion local | Workspace propio con selector explícito | Hace visible el ámbito sin fingir acceso global |
| Instalación/URL local disponible para Ads administrado | Mensaje de administración Enterprise en catálogo | Evita una ruta que el servidor debe rechazar |
| Sin lectura/propuesta accesible desde la vista | Filas compactas, formulario contextual y revisión separada | Jerarquía clara sin tarjetas enormes |
| Respuesta tardía podía pertenecer a contexto anterior | Abort, keys por política/asignación y precondición servidor | Corrección invisible y ausencia de cruces |
| Navegación susceptible a decoración innecesaria | Sin animación de entrada/teclado; controles y foco existentes | Uso frecuente, respuesta inmediata y movimiento reducido |

Pendiente: publicación/aplicación real de políticas, TLS/egress del despliegue,
activación central controlada y ampliación de herramientas. No cubre creación de
jerarquía completa, creativos ni recuperación de propuestas por listado en esta
vista. Un enlace EE no significa aprobación recibida ni ejecución completada.
