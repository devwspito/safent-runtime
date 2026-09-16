# LLM-01R — herencia firmada preparada; ejecución gestionada NO habilitada

Fecha: 2026-09-11. Revisión aislada sobre runtime `676afd3`, sin despliegue,
sin credenciales reales y sin cambios al motor Hermes ni a los hooks de jaula.

## Resultado y límite de publicación

El runtime puede verificar y persistir una asignación de gateway de Enterprise,
actualizarla y revocarla con tombstone. No acepta la clave maestra del proveedor
ni OAuth personal de Enterprise. La ejecución gestionada permanece **bloqueada**
antes de revelar la credencial desde la fuente de configuración de producción.
Esto no es una entrega de herencia usable y no debe autohabilitarse en clientes.

Se comprobó un bloqueo real en Hermes 0.21.1: al empezar cada conversación,
`agent.turn_context._publish_runtime_main(agent)` llama a `set_runtime_main`
pasándole `api_key`; este publica tanto un ContextVar como mirrors globales con
la clave. No hay argumento público para evitar los mirrors y la función no es
un método de instancia sobreescribible. Desactivar únicamente compresión o
background review no corrige esa exposición. No se añadió monkeypatch global.

## Cambios acotados

| Antes | Después | Motivo |
|---|---|---|
| El resolvedor volvía a elegir el proveedor nativo global después del alias | Identidad seleccionada explícita al resolvedor nativo, incluyendo OAuth local | No sustituir la selección de un agente por la personal |
| Caché por engine compartido | Identidad engine/modelo/proveedor/endpoint/digest de clave, con epoch | Evitar mezcla entre workers y respuesta obsoleta tras invalidación |
| Guardar alias inactivo sobrescribía `.env` del activo | Inactivos permanecen sólo en vault | No cambiar silenciosamente credencial efectiva |
| Selecciones sin API key no se sincronizaban | Selección OAuth/keyless local sí actualiza modelo nativo | Conservar camino nativo Hermes |
| Endpoint vacío retenía el anterior | Se elimina del modelo y se limpia env correspondiente | No enviar a un endpoint de otro proveedor |
| Campo `managed_by=cloud` del caller concedía autoridad | Sólo envelope firmado y ligado a instancia puede escribir cloud | Eliminar autoatribución de autoridad por D-Bus |
| Config-sync reenviaba claves directas mediante CRUD | Sólo `ApplyManagedLlmGateway`; ruta privada antigua eliminada | No distribuir upstream keys |
| Retirar proveedor permitía fallback personal | Revocación versionada/tombstone; unpair explícito restaura libre | Gestión revocada no equivale a permiso personal |
| Config capturada podía preceder fuente dinámica | Fuente de autoridad consultada cada turno antes del snapshot | Aplicar nueva política sin arrancar otro engine |
| Token gestionado podía llegar a auxiliares/MCP | Resolvedor público bloquea antes de vault; constructor y test D-Bus también bloquean | Hermes actual no aísla las credenciales auxiliares |

## Contrato firmado

- `ProviderSpec.credential_kind`: `direct` por defecto, omitido al serializar el
  valor por defecto para preservar firmas anteriores; gestionado exige
  `instance_gateway`, `openai_compatible`, modelo, endpoint y token delegado.
- `PolicyPayload.llm_instance_id`: omitido si `None`; obligatorio tanto en
  asignación gestionada como en revocación con `providers=[]`.
- Endpoint: HTTPS del origen **y prefijo de despliegue** de la asociación,
  `/v1/inference/{grant}/v1`; no query, fragment, userinfo o ruta arbitraria.
- Envelope Ed25519 completo: firma, tenant, instancia, frescura, monotonicidad y
  colisión de versión comprobados en daemon, además de UID real del caller.
- Exactamente un `set_active` si hay bindings; nunca se elige otro grant por
  intuición cuando se revoca el seleccionado. Publisher debe emitir default
  autorizado explícito o tombstone. Instancias sin asignación previa no deben
  recibir el marker de revocación.
- Fallo parcial de vault deja estado `applying` bloqueante; misma política
  firmada puede reintentarse. Versión antigua no reinstala credencial revocada.
- Token estable renovado por Enterprise al servir política activa; rotación
  humana entrega otro bundle firmado. No endpoint separado no firmado.

`resolve_managed_config` es la frontera **pública de ejecución**, actualmente
bloqueada si existe gestión. `_resolve_managed_binding` es privado de pruebas y
diagnóstico de contrato/transporte; ningún consumidor de producción lo llama.
La UI puede inspeccionar estado/bindings sin revelar clave; no debe presentar
un binding almacenado como ejecución disponible.

