# CORE · metadatos HTTP sin secretos de callback

## Causa y cambio

`shell_server/main.py` configuraba logging y después llamaba a `uvicorn.run` con `access_log=True` sin configuración propia. Uvicorn aplicaba su `dictConfig` y su AccessFormatter estándar publicaba el request-target completo. La reproducción en subproceso confirmó `code`, `state` y el ticket `?k` en stdout. El mismo ejercicio confirmó que HTTPX INFO imprimía la URL y HTTPcore DEBUG podía imprimir cabeceras.

Se mantiene el registro HTTP: cliente/método/ruta/versión/estado en Uvicorn y método/origen/ruta/estado en HTTPX. `uvicorn_log_config()` deriva una copia de la configuración oficial y adjunta filtros antes de formatear, por lo que sobreviven a la configuración de arranque de Uvicorn. No modifica `uvicorn.config.LOGGING_CONFIG` global ni desactiva `access_log`.

- Se eliminan query, fragment y userinfo del destino; control characters no pueden crear líneas nuevas.
- El filtro de acceso conserva la tupla de cinco campos requerida por AccessFormatter. Una forma desconocida se sustituye por metadatos no verificables, no por el mensaje original.
- HTTPX conserva su línea estándar sin el contenido sensible de URL. La frase del estado se deriva de `HTTPStatus`, no de texto controlado por el servidor.
- HTTPcore conserva nombre/nivel de evento, sin valores opacos de cabeceras o excepciones. Mensajes HTTPX no reconocidos omiten su payload.
- Las tres ramas descartan `exc_info`, `exc_text` y `stack_info`: el formatter no puede volver a adjuntar una excepción que contenga la URL original.

## Puentes y límites

Ads bridge usa **aiohttp**, no HTTPX (`ads_bridge.py:42`). Su log explícito de fallo de logout conserva sólo el nombre de clase de excepción (`:698`), sin URL ni payload. CORE también usa HTTPX en `shell_server/cowork/crm_api.py`, `remote_control/service.py` y `skills/skill_synthesis.py`; sus logs de transporte quedan cubiertos por la configuración de HTTPX/httpcore del shell.

No es un redactor universal de cualquier log de aplicación ni oculta datos que una aplicación sitúe directamente en el path. No elimina registros históricos. No cambia OAuth, autenticación, CSRF, respuestas HTTP ni el límite de concurrencia del servidor. Otro servicio que no use esta configuración necesita su propia integración; los cambios Ads se revisan y publican en su lote separado.

## Pruebas

La prueba nueva ejecuta `configure_structured_logging` seguido de Uvicorn real, con ASGI en un socket loopback efímero y HTTPX real (`trust_env=False`), niveles INFO/DEBUG y canarios ficticios OAuth/bootstrap/cabeceras. No usa proveedores externos, credenciales ni datos reales. Verifica ausencia de canarios y presencia de método, ruta y códigos 200/204.

Rojo inicial: el subproceso falló por la fuga concreta `OAUTH_CODE_7291`. La segunda prueba de wiring tuvo inicialmente un path de fixture erróneo, corregido antes de verificar el `log_config` explícito; no se atribuye ese error al producto.

- Focal final `test_http_access_logging.py` + `test_logging_setup.py`: **15 PASS**, 0.53 s.
- Incluye formatter Uvicorn real con excepción, excepción ya formateada, stack y reason phrase adversarial; URI con userinfo/IPv6 y configuración idempotente sin mutación del global.
- Ruff de código y test: PASS. `git diff --check`: PASS.
- Mypy del módulo señala **un error preexistente**: `processors: list` sin parámetro (HEAD original línea 53). No se declara Mypy verde ni se modifica ajenamente para ocultarlo.
- Revisión independiente de `/root/enterprise_ui`: filtro persistente tras dictConfig y subproceso Uvicorn/HTTPX revisados; sin edición por el revisor.

## Suite global

La suite base completa se ejecuta en snapshot aislado `c2687ef` más la integración inicial de logging y 9 pruebas nuevas. Mantiene las correcciones de fixtures certificadas en `1b9c805` (base anterior **7493 PASS / 21 SKIP / 250 deselected**). El entorno limpio usa Python3.12 y Hermes0.21.1 de la venv de regresión anterior.

La revisión final que limpia excepciones/stack y normaliza la frase HTTPX se añadió **después** de iniciar esa suite. Se certifica mediante los 15 focales finales; no se atribuye al snapshot global anterior una ejecución que no incluyó esos dos ajustes y tres pruebas adicionales.

Resultado global: **7502 PASS, 0 FAIL, 21 SKIP, 250 deselected, 8 warnings**, 439.04 s de pytest / 441.39 s de reloj. Son los 7493 anteriores más 9 pruebas nuevas del snapshot inicial. Los 21 skips mantienen el alcance anterior: fixtures cross-repo, contratos legacy, stubs SDK/tools de la suite global, plantillas Landlock, gitleaks ausente, QA de volúmenes opt-in y gate de release. Los warnings incluyen corutinas de mocks, marca asyncio de test síncrono, IDs OpenAPI duplicados y fork multihilo; no se han ocultado.

Scratch DGX `/tmp/safent-http-logs-final.SQ8Z5Z`; log global `/tmp/core-http-logs-backend-full.log`, focal final `/tmp/core-http-logs-focal-final.log`. Selección completa predeterminada de pyproject, que excluye grupos opt-in `requires_chromium`, `requires_llm`, `requires_external_ocr`, `integration`, `requires_network`, `requires_vm`, `requires_openshell`. No equivale a certificar esos entornos ni a QA del binario macOS.

No se modifican metadatos, tags o imágenes; el cambio requiere integración en el core publicado antes de proteger una instalación existente.
