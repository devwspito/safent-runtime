# LLM-02A — perfil corporativo single-binding (checkpoint 2026-09-11)

## Estado y frontera

Preparación implementada y probada; **ejecución gestionada todavía NO habilitada**.
`resolve_managed_config` conserva el gate central de LLM-01R y no entrega la
credencial delegada a consumidores de ejecución. No se modificó Hermes upstream,
GovernedAIAgent, sus hooks de jaula, el motor, lifecycle, restart, IPC, roster ni UI.
No se desplegó ninguna imagen ni se usaron credenciales reales.

## Cambios incluidos

- `runtime/managed_llm.py`: rechaza más de un binding firmado antes de mutar el
  estado; permite cero para tombstone/revocación. Context manager
  `local_configuration_write` serializa commits locales con la aplicación firmada.
- `runtime/managed_llm_profile.py`: funciones puras `build_profile` y
  `allowed_environment`. Genera selección custom explícita y todas las tareas
  auxiliares enumeradas por DEFAULT_CONFIG de la versión nativa fijada; mismo
  modelo/endpoint, sin fallback personal. No copia valores de configuración
  personales ni claves a config.yaml. Entorno por allowlist, HOME/HERMES_HOME/XDG
  exclusivos; no claves, proxies, perfiles SDK ni PYTHONPATH heredados.
- `agents_os/infrastructure/dbus_runtime_service.py`: setters síncronos locales
  serializados frente a la política firmada, con autorización antes de escribir.
  OAuth captura el DB scope al iniciar y vuelve a comprobar autoridad al persistir
  en los tres callbacks nativos (Nous, Codex y xAI). El polling HTTP ocurre fuera
  del lock. MCP nunca recibe automáticamente una credencial managed, incluso si
  en el futuro se abre el gate de ejecución.

Estas funciones de perfil **no están conectadas al arranque del daemon**. La
allowlist es una base de inferencia, no pretende sustituir la configuración de
D-Bus, broker y servicios del daemon completo.

## Evidencia

Los cinco primeros tests nuevos fallaron antes del arreglo: segunda asignación
aceptada, callback OAuth tardío persistiendo, clave managed exportada a MCP,
constructor de perfil y allowlist inexistentes. Tras implementar, el grupo
`test_managed_llm_profile.py` + `test_managed_llm_gateway.py` +
`test_provider_active_governs_engine.py` dio **66 passed, 2.29s, exit 0**.
Incluye modo OAuth personal intacto en los tres flows, callbacks managed
rechazados, validación de perfil, scope ausente y concurrencia setter/aplicación.

Matriz REAL en imagen DGX existente `365e584d7f5c`, Hermes **0.21.1**:

| Ruta nativa | Éxito | 401 | 402 | 429 | Timeout | Cancelación |
| --- | --- | --- | --- | --- | --- | --- |
| GovernedAIAgent chat | OK | Sin respuesta exitosa | Sin respuesta exitosa | Sin respuesta exitosa | Sin respuesta exitosa | hard_interrupt nativo |
| compression | OK | AuthenticationError | APIStatusError | RateLimitError | APITimeoutError | AuxiliaryExplicitCancellation |
| vision (router auxiliar) | OK | AuthenticationError | APIStatusError | RateLimitError | APITimeoutError | AuxiliaryExplicitCancellation |
| review | OK | AuthenticationError | APIStatusError | RateLimitError | APITimeoutError | AuxiliaryExplicitCancellation |
| memory_query_rewrite | OK | AuthenticationError | APIStatusError | RateLimitError | APITimeoutError | AuxiliaryExplicitCancellation |

**30/30 casos, exit 0; 43 requests al gateway ficticio, 0 al señuelo personal.**
El control positivo previo sí ejecutó una llamada real desde un perfil personal
ficticio al servidor señuelo con su clave ficticia. Después cada caso construyó
perfil corporativo limpio a partir de un entorno contaminado con ese HOME y API
key personal. Cada request de inferencia debía usar exactamente modelo `company`
y `Bearer test-scoped-only`. Los errores nunca se trataron como éxito.

`tests/integration/managed_native_profile_matrix.py` usa dos servidores loopback y
procesos efímeros **sólo como aislamiento de pruebas**, no como diseño productivo.
Contenedor `--network none`, único mount de scratch read-only, sin mounts de
usuario. No monkeypatch de SDK/Hermes. La fixture sustituye HTTPS por HTTP
loopback, fija timeout nativo custom a 0.2s, retries del agente a 1 y max_tokens a
16 para acotar tiempo/coste. Auxiliares usan `scoped_runtime_main` y cancel_event;
chat usa el agente real y `hard_interrupt`, no terminación forzada del proceso.

