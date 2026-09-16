# Community 0.9.1 — preparación de metadatos

Versión preparada en fuentes, **sin construir ni publicar imagen, bundle nativo,
DMG, firma o tag**. No se modifica Ads ni la lógica de LLM.

Fuentes sincronizadas con `VERSION`:

- `pyproject.toml` y `src/hermes/__init__.py`.
- `desktop/package.json` y las dos versiones del paquete raíz en su lock npm.
- `desktop/src-tauri/Cargo.toml`, entrada `safent-desktop` de `Cargo.lock` y
  `desktop/src-tauri/tauri.conf.json`.
- `frontend/package.json` y las dos versiones raíz del lock npm.

No se cambian dependencias ni versiones de crates ajenos con el mismo número.
No se reescriben fixtures ni informes históricos de 0.9.0. El scaffold antiguo
`frontend/src-tauri` permanece en 0.1.0: no aparece en scripts/workflows de
`.github`, `ops` o `desktop/scripts`; el launcher publicado es `desktop/src-tauri`,
documentado en `desktop/README.md` y `desktop/RUNTIME-BUNDLE.md`. No se elimina
ese scaffold en este corte.

## Checks ejecutados

- **5 PASS** Linux: consistencia de las fuentes autoritativas e import de
  `hermes.__version__`, más contrato `/healthz` existente.
- **7 PASS** renderer nativo: `src/native-updater.test.ts`, sin modificar sus
  ejemplos históricos. `desktop` typecheck y build JS/HTML PASS.
- `frontend` TypeScript + Vite build PASS.
- `cargo metadata --offline --locked --no-deps --format-version 1` sobre scratch
  DGX devuelve exclusivamente `safent-desktop` versión **0.9.1**. No compila Rust
  ni produce artefacto de distribución.
- Ruff del nuevo test y `git diff --check` PASS.

El nuevo `tests/unit/ops/test_release_version_consistency.py` enumera las fuentes
autoritativas, incluidos locks y Tauri, y falla con ruta/valor cuando divergen de
`VERSION`; no importa código de aplicación para analizar los manifests.

Queda pendiente la regresión final y única construcción/certificación de la
imagen decidida por el coordinador. Estos checks no equivalen a publicar 0.9.1.
