# Safent 0.9.9 / Ads 0.2.5 — recuperación idempotente y aceptación nativa

Versión candidata. No declarar entrega estable hasta registrar aceptación GUI real al final de este documento. Conserva todos los datos existentes; no requiere limpiar el Mac ni instalar Podman por separado.

## Causa y corrección

0.9.8 verificaba correctamente los IDs de las imágenes en ejecución y por eso detectó Ads antiguo. Sin embargo, al reparar encontraba un volumen privado existente y ejecutaba `podman volume create` incondicionalmente. El Podman privado real devuelve 125 en ese caso; el scaffold abortaba antes de publicar la proyección. La reproducción se hizo sólo con un volumen QA con nombre UUID, no con el de producción.

La provisión ahora inspecciona el volumen y exige driver local, ninguna opción de montaje y el label exacto de Safent. Sólo lo crea si falta y vuelve a validar la identidad después, incluida una carrera con otro creador. No adopta volúmenes extraños ni usa `--ignore` específico de Podman que rompería Docker. La proyección usa un directorio temporal único para no reabrir restos de ficheros 0400. El registro del host usa `mktemp` y rename, sin reabrir el `.tmp` fijo 0444 de un intento interrumpido.

Se mantienen la proyección root-owned, los secretos 0400, el montaje de sólo lectura hacia el core, el helper sin red y sin capabilities y la comprobación de imágenes por contenido. No se amplían permisos del agente ni se borran credenciales, base de datos o volúmenes reales.

## Conexión a proveedores

- Ads 0.2.5 normaliza el MCC oficial `123-456-7890` a `1234567890`; rechaza formatos ambiguos y caracteres ajenos. Fuente Ads `7920a2da01ecfccd0704b908bf15978f222177e9`; cambio funcional `1d2d31c`.
- La app permite abrir exclusivamente el enlace de ayuda `https://console.cloud.google.com/google/ads-apis/overview` en el navegador externo. No permite navegar a él dentro del webview ni habilita otras rutas, parámetros o dominios. Las validaciones OAuth siguen intactas.
- Google requiere un cliente OAuth de escritorio y consentimiento con acceso a Ads. Meta requiere configuración de su app y consentimiento; el callback HTTP local con puerto dinámico de Meta no está certificado sin comprobar la configuración real. No se distribuye un secreto maestro de Meta en Community.
- La sesión de Friendog Google exige reautenticación y Facebook pide login. No se han usado otras cuentas, extraído secretos del navegador ni modificado campañas/presupuestos. Sin consentimiento real no afirmar que están conectados.

## Publicación segura

Pipeline `1f87bf34a580e9138f5574a7816d5f20cffc017e`: todos los builds desktop se publican como prerelease, nunca latest automáticamente. El helper exige candidato con identidad/tag/SHA únicos y el final actualiza por ID con `make_latest=false`. Sólo una aceptación GUI permite una promoción posterior y explícita. Pruebas: 92 aprobadas, 3 skips de herramientas Apple ausentes en Linux.

## Evidencia previa a build

- Ads completo: 3429 pruebas unitarias aprobadas, sin skips, incluida consistencia de las tres fuentes de versión; mypy y ruff aprobados. Los tests no equivalen a acceso real a Google/Meta.
- Política nativa de ventanas: 10 pruebas focales y clippy `-D warnings` aprobados; aceptación del enlace exacto y rechazo de variantes/otras rutas.
- Runtime: suite Rust completa aprobada; consistencia de versiones 2/2; provisión y CLI juntas 121/121 sobre las fuentes finales; una segunda ejecución independiente de CLI 35/35. No sumar conjuntos solapados. Una ejecución anterior coincidió con el cambio de fixture y falló por no devolver la identidad del volumen; no representa el resultado de las fuentes finales.
- Podman real incluido: dos pruebas sobre recursos QA efímeros, con publicación repetida, residuo `.next` 0400, comprobación de contenido/permisos y rechazo de label ajeno/ausente. Todos los volúmenes QA retirados tras validar identidad; producción no modificada.
- Repetición del mismo arnés con el core real 0.9.8 y con Ads 0.2.4: ambas ejecuciones aprobaron sus dos tests (5,602 s y 4,942 s respectivamente). No se sustituyó el runtime por un doble.
- Frontend Community del snapshot exacto `dbf4587`: 359/359 tests, typecheck y build aprobados. Renderer desktop: 155 aprobados, 1 skip por ausencia de `.git` en `git archive`; typecheck/build aprobados. Ambas instalaciones usaron `npm ci` con sus lockfiles y Node 24.13.1 en scratch, sin alterar dependencias canónicas. No equivale a QA gráfica nativa.

