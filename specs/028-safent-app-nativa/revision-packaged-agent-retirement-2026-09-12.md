# Retirada de agentes empaquetados: superficies activas — 2026-09-12

## Resultado

El catálogo/seed de fábrica ya estaba retirado correctamente. Este corte corrige
restos de presentación y llamadas obsoletas realmente alcanzables; no borra
identidades, conversaciones, tareas, telemetría ni perfiles del usuario.

| Before | After | Why |
| --- | --- | --- |
| TUI y dock QML presentaban «Agentes» como sección/producto. | «Perfiles», descripción de configuración explícita. | Conservar perfiles custom sin presentar otra plantilla de equipo empaquetado. |
| Chips y cabecera afirmaban «Cerebro omnipotente». | «Perfil principal», protegido y sujeto a políticas/aprobaciones. | Un nombre de perfil no concede autoridad. |
| TUI esperaba `get_active_agent` para pintar la lista y ofrecía `set_active_agent`; QML mostraba «Activar»/«Activo». | Se retiran esas consultas, mutación, estado y controles globales obsoletos. | El daemon ya no expone activo global: el vínculo de conversación es la autoridad. |
| Prompt dirigía a «Agentes (/agentes)» y una pestaña Enseñar retirada. | Dirige a Tareas y actividad real En vivo, sin prometer controles inexistentes. | Alinear orientación del agente con las rutas actuales. |
| React no interceptaba enlaces del asistente a `/tareas`. | Navegación dentro de la SPA, probada en DOM. | Evitar recarga fuera de la app al seguir el enlace real. |

Emil se aplicó a coherencia/nomenclatura y supresión de controles engañosos.
No se añadieron animaciones, otro diseño ni un nuevo mecanismo de delegación.

## Inventario y evidencia de alcance

| Superficie | Estado comprobado |
| --- | --- |
| React, Community WebView | `App.tsx` ya monta Tareas; `/agentes` y `/office` son redirecciones históricas a Tareas, no catálogos. Se conserva la compatibilidad de enlaces/historial. Nav/labels legacy ahora dicen Perfiles; permiso técnico `agents` no cambia de identificador. |
| Prompt real de `NousReasoningEngine` | Se actualiza únicamente su descripción de rutas. Delegación nativa y emisor de identidad real no cambian. |
| TUI `hermes-tui` | Entrada instalada en `pyproject.toml`; pane, paleta, atajo 5, ayuda y cabecera coherentes. CRUD de perfiles continúa. Se retiran helpers de activo global también del bridge offline para no simular éxito de una API ausente. Alias de slash antiguos permanecen para navegación histórica. |
| QML compositor | Dock `agents` sigue montando `AgentsApp.qml`, con nombre visible Perfiles; lista/CRUD/capacidades permanecen. Se retira el botón y los chips de activo global. Los IDs internos no son un catálogo ni se migran innecesariamente. |
| QML Tareas/chat | Consumen perfiles reales mediante ListAgents. `sessions_fanout`, selección de conversación y `liveAgentRuns` se conservan: son delegación dinámica/telemetría, no seed empaquetado. |
| API REST/D-Bus | ListAgents usa el registro filtrado; endpoints de catálogo/default-roster y sus switches no están exportados. CRUD de custom/cloud permanece. Ninguna autoridad API se cambia en este lote. |
| Registro/seed | `SqliteAgentRegistry` sólo siembra `default`. Los 27 IDs retirados están reservados en `retired_factory.py`; se conservan filas históricas y se rechaza su ejecución/recreación sin fallback silencioso al principal. Perfiles custom y cloud preexistentes permanecen. |
| Ops | Búsqueda en `ops` y fuentes activas no encontró reintroducción de `default_roster`, seed o IDs empaquetados fuera del registro explícito de retirados. |

No se eliminan términos técnicos `agent`, `delegate_task`, `sessions_fanout`,
campos de telemetría ni los nombres internos de rutas/API. Tampoco se altera
el agente principal ni sus reglas, salvo orientación de rutas en el prompt.

## Pruebas

- **35 PASS**: `tests/tui/test_profile_retirement_surfaces.py`,
  `tests/tui/test_safent_terminal.py`, `tests/unit/test_factory_retirement.py`,
  `tests/unit/shell_server/cowork/test_roster_api.py`.
  Incluye Textual Pilot real con SQLite temporal: default + custom visibles,
  27 filas factory preservadas pero ocultas, teclado Enter sin mutación global,
  apertura/cierre de formulario y navegación a Tareas. Bridge externo explícito
  de prueba, no servidor D-Bus vivo.
- **12 PASS React**: ChatLifecycle, App y GovernanceSection. Regresión nueva:
  click en enlace `/tareas` del asistente evita navegación de documento y abre
  la ruta de la SPA. `NODE_OPTIONS=--no-experimental-webstorage`.
- **Build PASS**: `tsc --noEmit` y Vite, un build final del lote.
- Ruff PASS del nuevo archivo de pruebas; fuentes TUI/engine conservan deuda de
  lint preexistente (no se declara suite Ruff global verde). `git diff --check` PASS.
- Primera pasada Python tuvo dependencia `dbus-fast` ausente en el arnés;
  se añadió al entorno efímero y se repitió el conjunto de 35, sin skips.

Comando Python usado (dependencias de prueba `pytest`, `pytest-asyncio`, `textual`,
`dbus-fast`, `cryptography`, `httpx`, `fastapi`, `python-multipart`, `aiohttp`):

```sh
PYTHONPATH=src python -m pytest \
  tests/tui/test_profile_retirement_surfaces.py \
  tests/tui/test_safent_terminal.py \
  tests/unit/test_factory_retirement.py \
  tests/unit/shell_server/cowork/test_roster_api.py -q
```

## Límites de QA

QML se comprobó por su composición y ausencia de controles/calls retirados,
no mediante renderer Wayland: no hay `qmllint`/sesión Qt disponible en este
host. Textual Pilot y React DOM no certifican el binario Tauri de macOS.
No se abrió un proveedor, contenedor, servicio del usuario ni imagen final.
La UI legacy QML aún requiere QA general de errores y CRUD; este corte no la
presenta como una certificación visual completa.

## Archivos

- `frontend/src/lib/i18n.ts`
- `frontend/src/views/ChatView.tsx`
- `frontend/src/views/ChatLifecycle.test.tsx`
- `frontend/src/views/SeguridadView.tsx` (una etiqueta, sin política nueva)
- `src/hermes/runtime/nous_engine.py` (texto de rutas solamente)
- `src/hermes/tui/app.py`
- `src/hermes/tui/bridge.py`
- `src/hermes/tui/screens/agents.py`
- `src/hermes/tui/screens/chat.py`
- `src/hermes/tui/widgets/sidebar.py`
- `src/hermes/tui/widgets/statusbar.py`
- `src/hermes/lumen/compositor/qml/desktop/AgentsApp.qml`
- `src/hermes/lumen/compositor/qml/desktop/AppDock.qml`
- `tests/tui/test_profile_retirement_surfaces.py`
- Este informe.

Ads, LLM/herencia, configuración de seguridad y fixtures `.ui-*` quedan fuera.
