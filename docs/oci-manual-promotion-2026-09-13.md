# Promoción manual de OCI después de aceptación

Los workflows de publicación construyen las dos arquitecturas y esperan a ambas
antes de publicar el índice candidato. Ya no actualizan `:latest` automáticamente.
Se conservan los tags de versión y, donde ya existía, el tag SHA. El launcher nativo
continúa usando los digests del bundle firmado, no este alias mutable.

Este cambio no retira ni modifica ningún `latest` publicado anteriormente. Tampoco
reconstruye imágenes ni mueve tags existentes. Los artefactos Runtime 0.9.9 y Ads
0.2.5 conservan sus SHA de origen anteriores a este cambio de CI.

## Procedimiento reservado al operador

1. Registrar versión, commit de origen, digest del índice y digests hijos amd64 y
   arm64. Comprobar etiquetas OCI, firmas/provenance disponibles y que ambos hijos
   pertenecen al candidato aprobado; nunca deducir aprobación desde un tag.
2. Completar aceptación del bundle final: instalación/actualización nativa,
   conservación de datos, health real de core y Ads con imágenes esperadas,
   autenticación del panel y reapertura idempotente. Registrar evidencia y decisión
   humana. Un build verde o estas pruebas estáticas no sustituyen esa aceptación.
3. Con autorización explícita para promover, autenticarse con permisos del operador,
   comprobar que ninguna otra promoción está en curso y conservar el digest anterior
   de `latest` en el registro de release. Elegir el repositorio exacto:
   `ghcr.io/devwspito/safent` o `ghcr.io/devwspito/safent-ads`.
4. Copiar únicamente el índice aprobado por digest, sin resolver de nuevo un tag de
   versión ni construir imágenes. Ejemplo deliberadamente incompleto (sustituir los
   valores con la evidencia aprobada, no ejecutarlo como parte de CI):

   ```sh
   image='REPOSITORIO_EXACTO_APROBADO'
   approved_index='sha256:DIGEST_INDICE_APROBADO'
   docker buildx imagetools inspect "$image@$approved_index"
   docker buildx imagetools create --tag "$image:latest" "$image@$approved_index"
   docker buildx imagetools inspect "$image:latest"
   ```

5. Verificar que el digest de `latest` coincide exactamente con el índice aprobado
   y que sus dos hijos no cambiaron; registrar el resultado. Una discrepancia falla
   la promoción: no declarar éxito ni sobrescribir tags de versión. No actualizar
   `latest.json`, tags Git ni bundles firmados mediante este procedimiento.

No se ha ejecutado ninguna promoción con este cambio. Las pruebas comprueban la
ausencia de `:latest` en ambos workflows y preservan la barrera multiarquitectura,
los identificadores de ejecución y los tags de versión/SHA existentes.
