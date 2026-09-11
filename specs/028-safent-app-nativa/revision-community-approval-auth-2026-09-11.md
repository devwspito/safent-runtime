# Community — retirar MFA de la cadena de aprobación

Decisión del propietario: MFA sólo Enterprise. Este corte elimina contratos
residuales de aprobación local, **no** los controles de identidad, la jaula,
los permisos ni las decisiones empresariales firmadas.

## Implementación

- HTTP acepta únicamente `decision: once|deny`, sin `totp`, factores ni identidad
  del aprobador. Campos adicionales se rechazan con 422, antes de resolver.
- Retirados cuerpos muertos de enrolamiento/acertijos y la dependencia de
  `MfaStore`. La API no publica rutas de estado/enrolamiento MFA.
- D-Bus `Approve` pasa de `ss` a `s`: sólo `proposal_id`. Actualizados adaptadores
  del shell, compositor y terminal, y contrato de introspección probado.
- Gate/puertos/servicio local ya no aceptan `mfa_factors` ni `mfa_verifier`.
  El gate sigue prohibiendo aprobar localmente una propuesta Enterprise, y
  conserva autoría, token firmado ligado a propuesta, anti-replay y auditoría.
- Retirados `security/mfa.py` y `mfa_tool_tier.py`, sin consumidores de producto.
  Los ficheros de secretos/historia de una instalación antigua **no se borran**.
  El código retirado puede recuperarse de Git; no se migra ninguna instalación.
- Errores locales por defecto son `proposal_invalid`, no un MFA ficticio. La
  traducción genérica de errores D-Bus sigue preservando motivos estructurados.
- Terminal deja de exigir un código inexistente. Sigue requiriendo una decisión
  explícita; Escape no aprueba. No se afirma que una aprobación sea ejecución.

## Diseño

| Before | After | Why |
| --- | --- | --- |
| Terminal impedía aprobar sin código MFA aunque el motor ya no lo verificaba. | Revisión de la propuesta y botón explícito, sin factor local. | Misma autoridad de propietario que Community, sin obstáculo ficticio. |
| Error genérico invitaba a configurar MFA/acertijos inexistentes. | Error de aprobación y revisión del estado; Enterprise conserva su mensaje propio. | No enviar al usuario a un flujo que no existe ni ocultar la autoridad empresarial. |

No animaciones nuevas; el diálogo conserva sus gestos existentes. No es una
revisión visual de toda la terminal ni de toda la app nativa.

## Verificación

- Foco de aprobación/Enterprise/introspección D-Bus: **72 PASS**.
- Seguridad UID/proxy, errores estructurados, contratos y terminal: **121 PASS**;
  aviso previo por marcador pytest `security` no registrado.
- Terminal final con prueba explícita sin campo MFA: **12 PASS** (Textual Pilot,
  bridge aislado, no bus real ni sistema operativo del cliente).
- Suite completa runtime: **5728 PASS, 19 SKIP, 64 deselected**, 241.90s.
  Una prueba menos que el corte anterior corresponde al store TOTP retirado;
  su ensayo se sustituye por controles del token real de aprobación en seguridad.
  Los SKIP incluyen gitleaks ausente, gate de clave de release y Composio del
  host incompatible: siguen siendo bloqueos para certificar la imagen final.
- `git diff --check` correcto. Ruff del nuevo test limpio; los módulos grandes
  preexistentes aún tienen incidencias fuera del diff, no se declara lint global
  verde ni se aplican correcciones masivas para esconderlas.

Scratch DGX `/tmp/safent-community-auth.MWyLY8`, Python3.12/pytest9 del host:
`PYTHONPATH=src python3 -m pytest tests/unit tests/tasks -q -rs --tb=short`.
Log `community-auth-full.log`. El snapshot usa managed_llm/association_store
commiteados, no el WIP paralelo de locks; esa prueba tiene su propio informe.

## Fronteras pendientes

- El cambio D-Bus no conserva compatibilidad con clientes de dos argumentos:
  runtime y shell deben actualizarse juntos en la imagen final. Un cliente
  anterior recibe error; no hay fallback que altere autoridad.
- Enterprise MFA no se elimina: sus sesiones y step-up son de otro repositorio.
  Regresiones Enterprise-routed y remote_approvals se mantienen en runtime.
- Los helpers frontend MFA muertos se retiran en el corte paralelo UI-CONFIG.
- Quedan nombres de prueba/documentación histórica y la consulta constante
  `is_mfa_required=False`; no verifican ni enrolan factores. Los puertos antiguos
  de supervisor/telemetría aún usan el nombre `totp_validated`: requieren auditoría
  de su autorización y wiring antes de cambiar su contrato, no borrar el check.
- No tocar enumeraciones de evidencia TOTP histórica ni la prohibición de rutas
  de login del companion Ads: son límites/historia, no MFA Community operativo.
- No se desplegó imagen ni se certificó upgrade macOS/Windows con este corte.
