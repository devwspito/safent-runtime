# Community administrada: propuesta de conjuntos y anuncios

## Alcance

Se expone `propose_ad_child` mediante el transporte administrado existente y su
catálogo MCP. No se introduce otro ejecutor ni credenciales locales. Se conserva
la clasificación **SPEND** de `mcp__safent-ads__propose_ad_child` ya añadida en
`9d8fd05`; otros slugs no heredan esa clasificación por coincidencia de nombre.

Entrada administrada:

```text
{grant_id, arguments: {entity_ref, child_plan, cause}}
```

El schema se genera del **ManagedAdChildArgs** real de Ads
`composition/managed_service.py`, no del contrato público que recibe business_id.
El servicio central deriva negocio/cuenta del binding, y valida padre y plan.
En la ruta HTTP de Community sigue siendo obligatoria la precondición
`expected_binding`; no constituye autoridad ni se transmite al servicio central.

Cuatro variantes explícitas, todas con `status: PAUSED`: grupo Google SEARCH con
MANUAL_CPC, Google RSA, conjunto Meta Traffic/CBO y anuncio Meta con `creative_id`
existente autorizado por el backend. Sólo se crea una **propuesta**: ninguna de
esas operaciones anuncia entrega, publicación o efecto aprobado.

## Controles conservados

- Token efímero por petición, ligado exactamente a instancia/usuario/cuenta,
  firmado y contrastado con política y pairing vigentes. Sin caché ni fallback.
- Payload congelado antes de bootstrap; modificar el objeto llamante mientras
  llega el token no cambia el plan enviado.
- Revocación durante bootstrap impide enviar al central; revocación durante la
  respuesta impide divulgar resultado tardío. No reintento automático de una
  propuesta cuyo resultado sea incierto.
- `approve`, `execute`, `submit_approval` y `attach_creative` permanecen fuera de
  la allowlist. **ATTACH_CREATIVE no se abre**. La operación propuesta no otorga
  autoridad para aprobar ni ejecutar desde Community.
- `Cache-Control: no-store` y guard de snapshot HTTP permanecen activos.

## Evidencia

- Prueba roja inicial: **5 fallos/5 PASS** porque faltaban catálogo y tool en la
  allowlist. Después, **94 PASS**, cero skips: child, transporte/política Ads,
  sensibilidad y delicadeza de herramientas.
- **2 PASS cruzadas** con fuente Ads explícita: snapshot JSON idéntico al modelo
  central registrado en `TOOL_MODELS`; validación de anuncio pausado y rechazo de
  `ACTIVE`/`business_id` suministrado por cliente.
- **Hermes 0.21.1 real** en imagen `365e584d7f5c`, contenedor efímero sin red:
  nueve schemas administrados; `propose_pause` y `propose_ad_child` entregan sus
  argumentos exactos al broker una sola vez cada uno, y el broker puede denegar.
  Ninguna ejecución del subprocess MCP local ni descubrimiento nativo paralelo.
- Ruff de los archivos Python cambiados y `git diff --check` PASS.

Los tests del transporte usan pairing/vault/SQLite/firma reales con transporte
HTTP ficticio. El smoke Hermes comprueba registro/ruta al broker, **no crea un
anuncio ni llama a Google/Meta**. La persistencia/validación central y la posterior
aprobación/ejecución se entregan en el lote Ads coordinado, no se atribuyen a este
smoke de Runtime. No se construyó imagen ni se publicaron tags.

## Reproducción

Scratch DGX utilizado: `/tmp/safent-llm-admission.5IJtFL`, con Runtime en `src` y
el snapshot Ads en `ads-src` (ambos copiados, no instalación del usuario).

```sh
PYTHONPATH=src python3 -m pytest \
  tests/unit/test_managed_ads_child.py tests/unit/test_managed_ads_transport.py \
  tests/unit/test_managed_ads_policy.py tests/unit/capabilities/test_tool_sensitivity.py \
  tests/unit/capabilities/test_tool_delicacy.py -q

PYTHONPATH=src:ads-src /home/luiscorrea-dev/Desktop/safent-ads/.venv/bin/python \
  -m pytest tests/integration/test_managed_ads_child_schema.py -q -m integration

podman run --rm --network none -e PYTHONPATH=/review/src \
  -e HERMES_HOME=/tmp/ads-native-home -e HOME=/tmp/ads-native-home \
  -v /tmp/safent-llm-admission.5IJtFL:/review:ro --entrypoint python3 \
  365e584d7f5c /review/tests/integration/managed_ads_native_smoke.py
```
