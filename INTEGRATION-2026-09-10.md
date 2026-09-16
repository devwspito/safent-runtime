# Integración Ads desde el Mac a la DGX

Destino: `/home/luiscorrea-dev/Desktop/lumen-runtime-next`, rama
`feat/safent-next`. Merge de código `c28c8b4` sobre `2557f42`.

Se incorpora `6bb2f5c` (`Remove retired Google Ads token provisioning`):
`ops/container/companions/ads/provision.sh` solo admite las claves OAuth
Google y app Meta explícitamente soportadas. Conserva el provisionado SSO
de la rama 026 y los cambios posteriores de la DGX. Las 28 pruebas de
`tests/unit/ops/test_companion_provision.py` pasan sobre el resultado combinado.

El trabajo principal está integrado en
`/home/luiscorrea-dev/Desktop/safent-ads`, rama `main`, merge `732d6dc`:
credenciales conectadas, aislamiento Meta, lecturas MCP nativas gobernadas,
aprobación humana obligatoria para escribir y bundle de autonomía 24/7.
Consultar allí `INTEGRATION-2026-09-10.md` e `infra/native-mcp/README.md`
antes de reimplementar estos recorridos.

Los worktrees de otras ramas no reciben estos cambios automáticamente.
No se ha modificado `/home/luiscorrea-dev/Desktop/lumen-runtime` (checkout
antiguo de `main`, con cambios sin commitear), ni los demás carriles.

Esto integra código, no despliega servicios. Quedan pendientes la creación y
publicación completa de campañas, OAuth real, despliegue/autorización de los
triggers, recuperación tras reinicios y aceptación extremo a extremo.
