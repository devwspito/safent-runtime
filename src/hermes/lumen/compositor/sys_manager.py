"""SysManager — puente Python↔QML que expone capacidades del SO al desktop.

Reemplaza el C++ `sysManager` de WhaleOS. El qcow2 ES una Fedora completa (tiene
mkdir, timedatectl, lspci, useradd, xdg-open…); este objeto es el PEGAMENTO que
el QML usa para invocarlas. El QML llama 28 métodos: si falta UNO, la llamada
lanza "is not a function" en JS y ABORTA la función QML (así se rompía el envío
del chat → sysManager.createDir). Por eso TODOS existen y capturan sus errores:
degradan, pero NUNCA lanzan al QML.

Ops lentas/de SO corren en hilo y devuelven por señal (conexión encolada de Qt,
thread-safe hacia el hilo GUI). Las que requieren root (useradd/chpasswd/
timedatectl set-*) se intentan y devuelven resultado HONESTO (success+detail);
no fingen éxito.
"""
from __future__ import annotations

import json
import logging
import os
import pwd
import shlex
import shutil
import subprocess
import threading

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)


class SysManager(QObject):
    # Señales async (el QML conecta onTimeInfoReady/onUserOpResult/…).
    timeInfoReady = Signal(str, str, str, str, str)   # tz, ntpSync, ntpActive, localTime, utcTime
    timezonesReady = Signal(str)                       # lista \n-separada
    displayInfoReady = Signal(str)                     # texto tipo xrandr (best-effort)
    gpuInfoReady = Signal(str)                         # línea de GPU
    authResult = Signal(bool)                          # login (bypass en el SO)
    userOpResult = Signal(str, bool, str)              # operation, success, detail
    timeOpResult = Signal(str, bool, str)              # operation, success, detail

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _async(fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    @staticmethod
    def _sh(cmd: str, cwd: str | None = None, timeout: int = 8) -> tuple[bool, str]:
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,  # noqa: S602
                               cwd=cwd or None, timeout=timeout)
            out = (r.stdout or "") + (r.stderr or "")
            return r.returncode == 0, out.strip()
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)

    # ── Apps nativas / ficheros / portapapeles ──────────────────────────
    # Señal emitida cuando un binario nativo falla al arrancar (binario ausente,
    # crash inmediato, etc.). El QML AppWindow la escucha y muestra un toast con
    # el motivo real en vez del "Launching..." infinito que enmascara el bug.
    appLaunchFailed = Signal(str, str)  # (cmd, reason)

    @Slot(str)
    def launchNativeApp(self, cmd: str) -> None:
        if not cmd:
            return
        env = dict(os.environ)
        env.update({"WAYLAND_DISPLAY": "wayland-0", "QT_QPA_PLATFORM": "wayland",
                    "GDK_BACKEND": "wayland", "XDG_SESSION_TYPE": "wayland"})
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            self.appLaunchFailed.emit(cmd, f"comando mal formado: {exc}")
            return
        if not argv:
            self.appLaunchFailed.emit(cmd, "comando vacío")
            return
        # Pre-check: ¿existe el binario? Sin esto Popen no falla aunque el
        # ejecutable no exista (porque tira FileNotFoundError solo en exec()
        # del hijo, ya tarde — el padre no lo ve). Lo cazamos arriba.
        binary = argv[0]
        from shutil import which  # noqa: PLC0415
        resolved = which(binary) if "/" not in binary else (binary if os.path.isfile(binary) else None)
        if resolved is None:
            self.appLaunchFailed.emit(cmd, f"binario no encontrado: {binary}")
            logger.warning("sysmanager.launch_missing_binary cmd=%s bin=%s", cmd, binary)
            return
        try:
            # Capturamos stderr a /tmp/lumen-app-<binname>.log para diagnóstico —
            # antes el stderr iba al journal del compositor y se perdía en la
            # vorágine. Ahora puedes ver POR QUÉ chromium murió.
            logname = os.path.basename(binary).replace("/", "_") or "app"
            errpath = f"/tmp/lumen-app-{logname}.log"
            stderr_fd = open(errpath, "ab", buffering=0)  # noqa: SIM115
            proc = subprocess.Popen(  # noqa: S603
                argv, env=env, stdout=stderr_fd, stderr=stderr_fd,
                start_new_session=True,
            )
            logger.info("sysmanager.launch cmd=%s pid=%s log=%s", cmd, proc.pid, errpath)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sysmanager.launch_failed cmd=%s err=%r", cmd, exc)
            self.appLaunchFailed.emit(cmd, repr(exc))

    @Slot(str)
    def openFile(self, path: str) -> None:
        """Abre un fichero con su app por defecto (xdg-open) en el compositor."""
        if path:
            self.launchNativeApp("xdg-open " + shlex.quote(path))

    @Slot(result=str)
    def pasteFromClipboard(self) -> str:
        try:
            r = subprocess.run(["wl-paste", "-n"], capture_output=True, text=True, timeout=2)  # noqa: S607
            return r.stdout or ""
        except Exception:  # noqa: BLE001
            return ""

    @Slot(str)
    def copyToClipboard(self, text: str) -> None:
        try:
            subprocess.run(["wl-copy"], input=text or "", text=True, timeout=2)  # noqa: S607
        except Exception:  # noqa: BLE001
            pass

    @Slot(str)
    def createDir(self, path: str) -> None:
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sysmanager.createDir_failed path=%s err=%r", path, exc)

    @Slot(str, str, result=bool)
    def renameFile(self, src: str, dst: str) -> bool:
        if not src or not dst:
            return False
        try:
            shutil.move(src, dst)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("sysmanager.renameFile_failed %s->%s err=%r", src, dst, exc)
            return False

    @Slot(str, result=bool)
    def deleteFile(self, path: str) -> bool:
        if not path:
            return False
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("sysmanager.deleteFile_failed path=%s err=%r", path, exc)
            return False

    @Slot(str, result=str)
    def listDirectory(self, path: str) -> str:
        """Lista un directorio → JSON [{name, path, isDir, size, isHidden}]."""
        try:
            base = path or os.path.expanduser("~")
            out = []
            with os.scandir(base) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        size = 0 if is_dir else e.stat(follow_symlinks=False).st_size
                    except Exception:  # noqa: BLE001
                        is_dir, size = False, 0
                    out.append({"name": e.name, "path": os.path.join(base, e.name),
                                "isDir": is_dir, "size": size, "isHidden": e.name.startswith(".")})
            out.sort(key=lambda d: (not d["isDir"], d["name"].lower()))
            return json.dumps(out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sysmanager.listDirectory_failed path=%s err=%r", path, exc)
            return "[]"

    # ── Comandos del SO ─────────────────────────────────────────────────
    @Slot(str, str)
    def runCommandAsync(self, cmd: str, _arg: str = "") -> None:
        if not cmd:
            return
        try:
            subprocess.Popen(cmd, shell=True)  # noqa: S602 — comandos de NUESTRA UI
        except Exception as exc:  # noqa: BLE001
            logger.warning("sysmanager.runCommandAsync_failed cmd=%s err=%r", cmd, exc)

    @Slot(str, result=str)
    def runCommandQuick(self, cmd: str) -> str:
        return self._sh(cmd)[1] if cmd else ""

    @Slot(str, str, result=str)
    def runCommand(self, cmd: str, cwd: str = "") -> str:
        return self._sh(cmd, cwd=cwd)[1] if cmd else ""

    # ── Pantalla ────────────────────────────────────────────────────────
    @Slot(int, int, result=bool)
    def setDisplayResolution(self, _w: int, _h: int) -> bool:
        # En compositor Qt/Wayland la resolución la fija el WaylandOutput, no xrandr.
        return False

    @Slot(result=str)
    def getDisplayInfo(self) -> str:
        return ""  # sin xrandr; el QML cae a Screen.*

    @Slot()
    def getDisplayInfoAsync(self) -> None:
        self._async(self._emit_gpu)

    @Slot()
    def getGpuInfoAsync(self) -> None:
        self._async(self._emit_gpu)

    def _emit_gpu(self) -> None:
        ok, out = self._sh("lspci 2>/dev/null | grep -iE 'vga|3d|display' | head -1")
        self.displayInfoReady.emit("")
        self.gpuInfoReady.emit(out if ok and out else "VirtIO GPU")

    # ── Hora / zonas ────────────────────────────────────────────────────
    @Slot()
    def getTimeInfoAsync(self) -> None:
        def work():
            tz = sync = ntp = local = utc = ""
            ok, out = self._sh("timedatectl show -p Timezone -p NTPSynchronized -p NTP --value")
            if ok:
                v = out.split("\n")
                tz = v[0] if len(v) > 0 else ""
                sync = "yes" if (len(v) > 1 and v[1] == "yes") else "no"
                ntp = "active" if (len(v) > 2 and v[2] == "yes") else "inactive"
            try:
                from datetime import datetime, timezone  # noqa: PLC0415
                local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:  # noqa: BLE001
                pass
            self.timeInfoReady.emit(tz, sync, ntp, local, utc)
        self._async(work)

    @Slot()
    def getTimezonesAsync(self) -> None:
        def work():
            self.timezonesReady.emit(self._sh("timedatectl list-timezones")[1])
        self._async(work)

    @Slot(str)
    def setTimezoneAsync(self, tz: str) -> None:
        def work():
            ok, out = self._sh("timedatectl set-timezone " + shlex.quote(tz))
            self.timeOpResult.emit("setTimezone", ok, out)
        self._async(work)

    @Slot(str)
    def setManualTimeAsync(self, time_str: str) -> None:
        def work():
            ok, _ = self._sh("timedatectl set-ntp false")
            ok2, out = self._sh("timedatectl set-time " + shlex.quote(time_str))
            self.timeOpResult.emit("setManualTime", ok2, out)
        self._async(work)

    @Slot(bool)
    def toggleNtpAsync(self, enable: bool) -> None:
        def work():
            ok, out = self._sh("timedatectl set-ntp " + ("true" if enable else "false"))
            self.timeOpResult.emit("toggleNtp", ok, out)
        self._async(work)

    # ── Usuarios ────────────────────────────────────────────────────────
    @Slot(result=str)
    def listUsers(self) -> str:
        """Usuarios reales (uid>=1000, login válido) → JSON [{username, role}]."""
        try:
            out = []
            for p in pwd.getpwall():
                if p.pw_uid >= 1000 and "nologin" not in p.pw_shell and "false" not in p.pw_shell:
                    out.append({"username": p.pw_name, "role": "admin" if p.pw_uid == 1000 else "user"})
            return json.dumps(out)
        except Exception:  # noqa: BLE001
            return "[]"

    @Slot(result=bool)
    def accountConfigured(self) -> bool:
        """True si el onboarding ya creó la cuenta (sentinel de hermes-account-apply).

        El gate del compositor muestra el wizard mientras esto sea False, y el
        LoginScreen (PAM real) cuando sea True. Nunca el desktop sin cuenta.
        """
        return os.path.exists("/var/lib/hermes/account-applied")

    @Slot(result=bool)
    def shouldRestartForRemote(self) -> bool:
        """True si el remoto quedó auto-activado por el onboarding pero el compositor
        sigue en eglfs (local) en vez de VNC.

        hermes-account-apply, al crear la cuenta, escribe el flag espejo
        /etc/hermes/remote-display.env (QT_QPA_PLATFORM=vnc:...:5900) + el flag
        /var/lib/hermes/remote-active + habilita gateway/túnel/noVNC. Pero el
        compositor YA estaba arrancado (mostrando el wizard) en eglfs → no recoge el
        platform plugin VNC hasta reiniciar. Sin reinicio, :5900 no se sirve y noVNC
        se queda en 'Conectando…'. Este check lo detecta para forzar el reinicio al
        terminar el onboarding (ver restartForRemote)."""
        plat = os.environ.get("QT_QPA_PLATFORM", "")
        in_vnc = plat.startswith("vnc")
        remote_on = os.path.exists("/var/lib/hermes/remote-active")
        return remote_on and not in_vnc

    @Slot()
    def restartForRemote(self) -> None:
        """Sale del proceso para que lumenso-shell.service (Restart=always) reinicie
        el compositor cargando /etc/hermes/remote-display.env → QT_QPA_PLATFORM=vnc →
        sirve el framebuffer en :5900 (noVNC lo expone). Sin privilegios: no usa
        systemctl, solo termina; systemd rehace el arranque en modo VNC. Tras el
        reinicio el sentinel existe → se muestra el LoginScreen (PAM) por noVNC."""
        logger.info(
            "sys_manager.restart_for_remote: onboarding hecho + remote-active → "
            "saliendo para rearrancar el compositor en modo VNC (:5900)"
        )
        os._exit(0)

    @Slot(result=str)
    def loginUser(self) -> str:
        """Usuario de SO real de la sesión (el dueño del proceso compositor).
        SIEMPRE es contra ESTE usuario que se valida la contraseña — unix_chkpwd
        (no-root) solo puede verificar la del propio caller. El display-name que
        se teclea en el onboarding va al GECOS, NO crea un usuario nuevo."""
        try:
            return pwd.getpwuid(os.getuid()).pw_name
        except Exception:  # noqa: BLE001
            return "hermes-user"

    @Slot(result=str)
    def displayName(self) -> str:
        """Nombre visible (GECOS) que el usuario puso en el onboarding."""
        try:
            gecos = pwd.getpwuid(os.getuid()).pw_gecos or ""
            return gecos.split(",")[0].strip() or self.loginUser()
        except Exception:  # noqa: BLE001
            return self.loginUser()

    @Slot(str, str, result=bool)
    def authenticate(self, _user: str, password: str) -> bool:
        # Verificación REAL de la contraseña del onboarding (vs /etc/shadow).
        # CLAVE (fix lockout): se valida SIEMPRE contra el usuario de SO real
        # (loginUser = dueño del proceso = hermes-user), IGNORANDO lo que se
        # teclee en el campo. unix_chkpwd no-root solo verifica al propio caller;
        # autenticar el display-name ("ainux") fallaba siempre → lockout.
        # FAIL-CLOSED: sin contraseña o sin método disponible se DENIEGA.
        if not password:
            return False
        user = self.loginUser()
        # 1) python-pam con el servicio dedicado hermes-auth (solo pam_unix).
        try:
            import pam as _pam  # noqa: PLC0415

            if _pam.pam().authenticate(user, password, service="hermes-auth"):
                return True
        except Exception:  # noqa: BLE001
            pass
        # 2) Fallback robusto: unix_chkpwd (no requiere el paquete python-pam).
        try:
            proc = subprocess.run(  # noqa: S603
                ["/usr/sbin/unix_chkpwd", user, "nullok"],
                input=(password + "\0").encode(),
                capture_output=True,
                timeout=5,
            )
            return proc.returncode == 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("sysmanager.authenticate: sin método PAM (%s) — deniego", type(exc).__name__)
            return False

    @Slot(str, str)
    def authenticateAsync(self, user: str, password: str) -> None:
        self.authResult.emit(self.authenticate(user, password))

    @Slot(str, str, result=bool)
    def addUser(self, username: str, password: str) -> bool:
        ok, _ = self._sh(f"useradd -m {shlex.quote(username)} && echo {shlex.quote(username)}:{shlex.quote(password)} | chpasswd")
        return ok

    @Slot(str, str)
    def addUserAsync(self, username: str, password: str) -> None:
        # El handler QML compara operation === "addUser" → emitir ese string
        # exacto (antes "add" → no-op). En éxito, detail = username (el toast lo
        # interpola).
        def work():
            ok = self.addUser(username, password)
            self.userOpResult.emit("addUser", ok, username if ok else "requiere privilegios de root")
        self._async(work)

    @Slot(str, result=bool)
    def deleteUser(self, username: str) -> bool:
        return self._sh("userdel -r " + shlex.quote(username))[0]

    @Slot(str)
    def deleteUserAsync(self, username: str) -> None:
        def work():
            ok = self.deleteUser(username)
            self.userOpResult.emit("deleteUser", ok, username if ok else "requiere privilegios de root")
        self._async(work)

    @Slot(str, str)
    def changePasswordAsync(self, current: str, new: str) -> None:
        """Stage a password-change request for hermes-passwd-apply (root helper).

        Pre-verifies 'current' via PAM for fast UI feedback, but the
        AUTHORITATIVE check is the PAM gate in the root helper.  The staged
        file is shredded by the helper regardless of success or failure.
        """
        def work():
            # Fast-path rejection: wrong current password → immediate feedback
            # without waiting for the root helper. Not a security gate (the
            # helper repeats the check); purely a UX shortcut.
            if current and not self.authenticate("", current):
                self.userOpResult.emit(
                    "changePassword", False, "contraseña actual incorrecta"
                )
                return
            ok = self._stage(
                "passwd-request.json",
                {"action": "change_password", "current": current, "new": new},
            )
            self.userOpResult.emit(
                "changePassword", ok,
                "" if ok else "no se pudo enviar la solicitud de cambio de contraseña",
            )
        self._async(work)

    # ── Acceso remoto: el COMPOSITOR (hermes-user) stagea la petición ────────
    # El daemon está bloqueado de /run/hermes/remote-control por seguridad
    # (InaccessiblePaths) — un agente comprometido no debe poder pedir remoto.
    # La UI (operada por el humano que teclea la contraseña) sí. El root helper
    # hermes-remote-access-control PAM-verifica la contraseña antes de activar.
    @Slot(str, result=bool)
    def enableRemoteAccess(self, password: str) -> bool:
        return self._stage_remote("enable", password)

    @Slot(str, result=bool)
    def disableRemoteAccess(self, password: str) -> bool:
        return self._stage_remote("disable", password)

    def _stage_remote(self, action: str, password: str) -> bool:
        if not password:
            return False
        return self._stage(
            "request.json",
            {"action": action, "password": password},
        )

    def _stage(self, filename: str, extra_payload: dict) -> bool:
        """Atomically write a JSON staging file to /run/hermes/remote-control/.

        Creates the directory (mode 0700) if absent, writes to a .tmp sibling,
        chmods to 0600, then os.replace atomically — the root helper opens with
        O_NOFOLLOW and fstat-verifies uid/mode/nlink before reading.

        All staging files share the same directory; distinct filenames prevent
        collisions between concurrent requests (remote-control vs passwd-apply).
        """
        import json as _json  # noqa: PLC0415
        import stat as _stat  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415

        d = "/run/hermes/remote-control"
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            target = os.path.join(d, filename)
            tmp = target + ".tmp"
            payload = {
                **extra_payload,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(tmp, "w", encoding="utf-8") as f:  # noqa: PTH123
                f.write(_json.dumps(payload))
            os.chmod(tmp, _stat.S_IRUSR | _stat.S_IWUSR)  # 0600
            os.replace(tmp, target)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sysmanager.stage_failed file=%s err=%r", filename, type(exc).__name__
            )
            return False