## Verificación

Scratch DGX `/tmp/safent-llm01r.Vfo6Ep`, `PYTHONPATH=src` explícito para evitar
una instalación global de otro checkout. Ningún árbol canónico remoto cambiado.

1. Diez regresiones nuevas ejecutadas primero contra baseline: **10 fallos**.
2. Suite ampliada de providers, applier/config-sync, asociación, D-Bus,
   per-agent, caché, OAuth/native migration y jaula Hermes: **614 pasan**
   (19.70 s), después del gate central, ACK explícito y tests de firma/vault reales.
3. Concurrencia real `ThreadPoolExecutor` + barrera demuestra separación de
   credenciales/modelos en engine compartido. Tests comprueban que asignación
   nueva supera snapshot personal y que managed nunca construye AIAgent,
   mientras local sí lo construye.
4. Imagen real DGX `365e584d7f5c`, `hermes-agent==0.21.1` (alias rc2/0.9.0),
   `podman run --rm --network none`, HOME/HERMES_HOME efímeros y sólo scratch
   read-only. `tests/integration/managed_native_gateway_smoke.py`: **PASS**
   resolver native custom + GovernedAIAgent + Chat Completions SSE + function
   tools-schema por SDK real hacia fixture HTTP loopback. Son tres requests:
   probe Ollama `/api/show` respondido 404 y dos completions. No ejecución real
   de herramientas ni llamadas a proveedor live.
5. El smoke también **reproduce** el token ficticio en el mirror global nativo
   después de la conversación diagnóstica y verifica que el constructor de
   producción gestionado la bloquea antes. Esta evidencia es la razón del gate,
   no una declaración de compatibilidad segura completa.
6. Body nativo custom por defecto: `messages`, `model`, `stream=true`,
   `stream_options.include_usage=true`; no `max_tokens` por defecto en este
   modelo. Enviado al responsable del gateway para validar límites allí.
7. `git diff --check`: limpio. **Reejecución final completa sobre snapshot
   congelado: 5446 pasan, 19 skipped, 38 deselected, 4 warnings; exit 0**
   (194.80 s). Comando: `PYTHONPATH=src:. python3 -m pytest tests/unit -q
   --disable-warnings --maxfail=8`. No se editó ningún archivo durante esta
   ejecución. La corrida previa tuvo un nombre de fake incorrecto en la nueva
   prueba ACK (`FakeProxy` en vez de `FakeDbusProxy`); se corrigió y verificó
   primero con 99 pruebas del archivo y 614 focales, y después con esta suite
   completa final. No quedan fallos observados en la suite ejecutable.

Exclusiones de la suite completa, sin nuevas exclusiones introducidas:
38 deselected por los markers existentes `requires_chromium`, `requires_llm`,
`requires_external_ocr`, `integration`, `requires_network`, `requires_vm` y
`requires_openshell`. Los 19 skipped: dos contratos wizard spec003 ausentes,
siete casos de template Landlock que requieren parámetros, siete casos/módulos
Composio por versión del SDK de host, un test SDK Hermes ausente en host
(cubierto aparte por smoke de imagen), un binario gitleaks ausente y un gate
exclusivo de release que requiere `SAFENT_RELEASE=1`.

Primera colecta completa carecía de assets apps/ops/specs en scratch; se copiaron
del baseline. Segunda detectó dos assertions antiguas de CRUD sin firma, ya
adaptadas al contrato firmado, y ausencia del script raíz `safent`, ya copiado.
La siguiente corrida completa detectó cuatro assets adicionales ausentes
(`.github/workflows/agents-os-edition.yml` y `.gitleaks.toml`); se copiaron antes
de la corrida final indicada arriba. No se han ocultado errores ni hecho bypass
global de excepciones.

## Hermes real: referencias verificadas dentro de la imagen

- `/usr/lib/hermes-agent/agent/turn_context.py:368`: publicación de runtime.
- `/usr/lib/hermes-agent/agent/auxiliary_client.py:2535`: `set_runtime_main` sin
  opt-out; `:2502`: escritura de mirrors, incluyendo `_RUNTIME_MAIN_API_KEY`.
- Router auxiliar incorpora fallback/config/env/credential pools personales;
  `ContextCompressor` puede utilizarlo pese a recibir endpoint y key explícitos.

