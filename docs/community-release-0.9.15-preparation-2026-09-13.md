# Community 0.9.15 — preparación de imagen

Base `eeac35f583ae23fddbd931aceb5145c92663e8ce`, que incluye el cambio de sidebar local de Anuncios. Sólo se alinean las diez fuentes autoritativas de versión a 0.9.15: VERSION, pyproject, __version__, package/lock de desktop y frontend, Cargo.toml/Cargo.lock y configuración Tauri. Los dos locks npm cambian únicamente sus entradas raíz; Cargo.lock cambia sólo `safent-desktop`, nunca `dbus` ni otras dependencias.

Validación previa al tag: **13 PASS** (consistencia de versiones, contratos de workflow/build cache y healthz), **0.23 s**. `cargo metadata --offline --locked --no-deps` devuelve `safent-desktop 0.9.15`; `git diff --check` aprobado. El código de sidebar tiene su batería separada de **373 PASS** y typecheck/build en `docs/ads-single-sidebar-2026-09-13.md`; no se atribuye esa batería a una nueva ejecución de backend.

Se comprobó ausencia del tag remoto, release GitHub y manifest OCI `v0.9.15` antes de preparar la publicación. El workflow oficial `.github/workflows/publish-image.yml` publica las dos arquitecturas nativas y sólo combina el índice tras ambas; no promueve `:latest`. El tag nuevo debe apuntar a este commit y no reemplaza ningún tag anterior. La ejecución exacta y digests se comunicarán al coordinador tras CI.

No se lanza el workflow de apps: el coordinador espera Ads 0.2.8. No se toca el Mac ni sus datos. La aceptación visual del sidebar, el futuro paquete nativo y OAuth real siguen pendientes; conectar Google/Meta no está listo de fábrica y esta entrega no configura credenciales.
