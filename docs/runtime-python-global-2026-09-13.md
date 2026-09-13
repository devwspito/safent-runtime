# Runtime Python — revisión global del 13 de septiembre

Esta revisión no reconstruye imágenes, no mueve tags y no altera datos de usuario.
Se ejecuta la **suite base completa configurada en pyproject.toml**, no todos los
escenarios opt-in ni una certificación de despliegue. El frontend y Rust tienen
sus resultados separados; no se suman aquí.

## Primera pasada: roja, conservada como evidencia

Snapshot aislado `/tmp/safent-python-global.gGvi7u`: base
`d378021e1b3ee77fa32c52f90a6c485a283a5a61` más WIP capturado de preparación 0.9.10.
SHA256 de `source-wip.patch`:
`5c5c1484a2851998c6f833e732b8356021cf3f52b6782122ea4728da68490e95`.
Incluye el entonces nuevo test de reservas de red, SHA256
`a51abc99094c883206ef15d3e4fd2bef78128d9ef0427bc8b83ff98ea0a7ad4d`.

Resultado: **7472 PASS, 17 FAIL, 25 SKIP, 250 deselected**, 464.16 s de pytest,
466.92 s de reloj. Log: `backend-full.log` en ese scratch. No fue una ejecución
verde y no se reclasifica retroactivamente como tal.

## Causas y correcciones exclusivas de pruebas

1. Dieciséis tests HTTP de `test_system_update_manifest.py` no aislaban el almacén
   de solicitudes. La consulta real usa el lock de `install_requests`, que exige
   su directorio de instancia; el test intentaba crear `/var/lib/hermes/instance`
   en el host sin permiso. Se añade fixture autouse con `tmp_path` y monkeypatch
   de `_INSTANCE_DIR`. El código real de lectura/lock/firma se sigue ejecutando;
   no se simula `updating`, no se crea ni chmod de una ruta real del host.
2. El fake Podman de `test_companion_scaffold.py` devolvía exit0 vacío para
   `volume inspect`. La nueva verificación productiva correctamente rechazaba
   esa identidad y el arranque core legacy omitía el montaje de Anuncios. El
   fake ahora devuelve la identidad exacta del volumen privado. No se elimina
   la expectativa: se comprueban tokens argv, red única, montaje privado único
   de sólo lectura, ausencia de binds individuales secretos y ausencia del
   aviso de fallo de scaffold. La GUI requiere además la convergencia Ads real;
   estos tests del CLI no sustituyen esa aceptación.

Las dos causas se reprodujeron sobre el commit limpio
`a6a407d9bcbccaa994b0280c6ff2f4f0a2fc6b73` sin cambiar producción: **2 FAIL**.
Tras modificar sólo las dos fixtures: **64 PASS**, 1.42 s, incluidos los módulos
afectados y las pruebas de deriva del catálogo Ads con ruta explícita. Logs
`reproduce-before.log` y `focal-after.log` en
`/tmp/safent-python-a6a407d.9cCsWE`.

## Segunda pasada completa

Snapshot limpio `a6a407d9bcbccaa994b0280c6ff2f4f0a2fc6b73` más únicamente
las dos correcciones de fixtures descritas arriba. **7493 PASS, 0 FAIL,
21 SKIP, 250 deselected, 8 warnings**, 441.09 s de pytest y 443.67 s de reloj.
Log: `/tmp/safent-python-a6a407d.9cCsWE/backend-full-after.log`.
No cambió ningún archivo de producción para obtener este resultado.

Los 21 skips: dos fixtures cross-repo no suministradas, dos módulos wizard
spec003 ausentes, dos protocolos legacy ausentes, cuatro skips por stubs globales
del SDK/tools nativos, siete plantillas Landlock que necesitan parámetros, un
binario gitleaks fuera del PATH, dos pruebas opt-in de volumen real y un gate
opt-in de clave de release. Las dos pruebas de proyección real tienen evidencia
independiente en el cierre 0.9.9; no se cuentan como ejecutadas en esta full.

Los ocho warnings tampoco se ocultan: deprecación Starlette/AnyIO, campo `register`
en AgentDraft, dos avisos de coroutine de un sink fake, un marker asyncio sobre
test síncrono, dos operation IDs OpenAPI duplicados del puente Ads y el aviso de
fork desde un proceso multihilo. No se modificaron esos módulos en este corte.

## Entorno y cobertura

- Python 3.12.3, pytest9.1.1; virtualenv efímero con dependencias, sin modificar
  el `.venv` canónico (Python3.13, no contiene pytest). `PYTHONPATH=src` siempre
  apunta al snapshot, no a otro checkout instalado.
- Hermes Agent0.21.1 real, procedente de `/usr/lib/hermes-agent` de la imagen
  local `d77b4de1cfba`; mismos SDK declarados por Containerfile: MCP2.0.0,
  Anthropic0.87.0, boto3 1.42.89, Azure Identity1.25.3, Google Auth2.55.1;
  Pydantic2.13.4. La imagen sólo aportó fuentes/dependencias: no se arrancó un
  daemon ni se certificó con ello el artefacto final.
- `env -i`, HOME/HERMES_HOME temporales, sin variables de proveedores; caché
  Hugging Face offline. No cuentas publicitarias reales, instalación del Mac,
  despliegue ni servicios con datos de usuario.
- Los 250 deselected proceden del `addopts` versionado: integration,
  requires_network, requires_llm, requires_chromium, requires_external_ocr,
  requires_vm y requires_openshell. No se quitaron más módulos de la pasada.
- El aislamiento de HOME omitió cuatro checks de deriva Ads en la primera
  pasada; se ejecutaron en el foco y la segunda pasada usa `SAFENT_ADS_REPO`
  explícito, sólo para leer su catálogo de código.
- El test de SDK nativo se salta durante colección global por stubs de otros
  módulos, no porque falte Hermes real. Ejecución aislada del archivo:
  **9 PASS**, 4.84 s (`provider-sdk-isolated.log`). No se suman a la full.
- Siguen siendo límites separados los contratos legacy ausentes, plantillas
  Landlock parametrizadas, fixtures cross-repo no suministradas, gitleaks fuera
  de PATH y gates opt-in de proyección real/clave de release. Los skips exactos
  constan en cada log; no son pruebas aprobadas.

Reproducción: en un archivo completo del commit de origen, aplicar únicamente
estas dos correcciones de tests, proporcionar el venv de dependencias y ejecutar
`PYTHONPATH=src <venv>/bin/python -m pytest -q`. Aislar HOME/HERMES_HOME y fijar
`SAFENT_ADS_REPO` al checkout Ads auditado. No apuntar a `/var/lib/hermes` real.
