# Revisión global — 13 de septiembre de 2026

No equiparar una compilación nativa correcta con la finalización de todo Safent. Este resumen contrasta los documentos vigentes; no es una nueva certificación de cada módulo. La referencia de negocio más reciente es `lumen-control-enterprise/CIERRE-2026-09-12.md` (HEAD auditado `e4996a5`), que sustituye `SAFENT-PENDIENTES.md`. El resultado del paquete nativo se registra por separado en `ADS-FACTORY-0.9.9.md`.

| Frente | Pendiente verificado en la documentación vigente |
|---|---|
| Community nativa | Aceptación GUI del DMG final, Ads con pins correctos y reapertura. La revisión web no certifica permisos del SO, OAuth real, En vivo/VNC ni todos los recorridos nativos. Instalación mediante updater entre versiones aún no acreditada. |
| Enterprise/Friendog | Despliegue cloud real, dominio/TLS, IAM, digests/secretos, carga en VM pequeña, reinicio y backup externo. Lo documentado acredita pruebas offline/restauración PostgreSQL aislada. |
| Google/Meta | Login y consentimiento reales del propietario. Meta loopback HTTP con puerto dinámico no certificado. OAuth Ads no concede IAM/GCP/GTM/Analytics. Enterprise tiene inventario Analytics/GTM, no provisión GCP genérica ni edición/publicación GTM o informes GA completos. |
| Ads/autonomía | Operación administrada sostenida entre instancias y cuentas reales; recuperación continua. No existe cobertura universal de formatos/ediciones publicitarias; faltan keywords/geotargeting Google y upload/attach de creativos nuevos. |
| CRM | Validar con CRM real. El corte actual ofrece lecturas GET gobernadas; escrituras genéricas, webhooks y SSH no implementados. |
| Folder/conocimiento | GCS/IAM reales, PDF/OCR, búsqueda vectorial/híbrida y escala. Hoy texto/Markdown acotado, búsqueda literal y citas. Subir un archivo no equivale a indexarlo; no hay purga automática de historial/intenciones. |
| Equipo/Tareas | Recorrido desplegado entre instancias con UI/D-Bus/red reales y efecto de herramienta aprobado dentro del mismo encargo. Admisión incierta requiere revisión de evidencia; no replay automático. |
| LLM heredado | La admisión funcional sí está integrada (el plan antiguo estaba desactualizado). Falta certificación de imagen KVM/systemd/Landlock del flujo gestionado y cobertura universal de modelos/multimodal. Credenciales antes distribuidas requieren rotación, no supuesto borrado retroactivo. |
| Auditoría global | Los cierres históricos registran fallos corregidos mediante pruebas focales, no una segunda suite global Runtime/Enterprise completamente verde. Las suites actuales acreditan sus cortes, no todos los módulos. |

## Fuentes principales

Runtime, bajo `specs/028-safent-app-nativa/`: `inventario-ui-community.md`, `revision-community-crm-2026-09-12.md`, `revision-task-ce-native-roundtrip-2026-09-12.md`, `revision-llm-functional-admission-2026-09-12.md`, `ADS-FACTORY-0.9.9.md`.

Runtime, bajo `docs/`: `runtime-final-regression-2026-09-12.md`, `managed-ads-child-proposal-2026-09-12.md`, `oauth-callback-hardening-2026-09-13.md`.

Enterprise: `CIERRE-2026-09-12.md`, `docs/deploy-vm-2026-09-12.md`, `docs/google-connections-01-2026-09-12.md`, `docs/knowledge-01-2026-09-12.md`, `docs/files-knowledge-mcp-2026-09-12.md`, `docs/enterprise-final-regression-2026-09-12.md`.

La lectura documental no identificó un nuevo bug crítico de código. Sí confirma pendientes funcionales y de aceptación que no quedan cerrados por publicar el DMG. No se modificaron campañas, presupuestos ni permisos de proveedores durante esta revisión.