Comandos reproducibles (scratch de esta ronda):

```sh
ssh DGX-remote 'cd /tmp/safent-llm02a.38rE0v && PYTHONPATH=src:. python3 -m pytest tests/unit -q'
ssh DGX-remote 'podman run --rm --network none --entrypoint /usr/bin/python3 -v /tmp/safent-llm02a.38rE0v:/review:ro -e PYTHONPATH=/review/src -e HERMES_HOME=/tmp/llm-check -e HOME=/tmp/llm-check 365e584d7f5c /review/tests/integration/managed_native_profile_matrix.py'
```

Suite unit completa del snapshot congelado: **5467 passed, 19 skipped,
38 deselected, 4 warnings, 181.73s, exit 0**. Skips: dos contratos wizard ausentes,
siete plantillas Landlock no parametrizadas, siete por drift del SDK Composio del
host, un import Hermes ausente en host (cubierto separadamente con imagen real),
gitleaks no instalado y gate de release no activado. Los 38 deselected corresponden
a los markers predeterminados de pyproject (Chromium/LLM/OCR/integration/network/
VM/openshell). Warnings preexistentes: campo register de Pydantic, un test sync
marcado asyncio y dos operation IDs duplicados del proxy Ads. `git diff --check`
sin errores. No son pruebas de boot, release ni servicios productivos.

## Lo que aún NO está validado/habilitado

- Entrada/salida/rotación de gestión con drain, cancelación y reinicio controlado;
  limpiar globals sólo mediante proceso corporativo exclusivo, no monkeypatch.
- Proyección del perfil secreto-free a disco con ownership/permisos, inyección de
  token vault sólo al runtime nativo y configuración del entorno completo.
- Normalizar max_tokens nativo al límite real del grant y probar gateway EE real
  extremo a extremo. La matriz transporta 16 explícitamente, no demuestra que el
  default de Hermes cumpla el cap de cualquier grant.
- Cobertura funcional completa de imágenes reales en vision, plugins, MCP,
  herramientas, background lifecycle y todas las tareas auxiliares. Aquí se
  verifican rutas representativas de inferencia con prompts ficticios de texto;
  no es certificación de toda funcionalidad Hermes ni aislamiento OS/egress.
- Confirmar que el publisher Enterprise impone también single-binding y preparar
  una release conjunta. Dos bindings simultáneos ahora se rechazan, no se elige uno
  silenciosamente.

El resultado justifica continuar con daemon corporativo single-binding usando
Hermes nativo, pero **no quitar el gate ni publicar herencia usable aún**.

## Siguiente TASK-01R — sólo propuesta, NO implementado en este checkpoint

El usuario decidió retirar agentes empaquetados y sustituir Community por Tareas;
Enterprise debe mostrar equipo humano real y tareas. No se cambió ese backend
en esta ronda. El siguiente corte debe retirar catálogo/seeding/toggles y
atribución inferida sin borrar historial, sin cambiar agent_id de tareas antiguas
y sin fallback silencioso a default para un destino retirado. Preservar default,
custom/cloud autorizados y la jaula; retiro durable de filas factory legacy.

Contrato de lectura propuesto, todavía **NO endpoint existente**:
`GET /api/v1/tasks/dashboard?limit=100` →
`{available:true,tasks:[{task_id,label,status,source,requested_by?,created_at?,updated_at?,conversation_id?,result?,approval_ids?}],has_more}`.
Estados reales de agent_tasks: pending, in_progress, completed, failed,
pending_approval, rejected, cancelled. source local/enterprise sólo por relación
durable con pending_delegations.task_id. No copiar payload completo, credenciales
ni inferir autoría por nombres. Ausencia de fuente verificable → 503, no vacío
disfrazado de disponibilidad. Admisión de delegación conserva endpoint y autoridad
existentes; no inventar task_id antes de encolar.

Coordinación propuesta con Enterprise: conservar envelope firmado de 12 campos.
Outbox durable para POST /v1/delegations/{request_message_id}/status con
event_id/secuencia estables, instancia destino autenticada, estados de entrega y
ejecución separados. El ACK no significa running/completed. El emisor runtime de
estos eventos **NO está implementado aquí**.
