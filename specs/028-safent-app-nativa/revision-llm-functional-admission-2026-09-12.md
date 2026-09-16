# LLM Enterprise → Community: admisión funcional

## Resultado

Los dos bloqueos constantes se sustituyen por la autoridad **ya existente** del
proceso. No se añade un flag, ejecutor, proxy local, parche Hermes ni mecanismo de
credenciales paralelo. Una política firmada sin arranque corporativo admitido no
habilita inferencia. La instancia personal sin asociación conserva resolución nativa.

| Antes | Después | Motivo |
| --- | --- | --- |
| Resolver rechazaba toda asignación incluso después del bootstrap válido | Comprueba admisión y modo `managed`, lee binding bajo bloqueo y vuelve a comprobar generación | Hacer utilizable la herencia sin permitir un proceso antiguo/personal |
| Factory siempre rechazaba `managed=True` | Exige admisión y coincidencia de modelo, proveedor nativo, URL y credencial con el binding actual | Un objeto del llamante no puede elegir otra identidad |
| Error afirmaba que Hermes no podía aislar auxiliares | Explica falta de bootstrap corporativo vigente y reinicio controlado | Diagnóstico fiel al control que falla |
| Diagnóstico guest modificaba dos funciones instaladas | Verifica admisión en fuentes intactas y rechaza discos con manifiesto de parches legado | La prueba ya no necesita desactivar el producto |

## Autoridad y límites

- `ProcessAdmission` sigue procediendo de `complete_process_bootstrap`: perfil
  limpio, receipt sellado, PID, base de datos, generación y asociación vigentes.
  Este lote no modifica ni relaja ese bootstrap.
- Se comprueba admisión también cuando desaparece la política: un proceso
  corporativo viejo no puede pasar silenciosamente al entorno personal.
- El factory permite parámetros operativos ya existentes, pero no cambiar la
  identidad del binding ni `extra` para introducir extensiones de petición local.
- Continúan intactos la jaula, el broker y los controles por herramienta; no se
  habilitan capacidades nuevas. La revocación/generación invalida procesos y
  respuestas tardías; un resultado de SDK después de perder autoridad es cancelación.
- No se exporta credencial al entorno/perfil nativo ni se autoconfigura un MCP con
  ella. El perfil corporativo tiene un único binding y auxiliares dirigidos a él,
  sin cadena de fallback personal.
- El gateway Enterprise conserva validación y límite de tokens. No se distribuye
  la clave maestra de empresa ni OAuth personal. El schema inicial sigue siendo
  texto/herramientas de función: la prueba `vision` de routing usa **texto**, no
  certifica solicitudes multimodales admitidas por el gateway.

## Evidencia ejecutada

1. **173 PASS**, cero skips: 11 módulos focales Linux con SQLite/vault reales;
   firmado/admisión/arranque/generación/revocación/PID/base de datos/alias,
   fuentes de configuración, selección nativa, compatibilidad Hermes, perfiles y
   arnés guest. Incluye lectura que cambia autoridad antes de devolver el binding,
   y factory que rechaza endpoint/clave/modelo/proveedor manipulados. Una pasada
   previa tuvo 170 PASS y un fallo de regex de mensaje antiguo; se corrigió la
   expectativa, no se relajó la denegación.
2. **Hermes 0.21.1 real**, imagen existente `365e584d7f5c`, red externa deshabilitada:
   matriz de 30 casos (chat, compresión, visión, review, memory-query-rewrite ×
   éxito/401/402/429/timeout/cancel). **43 peticiones al gateway fixture, cero al
   señuelo personal**, después de verificar que el señuelo sí recibe el control
   positivo. No parches upstream.
3. **Factory productivo → Hermes real → HTTPS loopback → validador Enterprise real**,
   con exec limpio/receipt sellado, **cero sustituciones de gates**. Éxito con cap
   explícito 16; éxito con `max_tokens=None` real (Hermes omite el campo y EE aplica
   4096); revocación con salida controlada 75; reemplazo arranca bloqueado.
   **9 peticiones, 7 comprobaciones EE** incluyendo modelo/credencial/schema/cap
   inválidos. Tras el turno, la credencial no está en variables de entorno ni en
   `config.yaml`, `.env` o `auth.json` del perfil efímero.
4. Ruff PASS en nuevos tests y scripts modificados de factory/bootstrap/verificación,
   y `managed_llm.py`. `git diff --check` PASS. No se afirma lint global limpio:
   `model_config.py` conserva un aviso previo de imports y `nous_engine.py` tiene
   deuda de lint anterior ajena a este corte.

## Reproducir sin proveedores ni datos de usuario

Crear scratch dedicado y copiar `src`, `tests`, `pyproject.toml` del runtime como
directorios completos. Copiar `enterprise/src/` a `enterprise-src/` dentro del
scratch. No montar `/var/lib/hermes` ni HOME de una instalación. La fixture rechaza
sobrescribir un master key existente. Sólo se utilizan valores ficticios.

Scratch utilizado: `/tmp/safent-llm-admission.5IJtFL` en DGX.

```sh
cd /tmp/safent-llm-admission.5IJtFL
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_managed_execution_admission.py \
  tests/unit/test_managed_llm_gateway.py tests/unit/test_managed_llm_bootstrap.py \
  tests/unit/test_managed_llm_lifecycle.py tests/unit/test_managed_llm_profile.py \
  tests/unit/test_managed_factory_payload.py tests/unit/test_managed_provider_isolation.py \
  tests/unit/test_provider_config_source.py tests/unit/test_provider_active_governs_engine.py \
  tests/unit/test_nous_engine_hermes021_compat.py tests/unit/test_managed_guest_harness.py -q

podman run --rm --network none --entrypoint /usr/bin/python3 \
  -v /tmp/safent-llm-admission.5IJtFL:/review:ro \
  -e PYTHONPATH=/review/src -e HERMES_HOME=/tmp/llm-check -e HOME=/tmp/llm-check \
  365e584d7f5c /review/tests/integration/managed_native_profile_matrix.py

podman run --rm --network none --add-host enterprise.fixture.test:127.0.0.1 \
  -v /tmp/safent-llm-admission.5IJtFL:/review:ro --entrypoint python3 \
  365e584d7f5c /review/tests/integration/managed_factory_gateway_smoke.py
```

## Pendiente de release, no atribuido a estas pruebas

No se repitió KVM/systemd/Landlock completo en este corte, por decisión coordinada:
queda para la única certificación de imagen final. Los scripts guest ahora exigen
fuentes intactas; el baseline sin gateway declara que **no** prueba inferencia
exitosa, y el escenario con gateway verifica la ejecución positiva. Tampoco se
construyó/publicó imagen, DMG ni release, ni se gastó en proveedor real.

El cambio permite ejecución **sólo después del lifecycle vigente**; no significa
que todas las instalaciones existentes estén reiniciadas/admitidas o desplegadas.
