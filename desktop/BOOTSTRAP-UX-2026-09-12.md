# Arranque autónomo: UI y contrato de producto

Base: Runtime `0b94f3f`, worktree aislado. Alcance: renderer nativo y sus
artefactos generados, pruebas y documentación. No modifica CLI, Rust, namespace,
instalación, políticas, permisos ni publicación.

Skill aplicada: `emil-design-eng`. La mejora es de comportamiento y claridad,
sin añadir animaciones ni volver a diseñar el loader.

| Before | After | Why |
| --- | --- | --- |
| Fallos no reintentables podían pedir «Vuelve a intentarlo» con botón oculto | Texto y botón siguen la misma autoridad `retryable` | El siguiente paso debe existir y no inducir un bucle |
| Un fallo interno mencionaba otras máquinas; otro culpaba a lo instalado | Mensaje de Safent, sin requisitos externos ni atribución especulativa | La infraestructura privada no es una tarea del usuario |
| Error de conexión pedía cerrar aplicaciones que ocupasen puertos | Reintento permitido o diagnóstico de arranque | Evitar intervenciones técnicas sobre otras aplicaciones |
| Progreso «3 de 5 capas» | «3 de 5 partes» con los mismos contadores reales | Lenguaje de producto sin inventar porcentajes |
| Promesas de reanudar exactamente y de datos intactos | Conexión/reintento/diagnóstico sin garantías no confirmadas | Un mensaje tranquilizador no sustituye evidencia |

## Contrato preservado

- Etiquetas de etapa sólo desde la allowlist de `StageId`; detalles técnicos
  cerrados de forma predeterminada. El snapshot no transporta texto libre.
- Cancelación solicita el comando real con `attempt_id`; no afirma cancelado
  hasta el evento terminal. No cambia el punto de no retorno.
- Reintento manual sólo cuando el runtime lo permite. Sin comandos de terminal,
  instalaciones adicionales ni intervención sobre procesos ajenos.
- Reconexión espera el ciclo nativo existente; no se añade un segundo reparador.
- Exportación conserva el contrato mínimo de diagnóstico, sin stdout, secretos
  ni conversaciones. No se promete recopilar un bundle de soporte completo.
- El namespace privado no necesita campos nuevos para este renderer. La
  recuperación automática y el aislamiento de otras aplicaciones requieren
  validación del CLI/Rust en el carril de runtime; este cambio no los certifica.

## Evidencia

`NODE_OPTIONS=--no-experimental-webstorage npm test --prefix desktop`:
156 PASS, 10 archivos, 775 ms. `npm run typecheck --prefix desktop` y
`npm run build --prefix desktop`: PASS. Artefactos `desktop/ui` regenerados por
el build oficial. Regresiones cubren todos los FailureCode con retry true/false,
fallback desconocido no reintentable y DOM real del loader: preparación,
progreso, fallo bloqueado, detalles cerrados y cero cancel/retry automático.
Las pruebas de replay, cancelación/foco y actualizador anteriores siguen verdes.

No cambia CSS ni dimensiones: no se atribuye una nueva validación visual o un
arranque de binario nativo a estas pruebas de renderer. No se ejecutó Safent.
