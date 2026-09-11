# No publicar éxito tras perder autoridad durante inferencia

La prueba nativa `799f35c` encontró que Hermes puede devolver una narrativa de
interrupción en lugar de lanzar una excepción. El puente la propagaba y la cola
marcaba la tarea completada después de revocar la autoridad corporativa.

`run_admitted_native` ahora revalida la generación durable **después** de volver
del SDK y vuelve a comprobar el cierre del proceso al entregar el resultado al
event loop, sin E/S bloqueante en ese segundo paso. Si perdió autoridad durante
la llamada, propaga `OperationCancelled`: la cola y stream usan su ruta terminal
existente, sin retry bajo otra autoridad futura. La falta de autoridad al entrar
sigue denegándose; las excepciones originales del proveedor no se transforman.

La nota de cancelación ya no afirma que necesariamente fue el propio operador:
puede tratarse de una revocación Enterprise o de autoridad no verificable.

## Evidencia

- Cinco nuevas regresiones fallaron contra el código anterior, por devolver éxito
  tras cerrar admisión, revocar, revocar/restaurar, perder almacenamiento o cerrar
  la admisión mientras el resultado esperaba entrega al loop.
- 22 tests de lifecycle, incluidos cola SQLite real/reinicio/stream cancelado y
  ausencia de CHAT_REPLIED/completed. No se invoca un proveedor real en estas pruebas.
- Focal lifecycle/gateway/bootstrap/perfil/tasks: **260 PASS,26 deselected**,
  62,63 s; dos warnings de coroutine no esperada en fixtures preexistentes.
- Ruff y diff check PASS. Snapshot DGX `/tmp/safent-native-result.FjiPjg`,
  base `e58eaad` + tres archivos del corte; logs
  `/tmp/safent-native-result-{red,focus,full}.log`.

Suite completa del snapshot: **5879 PASS,19 SKIP,64 deselected**, siete warnings,
266,77 s. Los SKIP son SDKs/plantillas/escáner y gate de release ausentes en ese
entorno; el log los identifica. No incluye las diez pruebas del harness guest,
que fue integrado como otro corte mientras corría esta suite.

La reproducción nativa después del cambio y la clasificación general de flags `failed/interrupted`
del SDK son cortes separados; este arreglo no abre ninguno de los gates managed.
No certifica herramientas/servicios auxiliares, la imagen final ni una revocación
externa todavía no recibida. No deshace efectos ya ejecutados por un proveedor.
