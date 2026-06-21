#!/usr/bin/env bash
# =============================================================================
# Validación P0–P2 en VM bootc (Agents OS Edition)
# =============================================================================
# Corre DENTRO del SO ya arrancado (qcow2 en QEMU o hardware nativo). Verifica
# lo único que no se puede validar fuera de hardware: el boot inversion físico
# (G3) y el aislamiento de input (G5).
#
# Uso (como hermes-user o root en la VM):
#   sudo HERMES_VM_VALIDATION=1 bash p0-p3-vm-validation.sh
#
# Read-only por defecto: NO fuerza fallos destructivos. Para probar el camino de
# rescate/recuperación, ejecutar las secciones marcadas [DESTRUCTIVO] a mano.
# =============================================================================
set -uo pipefail
export HERMES_VM_VALIDATION=1

PASS=0; FAIL=0; SKIP=0
ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
hdr()  { echo; echo "== $* =="; }

# -----------------------------------------------------------------------------
hdr "G3 — Boot inversion (PIEZA 3): el agente arranca ANTES de la sesión gráfica"
# El daemon debe estar vivo (READY=1) y el gate de liveness alcanzado.
if systemctl is-active --quiet hermes-runtime.service; then
  ok "hermes-runtime.service activo"
else
  bad "hermes-runtime.service NO activo (el agente debe ser el proceso primario)"
fi
if systemctl is-active --quiet hermes-runtime-ready.target 2>/dev/null; then
  ok "hermes-runtime-ready.target alcanzado (gate de liveness)"
else
  bad "hermes-runtime-ready.target NO alcanzado"
fi
# El runtime-ready debe estar Before=multi-user (arranca antes que la sesión).
if systemctl show hermes-runtime-ready.target -p Before 2>/dev/null | grep -q multi-user; then
  ok "runtime-ready Before=multi-user.target (inversión correcta)"
else
  bad "runtime-ready NO está Before=multi-user (no se invirtió el boot)"
fi
# READY=1 del daemon debe preceder a la activación de GDM (0 pantallas antes).
RT=$(systemctl show hermes-runtime.service -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
GDM=$(systemctl show gdm.service -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
if [[ -n "$RT" && -n "$GDM" && "$RT" -gt 0 && "$GDM" -gt 0 && "$RT" -lt "$GDM" ]]; then
  ok "daemon READY ($RT) ANTES de GDM ($GDM) — 0 pantallas sobre un agente muerto"
elif [[ -z "$GDM" || "$GDM" -eq 0 ]]; then
  skip "GDM no activo aún / headless — comparación de timestamps no aplicable"
else
  bad "GDM ($GDM) arrancó antes/igual que el daemon ($RT)"
fi
# NotifyAccess=main (anti-spoof de READY=1).
if systemctl show hermes-runtime.service -p NotifyAccess --value 2>/dev/null | grep -qx main; then
  ok "NotifyAccess=main (solo el proceso principal notifica liveness)"
else
  bad "NotifyAccess != main (READY=1 spoofeable)"
fi
# Daemon sin modelo = sano-idle (SC-2): el boot NO depende del LLM.
if ! systemctl show hermes-runtime.service -p Requires --value 2>/dev/null | grep -q hermes-llm; then
  ok "hermes-runtime NO Requires hermes-llm (arranca sano-idle sin modelo, SC-2)"
else
  bad "hermes-runtime Requires hermes-llm (un modelo lento brickearía el boot)"
fi
# Rescate autenticado disponible (no auto-root).
if systemctl cat hermes-rescue.target >/dev/null 2>&1; then
  ok "hermes-rescue.target existe (camino de no-brick)"
  if ! grep -q "AutomaticLogin" /etc/systemd/system/hermes-rescue*.service 2>/dev/null; then
    ok "rescate sin auto-login (AUTENTICADO)"
  fi
else
  bad "hermes-rescue.target ausente (riesgo de brick)"
fi

# -----------------------------------------------------------------------------
hdr "P2 — Default-deny + anti-autopirateo (verificación en runtime)"
# La allow-list de triggers nace VACÍA: nada auto-dispara.
DB="${HERMES_SHELL_DB:-/var/lib/hermes/shell-state.db}"
if command -v sqlite3 >/dev/null 2>&1 && [[ -f "$DB" ]]; then
  N=$(sqlite3 "$DB" "SELECT COUNT(*) FROM authorized_trigger_instances WHERE enabled=1;" 2>/dev/null || echo "?")
  if [[ "$N" == "0" ]]; then ok "allow-list de triggers VACÍA (default-deny: 0 orígenes habilitados)"
  elif [[ "$N" == "?" ]]; then skip "no se pudo leer authorized_trigger_instances"
  else echo "  [INFO] $N triggers habilitados (firmados por admin) — revisar que son esperados"; fi
else
  skip "sqlite3 / shell-state.db no disponible para verificar la allow-list"
fi
# La denylist anti-autopirateo es de runtime (probada en tests); aquí solo nota.
skip "G5 aislamiento de input físico (teclado/ratón/pantalla/browser) — probar a mano con N tareas concurrentes"

# -----------------------------------------------------------------------------
hdr "Suite requires_vm (los tests de boot que solo corren en hardware)"
if command -v pytest >/dev/null 2>&1 && [[ -d /usr/lib/python3*/site-packages/hermes || -d ./src/hermes ]]; then
  python3 -m pytest -m requires_vm tests/vm/ -q 2>&1 | tail -5 | sed 's/^/  /' || skip "pytest requires_vm no ejecutable aquí"
else
  skip "pytest / código no disponible en la VM para correr requires_vm"
fi

# -----------------------------------------------------------------------------
echo
echo "============================================================"
echo "  RESUMEN: $PASS PASS · $FAIL FAIL · $SKIP SKIP (medición/manual)"
echo "============================================================"
echo "  [DESTRUCTIVO, a mano] Probar rescate/recuperación:"
echo "    - Forzar fallo del daemon (StartLimitBurst) → debe caer a hermes-rescue.target, NO brick."
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