## Alternativa mínima LLM-02: una identidad corporativa por Community

**Viable como diseño preferente v1, todavía no implementado ni habilitado:**
exactamente un binding/proveedor/modelo por Community, compartido por todos sus
agentes en su daemon exclusivo ya existente. Los mirrors globales contendrían
la MISMA identidad autorizada incluso con ciclos concurrentes. No se necesita
un proceso por ciclo ni nuevo IPC para el requisito de heredar un solo LLM.
Los workers ya comparten engine/broker (`tasks/application/worker_pool.py:357`);
se conservan GovernedAIAgent, jaula, D-Bus, aprobaciones y persistencia.

Condiciones que hay que implementar y comprobar antes de quitar el gate:

- Restricción de un único binding en publisher y daemon; ningún alias/modelo
  personal o segundo grant efectivo mientras está gestionada la instancia.
- HOME/HERMES_HOME corporativo controlado y entorno allowlist sin OAuth,
  claves, pools o perfiles alternativos. Runtime, shell-server y config-sync
  comparten HOME actualmente; no basta cambiarlo sólo en un constructor.
- Pinning nativo explícito de proveedor, modelo y endpoint para tareas
  auxiliares; impedir overrides/fallbacks/plugin defaults ajenos a la política.
- Cerrar admisión y drenar/cancelar turnos antes de entrar, salir o rotar binding;
  terminar el cgroup antiguo y reiniciar con el perfil nuevo. Ya existe
  `KillMode=control-group` (unit `:247`), pero SIGTERM sólo ofrece cinco segundos
  antes de cancelar tasks (`runtime/__main__.py:2025`), no un drain transaccional
  completo de writes/streams. Recuperación no debe duplicar herramientas.
- El token sigue siendo exclusivamente delegado a esa instancia. Compartirlo
  entre agentes de esa misma identidad no autoriza exportarlo al entorno MCP;
  al habilitar el resolvedor habrá que conservar esa prohibición específica
  en el auto-wiring de MCP, que hoy queda cubierto por el gate central.

### ¿Basta configuración nativa sin cambiar la red general?

Es una alternativa **plausible que debe verificarse**, no existe un flag nativo
universal de exclusión de fallback confirmado. En la imagen 0.21.1:

- `DEFAULT_CONFIG.auxiliary.<task>` permite fijar provider/model/base_url/api_key
  (y api_mode según tarea). Incluye compression, vision, review,
  background_review, title_generation, memory_query_rewrite y mcp, entre otras.
- `_get_provider_chain()` sólo enumera openrouter/nous/local-custom/api-key;
  `_try_custom_endpoint` exige base+key; `_resolve_api_key_provider` salta
  proveedores sin credencial/pool. En un perfil realmente limpio, con sólo
  una credencial, esto respalda que sólo el gateway sea resoluble.
- Pero `_ladder_provider_fallback` permite fallback ante 402/429/conexión incluso
  con proveedor explícito: task.fallback_chain, main-agent-model y en ciertas
  segundas pasadas discovery. `_resolve_call_client` puede volver a auto si
  falta endpoint; tareas nuevas/plugins añaden defaults. Pinning de las tareas
  conocidas no es por sí solo una frontera universal de seguridad.
- Pendiente matriz con Hermes real: inferencia/compresión/visión/subagentes y
  éxitos/401/402/429/timeouts, perfil sin secretos alternativos, captura de todos
  los destinos. Debe demostrar cero tránsito alternativo sin alterar tráfico
  legítimo de herramientas gobernadas. No se afirma que un firewall nuevo sea
  obligatorio antes de probar esta opción más simple.

La unidad actual declara red HOST expresamente (`hermes-runtime.service:274`);
el proxy de egress existente confina navegador/MCP, no toda inferencia del
daemon. Si configuración nativa controlada no satisface la matriz, habrá que
añadir una frontera de egress específica. Aislamiento por proceso/ciclo e IPC
queda como alternativa de mayor coste sólo si se requieren distintos bindings
simultáneos por Community o falla la opción de identidad única; no es requisito
deducido del pedido actual del usuario.

Revocar server-side debe impedir nuevos requests y no confiar en un snapshot
ya entregado; una llamada ya aceptada/en vuelo requiere semántica explícita de
cancelación. No se promete cortar retrospectivamente tokens ya generados.

Sin commits, stage, push, imagen nueva o despliegue en este bloque. Las fixtures
de UI permanecen excluidas y no se ha trabajado sobre UI durante LLM-01R.
