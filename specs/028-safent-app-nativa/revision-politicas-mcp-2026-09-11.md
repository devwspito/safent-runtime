# Políticas y aprobación MCP — 2026-09-11

Bloque de seguridad implementado en rama de revisión. No certifica el runtime
completo ni publica imagen. Community mantiene confirmación humana sin MFA.

## Fallos corregidos

- Archivo de políticas corrupto/ilegible ya no se interpreta como configuración
  inicial permisiva. Deniega herramientas (también overlays Enterprise), mantiene
  aprobación de peligros y rechaza mutaciones que sobrescribirían las restricciones.
  Solo la ausencia inicial del archivo recibe los valores por defecto; un symlink
  roto no se interpreta como instalación nueva.
- Validación estricta de preset, overrides y booleano de aprobación: ni `"false"`
  ni `0` se convierten implícitamente en una decisión del dueño.
- Snapshot y decisión individual leen una sola versión coherente del archivo.
- API devuelve 503 `policy_unavailable`, sin contenido de archivo; UI elimina el
  fallback inventado «equilibrado» y presenta error/reintento sin controles activos.
- `force=True` para MCP normal y managed-remote requiere un grant de dueño de un
  solo uso, ligado a la sesión y hash de operación exacta. Para MCP local incluye
  servidor, label normalizado, argv y env; para managed incluye slug y URL.
  Cambiar cualquier campo invalida la confirmación. Verificación ocurre ANTES
  de cambiar el endpoint remoto. El hash no expone secretos env.
- El grant se emite únicamente después de registrar la aprobación con éxito.
  El token interno no puede emitirlo ni usarlo. La UI transporta el grant por
  cabecera y no reintenta una operación con un grant rechazado.
- Managed remoto conserva la URL que originó la revisión, aunque se edite el
  formulario después. Instalación normal sin override conserva el scan del daemon.

## Verificación focalizada

- 111 pruebas de políticas/catálogo/overlays/API/concurrencia correctas.
- 56 pruebas MCP normal/managed y grants skill correctas, incluyendo rechazo antes
  de efectos, replay, cambio de campos y actor incorrecto.
- Ruff de los módulos cambiados y pruebas nuevas correcto.
- Frontend compartido: 154 pruebas / 30 archivos y TypeScript/Vite correctos antes
  de añadir las dos pruebas de transporte MCP; se repetirá al integrar los agentes.
- Suite completa runtime `39f4cea` en DGX: **5.411 correctas, 19 omitidas,
  38 deseleccionadas y 4 avisos**, 179,86 s, con `PYTHONPATH=src`, no contra el
  paquete instalado global del host. Registro: `/tmp/safent-runtime-parallel-security-tests-20260911.log`.

## Límites / siguientes pasos

- La aprobación de scan y la caché ALLOWED del daemon siguen identificando un
  target; falta vínculo de artefacto inmutable y auditoría end-to-end de otros
  caminos D-Bus/nativos/config-sync. No confundir el grant REST con eliminación
  de todos los riesgos TOCTOU o una aprobación criptográfica del paquete binario.
- Recuperación de archivo de políticas dañado requiere restauración verificada;
  no existe botón UI que lo sustituya automáticamente por permisos por defecto.
- Seguimiento posterior: API del freno devuelve 503 ante daemon ausente o
  estado malformado, y cliente propaga ese fallo. La UI muestra estado desconocido
  con reintento, no «Todo en marcha»; permite intentar activar el freno, pero
  no liberarlo desde un estado desconocido. Esto no cambia el estado real del daemon.
- No se han desplegado estos cambios ni modificado credenciales reales.
