# Revisión global — 13 de septiembre de 2026

No equiparar una compilación nativa correcta con la finalización de todo Safent. Este resumen contrasta los documentos vigentes y separa las nuevas pruebas indicadas abajo de los pendientes de despliegue. La referencia de negocio es `lumen-control-enterprise/CIERRE-2026-09-12.md` (HEAD auditado `e4996a5`), que sustituye `SAFENT-PENDIENTES.md`. El resultado del nuevo paquete nativo se registra por separado en `ADS-FACTORY-0.9.10.md`; 0.9.9 falló aceptación GUI y no se promovió.

| Frente | Pendiente verificado en la documentación vigente |
|---|---|
| Community nativa | DMG 0.9.10 recupera Ads y reabre con los mismos IDs/puerto; pins, proyección privada y MCP comprobados en el Mac. Falta cerrar primera configuración funcional Ads, permisos del SO, OAuth real, En vivo/VNC y todos los recorridos nativos. Instalación mediante updater entre versiones aún no acreditada. |
| Enterprise/Friendog | Despliegue cloud real, dominio/TLS, IAM, digests/secretos, carga en VM pequeña, reinicio y backup externo. Lo documentado acredita pruebas offline/restauración PostgreSQL aislada. |
| Google/Meta | Login y consentimiento reales del propietario. Meta loopback HTTP con puerto dinámico no certificado. OAuth Ads no concede IAM/GCP/GTM/Analytics. Enterprise tiene inventario Analytics/GTM, no provisión GCP genérica ni edición/publicación GTM o informes GA completos. |
| Ads/autonomía | Defecto descubierto en primera entrada real: falta alta de negocio con base vacía, en corrección mediante UI/API. Operación administrada sostenida entre instancias y cuentas reales aún pendiente. No existe cobertura universal de formatos/ediciones publicitarias; faltan keywords/geotargeting Google y upload/attach de creativos nuevos. |
| CRM | Validar con CRM real. El corte actual ofrece lecturas GET gobernadas; escrituras genéricas, webhooks y SSH no implementados. |
| Folder/conocimiento | GCS/IAM reales, PDF/OCR, búsqueda vectorial/híbrida y escala. Hoy texto/Markdown acotado, búsqueda literal y citas. Subir un archivo no equivale a indexarlo; no hay purga automática de historial/intenciones. |
| Equipo/Tareas | Recorrido desplegado entre instancias con UI/D-Bus/red reales y efecto de herramienta aprobado dentro del mismo encargo. Admisión incierta requiere revisión de evidencia; no replay automático. |
| LLM heredado | La admisión funcional sí está integrada (el plan antiguo estaba desactualizado). Falta certificación de imagen KVM/systemd/Landlock del flujo gestionado y cobertura universal de modelos/multimodal. Credenciales antes distribuidas requieren rotación, no supuesto borrado retroactivo. |
| Auditoría global | Enterprise y Runtime tienen nuevas suites completas ejecutables verdes, detalladas abajo. Se conservan los fallos y límites de la ejecución inicial en la evidencia. No confundir estas suites con aceptación cloud/proveedores reales. |

## Nuevas comprobaciones ejecutadas

- Enterprise: snapshot `e4996a569f4998f6e81ee1bad83a946b932ccc57` + Runtime `53b16a3e14c5d38310fd16eba908d3e7abcdbec3`; backend **1681 PASS, 11 SKIP, 0 FAIL/ERROR**, 641.02 s. Los skips son variantes SQLite de carreras multiproceso; sus equivalentes PostgreSQL 16 reales sí pasan. También se ejecutaron los opt-in de Compose y backup/restore. No se ocultan las 504 advertencias de deprecación. Frontend **332 PASS**, build/TypeScript aprobados, lint sin errores y nueve advertencias históricas. Ruff y mypy aprobados; auditorías de dependencias sin vulnerabilidades conocidas en el corte analizado. Evidencia DGX `/tmp/safent-ee-global-20260913.3s96y4`.
- Runtime: primera batería base **7472 PASS, 17 FAIL, 25 SKIP, 250 deselected**, 464.16 s. 16 fallos por fixture del actualizador sin `INSTANCE_DIR` aislado; uno por doble de runtime que no respondía a la nueva inspección de identidad del volumen. Tras corregir sólo esas fixtures, **7493 PASS, 0 FAIL, 21 SKIP, 250 deselected**, 441.09 s: snapshot `a6a407d` + dos archivos de pruebas. La ruta explícita del repo Ads habilita cuatro contratos que antes se omitían. No se borran ni relajan las comprobaciones de red/proyección privada, ni se cambiaron funciones de producto. Las 250 exclusiones corresponden a marcadores opt-in de la configuración base; no son cobertura acreditada. Nueve pruebas de SDK pasaron además aisladas; no se suman a la cifra global. Evidencia DGX `/tmp/safent-python-a6a407d.9cCsWE/backend-full-after.log`.
- Instalación: reparación de reservas de red aprobada con Podman real aislado y con Bash 3.2 del Mac; luego el DMG 0.9.10 recuperó realmente Ads desde GUI, llegó al chat y reabrió conservando IDs y puerto. Se comprobaron pins por contenido, DB, proyección privada y 64 herramientas MCP. La consulta del actualizador nativo 0.9.9 se verificó desde su botón y respondió correctamente contra el canal estable; no acredita todavía instalación entre versiones. El hallazgo posterior de alta de negocio impide declarar cerrado todo Anuncios.

Cambios de infraestructura de pruebas: Enterprise `5965a51bda43301a2db61d1e08a4ec48550dcf25` añade frontend, PostgreSQL y contratos Runtime a CI con pins explícitos; revisión independiente sin objeciones. CI hospedado aún no ejecutado. Runtime `1b9c8058762821de2d1f1de673860dfc17e895e6` conserva el informe global y las dos fixtures corregidas.

## Fuentes principales

Runtime, bajo `specs/028-safent-app-nativa/`: `inventario-ui-community.md`, `revision-community-crm-2026-09-12.md`, `revision-task-ce-native-roundtrip-2026-09-12.md`, `revision-llm-functional-admission-2026-09-12.md`, `ADS-FACTORY-0.9.9.md`.

Runtime, bajo `docs/`: `runtime-final-regression-2026-09-12.md`, `managed-ads-child-proposal-2026-09-12.md`, `oauth-callback-hardening-2026-09-13.md`.

Enterprise: `CIERRE-2026-09-12.md`, `docs/deploy-vm-2026-09-12.md`, `docs/google-connections-01-2026-09-12.md`, `docs/knowledge-01-2026-09-12.md`, `docs/files-knowledge-mcp-2026-09-12.md`, `docs/enterprise-final-regression-2026-09-12.md`.

La lectura documental no identificó un nuevo bug crítico de código. Sí confirma pendientes funcionales y de aceptación que no quedan cerrados por publicar el DMG. No se modificaron campañas, presupuestos ni permisos de proveedores durante esta revisión.
