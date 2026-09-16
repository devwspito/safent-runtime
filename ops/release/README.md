# Publicador de releases de Safent Desktop

`publish-desktop-release.sh` corre en esta DGX (nunca en CI) y publica el
Release de GitHub de `devwspito/safent-runtime` a partir de los artefactos de
una ejecución de `safent-desktop.yml` (repo `devwspito/agents-autonomy`).

## Secuencia de punta a punta

1. **Etiquetar** el commit trunk que se va a publicar.
2. **Disparar el pipeline** (inputs reales del workflow hoy: `publish`, `tag`,
   `companion_tag` — no existe aún un modo `release_artifacts` separado; ver
   "Estado real" abajo):
   ```
   gh workflow run safent-desktop.yml --repo devwspito/agents-autonomy \
     -f publish=true -f tag=vX.Y.Z -f companion_tag=vA.B.C
   gh run watch --repo devwspito/agents-autonomy   # anota el run-id
   ```
3. **Publicar**:
   ```
   ops/release/publish-desktop-release.sh <run-id> vX.Y.Z --dry-run   # solo verifica
   ops/release/publish-desktop-release.sh <run-id> vX.Y.Z             # crea/reusa draft, sube, verifica, undraft
   ```

## Estado real del pipeline

Hoy `safent-desktop.yml` ya sube `latest.json`/`runtime-manifest.json`/`.minisig`
directo al draft del Release y lo publica él mismo con `SAFENT_RUNTIME_REPO_TOKEN`.
Este script añade una segunda verificación independiente, gateada desde esta
DGX, antes de confirmar — y queda listo para cuando el pipeline deje de
autopublicar y sólo exporte artefactos descargables.

## Tests

```
ops/release/tests/run_tests.sh
```

Todo offline: un `gh` ficticio sustituye a GitHub; casos válido, tamaño
excedido, URL incorrecta, firma incorrecta y DMG ausente.
