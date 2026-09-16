# Errores genéricos del motor: privacidad y cierre visible

Corte posterior al clasificador nativo `ff6bf5e`. El catch de errores inesperados
copiaba `str(exc)` y traceback a journald y al chat/cola; un SDK puede incluir ahí
cabeceras, prompts, cuerpos del proveedor o URLs firmadas. Además, un `__str__`
fallido podía romper el propio tratamiento del error.

Ahora ese límite no convierte la excepción en texto. Loguea evento, clase de
error, latencia y task ID; el usuario recibe un mensaje propio con esa referencia.
Los fallos nativos estructurados conservan sus mensajes útiles y reglas de retry
explícitas. El error genérico mantiene el backoff acotado anterior, no éxito ni
aprobación. No es un saneamiento global de todos los logs/SDK del proyecto.

## Evidencia

- Dos pruebas nuevas rojas antes de cambiar producción: token ficticio publicado
  en log/chat/cola y excepción cuyo `__str__` lanza otro error.
- Dos pruebas verdes después, con SQLite real reabierta, sink, conversación y logs.
- Focal de tareas + clasificación: **235 PASS**, 26 deselected, 1 aviso de coroutine
  del fixture de streaming existente.
- Full: **5939 PASS**, 19 SKIP, 64 deselected, 7 avisos, 248,15 s.
  Los SKIP conocidos incluyen SDK del host, scanner no instalado y gates de release;
  no se contabilizan como cobertura nativa ni imagen final.
- Ruff del nuevo test y diff-check PASS. Tras formato del test, repetición focal
  final **2 PASS**; producción no cambió después del full.

Snapshot DGX `/tmp/safent-engine-privacy.E1w0so`: archive `ff6bf5e` más exactamente
`agent_loop_orchestrator.py` y `test_engine_error_privacy.py`. Log completo
`/tmp/safent-engine-privacy-full.log`. No incluye los cuatro tests adicionales del
harness `1c6b837`, integrado en paralelo; no hubo llamadas a proveedores reales.
