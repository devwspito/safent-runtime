# Continuar Safent

Entrada canónica del traspaso:
[CONTINUAR-SAFENT.md de Enterprise](https://github.com/devwspito/safent-control-enterprise/blob/fix/enterprise-review-20260911/CONTINUAR-SAFENT.md).

Leer después su `docs/safent-execution-plan.md` y
`docs/logica-pendiente-2026-09-11.md`. Incluyen todas las peticiones, arquitectura,
contratos pendientes, repositorios, pruebas y límites; no sólo OAuth o Tareas.

Prioridad actual: **TODA UI** Community APP NATIVA (Tauri, no sitio web),
Enterprise WEB APP CLOUD y Anuncios. Los tests browser no sustituyen instalación
y permisos OS. Agentes empaquetados salen; Enterprise Equipo humano + tareas,
Community Tareas. No crear segundo chat/motor ni saltar jaula/HITL.

Checkpoints runtime: ca53fa1 LLM-02A perfil probado pero gate cerrado;
6515a4c Tareas + arranque nativo + navegación. Consultar `git log` para commits
posteriores y sus informes en `specs/028-safent-app-nativa/`.
El dashboard backend de Tareas todavía falta. La UI informa indisponibilidad.
Ads b42e7a0 es WIP con migración fallida/IAM pendiente, no desplegar.

Antes de cambiar, comprobar HEAD/rama/remotos/dirty y coordinar agentes activos.
Hay una reactivación Codex cada 10 min; no crear otra. No borrar trabajo ajeno,
fixtures ni historial. No publicar imagen/tag v0.9.0 hasta cerrar bloqueos.
