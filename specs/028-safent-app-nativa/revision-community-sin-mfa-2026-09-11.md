# Community sin MFA — checkpoint de revisión, NO publicable

Este checkpoint conserva el trabajo pendiente para que sea visible en GitHub y
DGX. No representa la retirada completa de MFA ni el rediseño completo de Safent.
No fusionar a la rama publicable ni generar la imagen final hasta cerrar las
pruebas, el resto de autorización y la revisión de aplicación nativa.

## Decisión y cambios

Community no pide MFA; Enterprise conserva su control de seguridad. Retirar el
factor no debe permitir que el agente se apruebe a sí mismo. Se conservan el
HITL, aprobación de un solo uso, aislamiento y enrutamiento corporativo.

Los cambios acumulados incluyen confirmaciones sin TOTP en la UI, cola de
aprobaciones con errores explícitos, eliminación del verificador MFA de la
composición Community y separación de clasificación MFA/ruta Enterprise.

La auditoría detectó que el primer cambio retiraba también la comprobación de
`force=True` al instalar skills. Ahora:

- Una decisión de instalación que eleva permisos requiere el bearer de la UI
  del dueño; el bearer interno del daemon no sirve.
- Solo una respuesta del runtime con `ok: true` permite acuñar `approval_grant`.
- El grant se vincula a aplicación, sesión, identificador y acción, caduca a los
  120 segundos y se consume una sola vez antes de despachar. Un fallo, reinicio
  o resultado de ejecución desconocido obliga a confirmar de nuevo.
- El cliente envía `X-Owner-Approval-Grant`, no un token en el cuerpo. Un rechazo
  `invalid_owner_approval` no refresca sesión ni reintenta la mutación.
- Liberar el freno requiere la sesión del dueño, no MFA ni contraseña del equipo.
  Activar el freno sigue siendo inmediato tras la autenticación normal.
- Eliminado el helper HTTP de contraseña/PAM del freno: no tenía consumidores
  tras retirar ese factor. No se ha eliminado el control PAM de otras funciones.
- Pruebas antiguas de MFA para estas rutas sustituidas por pruebas de dueño,
  token interno rechazado, expiración, uso único y concurrencia; no se han
  desactivado pruebas para ocultar un resultado de la suite completa.

## Evidencia y límites de esta comprobación

- Frontend: 143 pruebas correctas, 26 archivos; `tsc --noEmit` y build correctos.
  Pendiente optimización: chunks de entrada ~975 KB y Swarm ~1.415 KB minificados.
- Backend focalizado FINAL de instalación/freno: 28 pruebas correctas; Ruff
  del nuevo helper y esas pruebas correcto. Los dos routers conservan avisos
  de complejidad por su patrón de funciones anidadas, no declarados resueltos.
- Suite completa INTERMEDIA, antes de sustituir las pruebas antiguas del freno
  e instalación: 5.301 correctas, 33 fallidas, 19 omitidas, 38 deseleccionadas.
  Log DGX `/tmp/safent-runtime-owner-confirmation-tests-20260911.log`.
  Esto NO es una suite completa verde del estado final de este checkpoint.
- Ejecutar backend en DGX con `PYTHONPATH=src python3 -m pytest ...`; el venv del
  checkout canónico no tiene pytest. Confirmar `__file__` para evitar otras copias.
- Frontend en Mac necesita `NODE_OPTIONS=--no-experimental-webstorage` con Node 26.

## Trabajo restante prioritario

1. Terminar la limpieza de tipos/rutas/helpers/configuración MFA en Community;
   siguen referencias heredadas en approvals, policies, egress, tailnet y D-Bus.
   No basta con cambiar expectativas de tests: verificar el control del dueño
   en todas las mutaciones de postura y que Enterprise siga recibiendo su ruta.
2. Revisar fallos restantes de approval_router, hitl_block_and_resume, policies
   y revocación SSH; repetir suite completa sobre el estado final.
3. El grant de instalación vincula el identificador, NO el hash inmutable del
   artefacto. Revisar vínculo servidor scan/target/version y cambios de contenido
   entre revisión e instalación. El runtime acepta metadatos de scan del cliente;
   no declarar cerrada esa frontera por tener un grant de un solo uso.
4. GET kill-switch devuelve `engaged: false` cuando el daemon falla: auditar estado
   desconocido en API/UI para no presentar una protección no comprobada.
5. Validar en aplicación nativa real, además de la vista de pruebas con fixtures.
   Continuar TODA la UI, Ads, Enterprise y backend del alcance integral.

Registro global: `safent-control-enterprise/docs/safent-integral-status.md`.
