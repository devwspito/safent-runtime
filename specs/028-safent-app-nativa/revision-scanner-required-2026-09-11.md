# Instalación: analizador obligatorio, nunca PASS ficticio

## Defecto reproducido

`_scan_install_target` devolvía None al faltar Security Center. Los instaladores
de skills, MCP y paquetes interpretaban esa ausencia como autorización para
continuar. `scan_install_draft` además fabricaba PASS/100 y un scan_id vacío.
No era una elección persistida y revisada del propietario, sino fail-open por
falta de una dependencia.

Nueve regresiones fallaron contra el código anterior: tres clases de revisión,
preview ficticio, instalación skill normal/force, paquete y MCP normal/force.
La prueba inicial tuvo un error de fixture (approval_gate ausente), corregido
antes de obtener esos nueve fallos funcionales; no se cuenta como reproducción.

## Cambio

- Dependencia ausente, servicio no construible o inicialización fallida:
  `blocked:true`, `code:scan_unavailable`, error seguro sin path/secretos upstream.
- No se inventan ID, veredicto, score ni evidencia para un análisis inexistente.
- Ningún instalador empieza descarga/prefetch/conexión/operación de paquete si
  falta ese análisis. `force` no lo convierte en revisión del propietario.
- Se conservan la revisión real, los hallazgos WARN/FAIL y su flujo de decisión
  explícita. No se cambian políticas, firmas ni preferencias del usuario.
- El frontend paralelo UI-CONFIG también deja de continuar cuando su preanálisis
  falla; el control de backend no depende de que la UI haga lo correcto.

## Pruebas

- Primer foco: **65 PASS**, incluyendo gates/overrides MCP existentes.
- Añadida décima regresión de inicialización fallida y cuerpo sin datos sensibles.
- Suite completa: **5738 PASS, 19 SKIP, 64 deselected**, 236.97s. Warnings y
  SKIP preexistentes (gitleaks/release/SDKs del host) no certifican imagen final.
- Scratch DGX `/tmp/safent-community-auth.MWyLY8`, `scanner-required-full.log`.
  `PYTHONPATH=src python3 -m pytest tests/unit tests/tasks -q -rs --tb=short`.
- No instaladores reales, red de paquetes ni credenciales del cliente en estas
  pruebas: dobles de los efectos confirman que no se llaman.

No es una auditoría terminada de todo Security Center. Pendientes separados:
proveniencia del preview de bloqueos sin ScanRecord, alcance/TTL de overrides,
vinculación exacta al artefacto descargado y fallo de persistencia de auditoría.
Tampoco certifica todos los analizadores de una imagen final.