## Construcciones exactas

- Runtime fuente/tag `dbf4587e2b99006e1ae714c98468cb2e86d57b4f` / `v0.9.9`, run `34737558621` aprobado. Índice `sha256:14d237d51a97f5689f50dd2a946ffdd03f3886bc0c39a0ddf13890ec9469b5cd`; amd64 `sha256:85d61368ebf5c82def6e597c6a4f0bbdbf956ff6e12a79666626328e464958df`; arm64 `sha256:dfcdb5568176956ce9a417c8664097bf1e4b9dafe00acc3a5e01bd1e8aea226f`.
- Ads fuente/tag `7920a2da01ecfccd0704b908bf15978f222177e9` / `v0.2.5`, run `34737392400` aprobado. Índice `sha256:99cbe593fc0f24f1a600ab6f97d8e06bfb3652a1b0519b76ef77f39383f0a480`; amd64 `sha256:ecc3e0975ffaed3e9e21b70a20da72a14d8c226cd2ddd0ea9e3de5e786d57039`; arm64 `sha256:2f34a502f5f6bea8569da3d96450f636cfa6750356290173bda673cc8f7cdc1e`.
- Desktop run `34738041864`, source pipeline `1f87bf34a580e9138f5574a7816d5f20cffc017e`, ref exacta runtime `dbf4587`: todos los gates aprobaron. Release candidata única `387791934`, 15 assets/14 SHA256SUMS concordantes, manifiesto minisign válido y tres plataformas de updater verificadas independientemente. Sin PKG, Windows ni Intel Mac. La release estable seguía siendo 0.9.5; esta candidata no se promovió.
- Acceso anónimo independiente a ambos OCI: índice, manifiesto ARM64 y config GET 200 con hashes verificados, HEAD de una capa real 200. Tokens públicos sólo en memoria, sin PAT/cookies/auth Docker/proxy del entorno. No se descargaron todas las capas en esa comprobación y no sustituye una instalación completa de máquina vacía.

## Aceptación del artefacto final — NO aprobada

DMG final `Safent_0.9.9_aarch64.dmg`, asset `560568021`, 1.016.790.405 bytes, SHA256 `896f6aafa3edc80992f7a50408c4bf59e627a76471dd9a9db852a658ef3e32e0`. Descarga local coincide, `codesign --verify --deep --strict`, `spctl` (Notarized Developer ID) y `stapler validate` aprueban. Pins y modos de 28 recursos verificados. Los hashes de los ocho Mach-O re-firmados cambian después de generar el manifiesto: se verifica su sello Apple y el de la app, tal como prescribe el código; no afirmar que sus SHA previos a firma coinciden byte a byte.

Instalado en `/Applications/Safent.app` preservando copia de 0.9.8 y todos los datos. El scaffold ahora sí se ejecuta repetidamente; se descargan ambas imágenes, core arranca con digest correcto y la migración Ads termina con código 0. Broker y worker ejecutan Ads 0.2.5, pero API queda `Created`. Error real del engine: la IP fija `10.201.0.10` ya estaba asignada dinámicamente al broker (ID `19f23afb2bcafe70a4aa8d22e99b242b6022de050316a7451b49b8f44fd42c47`). Inventario observado: core `.9`, DB `.11`, broker `.10`, worker `.13`; API sin IP. No se declaró éxito ni se reparó manualmente ese estado para maquillar la prueba.

Esto requiere una corrección de asignación de red y recuperación de ocupantes propios en la siguiente versión. 0.9.9 permanece candidata. No está acreditado MCP/panel final ni reapertura. También se encontró un defecto visual: Preflight emitía inicio pero no finalización; ese arreglo va en el wrapper siguiente, no en este artefacto inmutable.

La consulta del actualizador ya se probó en 0.9.8; la instalación entre versiones mediante el actualizador no está certificada. Google/Meta siguen pendientes de login y consentimiento reales.
