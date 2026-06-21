# Runbook — Validar P0–P2 en VM bootc

Lo único que no se puede validar fuera de hardware: el **boot inversion físico**
(P1/G3), el **aislamiento de input** (P1/G5) y la validación en VM. Todo lo demás
está verde en CI. Este runbook lo cierra en la VM.

> Reglas del proyecto: el **build de la imagen (bib/podman, sudo) y el deploy los
> lanzas tú**. Aquí están los comandos exactos; yo no los ejecuto.

## 0. Prerequisitos (en la máquina de build)
- `podman 5+` con buildx, `cosign 2.4+`, `syft`.
- La rama activa mergeada o checked-out.

## 1. Construir la imagen con el código nuevo
```bash
cd ops/agents-os-edition/build
./build-agents-os.sh --profile personal-desktop
# (o el pipeline qcow2 para QEMU)
./build-iso.sh --type qcow2 --profile personal-desktop
```

## 2. Arrancar la VM (test) o instalar en hardware real
```bash
./boot-iso-qemu.sh build/.../agents-os-personal-desktop.qcow2
# Hardware: instalar con build-iso.sh --type anaconda-iso.
```

## 3. Validar (dentro de la VM arrancada)
```bash
sudo HERMES_VM_VALIDATION=1 bash /path/a/ops/agents-os-edition/validation/p0-p3-vm-validation.sh
```
Verifica: G3 (daemon READY antes de GDM, NotifyAccess=main, rescate autenticado),
P2 (allow-list de triggers VACÍA = default-deny), y corre `pytest -m requires_vm`.

## 4. Acceso remoto — túnel named (opt-in)
El túnel a internet requiere que el operador deposite su Cloudflare token:
```bash
echo "<token-de-cloudflared-tunnel>" > /etc/hermes/credentials/cloudflare-tunnel.token
chmod 0600 /etc/hermes/credentials/cloudflare-tunnel.token
chown root:root /etc/hermes/credentials/cloudflare-tunnel.token
systemctl start hermes-remote-tunnel.service
```
Sin token: el servicio NO arranca (ConditionPathExists= falla silenciosamente).
noVNC local (LAN) está disponible siempre en `http://<ip>:6080` vía `hermes-novnc.service`.

## 5. Pruebas de recuperación (manuales, [DESTRUCTIVO])
```bash
# Forzar crash-loop del daemon (StartLimitBurst) → debe caer a hermes-rescue.target, NO brick.
systemctl stop hermes-runtime      # → hermes-rescue.target activo
```
