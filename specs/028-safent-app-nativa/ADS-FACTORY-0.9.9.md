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

## Aceptación del artefacto final — pendiente

Registrar aquí: run IDs y digests OCI exactos; SHA, firma/notarización y contenido del DMG; actualización sobre `/Applications/Safent.app` conservando `.safent`; arranque GUI sin preprovisión manual; cuatro roles Ads ejecutando pins correctos, PostgreSQL sano, MCP y panel integrado sin segundo login; reapertura sin reinstalación. La consulta de actualizador ya se probó en 0.9.8; la instalación entre versiones mediante el actualizador no está certificada.
