"""LandlockRulesetBuilder — genera el ruleset Landlock por capability.

Spec 003 FR-052 BLOQUEANTE (sandbox kernel enforcement) — research §10
NemoClaw-style hardening.

Por cada capability concedida en consent_manager construimos el set
mínimo de paths read/write y syscalls permitidos. El loader real
(`landlock_loader.py` siguiente capa) traduce este RulesetSpec a la
syscall `landlock_create_ruleset` + `landlock_add_rule` + `prctl`.

Esta clase NO toca el kernel — produce una estructura inmutable
serializable a JSON para audit.

Startup invariante (finding #19 / FR-052): llamar assert_landlock_active()
desde el entrypoint del runtime antes de construir rulesets para capabilities
que dependen de enforcement kernel. Si Landlock no está activo el runtime
debe negarse a arrancar (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from hermes.agents_os.application.consent_manager import Capability

# ---------------------------------------------------------------------------
# Landlock LSM active check (finding #19 / FR-052)
# ---------------------------------------------------------------------------

_LSM_STATUS_PATH = Path("/sys/kernel/security/lsm")


def is_landlock_active() -> bool:
    """Devuelve True si Landlock aparece en /sys/kernel/security/lsm.

    Requiere securityfs montado (estándar en Linux ≥ 5.13 con bootc).
    Devuelve False si el fichero no existe (kernel sin securityfs o sin LSM).
    """
    try:
        lsm_list = _LSM_STATUS_PATH.read_text(encoding="ascii").strip()
        return "landlock" in lsm_list.split(",")
    except OSError:
        return False


def assert_landlock_active() -> None:
    """Lanza RuntimeError si Landlock no está activo — fail-closed.

    Llamar desde el entrypoint del runtime antes de conceder cualquier
    capability que requiera enforcement kernel (FR-052).

    Si el kernel cmdline no incluye lsm=landlock,... o el kernel fue
    compilado sin CONFIG_SECURITY_LANDLOCK=y, este check detecta la
    ausencia y el runtime se niega a arrancar.
    """
    if not is_landlock_active():
        raise RuntimeError(
            "Landlock LSM no está activo en este kernel. "
            "Verifica: cat /sys/kernel/security/lsm — debe incluir 'landlock'. "
            "Cmdline requerido: lsm=landlock,lockdown,yama,bpf. "
            "El runtime no puede garantizar sandbox sin kernel enforcement (FR-052)."
        )


class AccessRight(StrEnum):
    """Subset de Landlock access rights soportados por el SO."""

    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    EXECUTE = "execute"
    READ_DIR = "read_dir"
    REMOVE_DIR = "remove_dir"
    REMOVE_FILE = "remove_file"
    MAKE_CHAR = "make_char"
    MAKE_DIR = "make_dir"
    MAKE_REG = "make_reg"
    MAKE_SOCK = "make_sock"
    MAKE_FIFO = "make_fifo"
    MAKE_BLOCK = "make_block"
    MAKE_SYM = "make_sym"
    REFER = "refer"
    TRUNCATE = "truncate"


@dataclass(frozen=True, slots=True)
class PathRule:
    """Regla de path con accesos permitidos."""

    path: str
    accesses: frozenset[AccessRight]


@dataclass(frozen=True, slots=True)
class RulesetSpec:
    """Spec inmutable que se pasará al loader Landlock real."""

    capability: Capability
    handled_access_fs: frozenset[AccessRight]
    rules: tuple[PathRule, ...]
    deny_all_network: bool = True
    description: str = ""


# Mapeo capability → paths permitidos. SIEMPRE deny-by-default (FR-052
# constitución IV fail-closed). Si una capability nueva no aparece aquí,
# el builder LANZA — no concedemos default ancho.

# P0-2: rights del ruleset RUNTIME (el propio daemon). RX = leer+listar+ejecutar;
# RW = +escribir/crear/borrar. Amplio a propósito: el daemon es código confiable
# ya confinado por systemd; esta capa LSM no debe poder ROMPERLO.
_RUNTIME_RX: frozenset[AccessRight] = frozenset({
    AccessRight.READ_FILE, AccessRight.READ_DIR, AccessRight.EXECUTE,
})
_RUNTIME_RW: frozenset[AccessRight] = _RUNTIME_RX | frozenset({
    AccessRight.WRITE_FILE, AccessRight.MAKE_REG, AccessRight.MAKE_DIR,
    AccessRight.MAKE_SOCK, AccessRight.MAKE_FIFO, AccessRight.MAKE_SYM,
    AccessRight.REMOVE_FILE, AccessRight.REMOVE_DIR, AccessRight.TRUNCATE,
})

_CAPABILITY_PATHS: dict[Capability, tuple[tuple[str, frozenset[AccessRight]], ...]] = {
    Capability.DOCUMENTS: (
        (
            "/home/{user}/Documents",
            frozenset(
                {
                    AccessRight.READ_FILE,
                    AccessRight.READ_DIR,
                    AccessRight.WRITE_FILE,
                    AccessRight.MAKE_REG,
                    AccessRight.TRUNCATE,
                }
            ),
        ),
    ),
    Capability.DOWNLOADS: (
        (
            "/home/{user}/Downloads",
            frozenset(
                {
                    AccessRight.READ_FILE,
                    AccessRight.READ_DIR,
                    AccessRight.WRITE_FILE,
                    AccessRight.MAKE_REG,
                    AccessRight.TRUNCATE,
                }
            ),
        ),
    ),
    Capability.PACKAGE_MANAGER: (
        ("/usr/bin/rpm-ostree", frozenset({AccessRight.READ_FILE, AccessRight.EXECUTE})),
        ("/usr/bin/flatpak", frozenset({AccessRight.READ_FILE, AccessRight.EXECUTE})),
    ),
    Capability.TERMINAL: (
        ("/usr/bin/bash", frozenset({AccessRight.READ_FILE, AccessRight.EXECUTE})),
        ("/usr/bin/sh", frozenset({AccessRight.READ_FILE, AccessRight.EXECUTE})),
        # Workspace donde el terminal puede escribir.
        (
            "/var/lib/hermes/terminal-workspace",
            frozenset(
                {
                    AccessRight.READ_FILE,
                    AccessRight.READ_DIR,
                    AccessRight.WRITE_FILE,
                    AccessRight.MAKE_REG,
                    AccessRight.MAKE_DIR,
                    AccessRight.TRUNCATE,
                }
            ),
        ),
    ),
    Capability.SYSTEM_SETTINGS: (
        # gnome-control-center via DBus — no FS access ancho, solo el dir
        # de schemas gsettings.
        (
            "/usr/share/glib-2.0/schemas",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
    ),
    Capability.DESKTOP_FILES: (
        (
            "/home/{user}/Desktop",
            frozenset(
                {
                    AccessRight.READ_FILE,
                    AccessRight.READ_DIR,
                    AccessRight.WRITE_FILE,
                    AccessRight.MAKE_REG,
                    AccessRight.TRUNCATE,
                }
            ),
        ),
    ),
    Capability.FILESYSTEM_FULL: (
        # Esta capability es PELIGROSA — solo concedible vía HITL y con
        # audit reforzado. Aquí abrimos el home completo.
        (
            "/home/{user}",
            frozenset(
                {
                    AccessRight.READ_FILE,
                    AccessRight.READ_DIR,
                    AccessRight.WRITE_FILE,
                    AccessRight.MAKE_REG,
                    AccessRight.MAKE_DIR,
                    AccessRight.REMOVE_FILE,
                    AccessRight.REMOVE_DIR,
                    AccessRight.TRUNCATE,
                }
            ),
        ),
    ),
    Capability.CAMERA: (),  # solo dispositivos via PipeWire portal, no FS.
    Capability.MICROPHONE: (),
    Capability.NETWORK_LOCAL: (),  # control vía nftables fuera de Landlock.
    # spec 009 §4: confinamiento del navegador del agente.
    # deny_all_network en RulesetSpec es informativo — la red la hace netns+nft.
    # Sin regla para /etc/hermes, /run/hermes, audit, otras sesiones → deny por defecto.
    Capability.BROWSER: (
        # PASS-2 FIX (red-team 2026-06-19): the launcher does NOT exec
        # /usr/bin/agent-browser (that is the node CLI). It exec's a Landlock
        # self-apply shim — `/usr/bin/python3 -c <shim> /usr/bin/chromium-browser …`
        # — which restricts ITSELF to this ruleset and then execv()s the REAL
        # Chromium. Landlock EXECUTE rules are evaluated against the RESOLVED
        # symlink target, so granting EXECUTE on the /usr/bin/* symlinks is not
        # enough — the rule must cover the actual on-disk binaries:
        #   • /usr/bin/python3  → /usr/lib(64)/… interpreter  (the shim host)
        #   • /usr/bin/chromium-browser → /ms-playwright/chromium-*/chrome-linux/chrome
        #     (symlink created in ops/container/Containerfile: `ln -sf $CHROME …`)
        # WITHOUT these the post-restrict_self execv of Chromium returns EACCES
        # and the browser never starts (the PASS-1 ruleset bound nothing real).
        # READ is required alongside EXECUTE because the ELF loader must read()
        # the binary + its bundled .so's.
        (
            "/usr/bin/python3",
            frozenset({AccessRight.READ_FILE, AccessRight.EXECUTE}),
        ),
        # Python interpreter real image + stdlib + the hermes package live under
        # BOTH /usr/lib (bootc target=/usr/lib/python3.13/site-packages) and
        # /usr/lib64 (compiled extensions / ld real target). The shim imports
        # hermes.security.landlock_loader BEFORE restrict_self, but the execv of
        # Chromium afterwards still needs the dynamic loader + libc here.
        (
            "/usr/lib",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR, AccessRight.EXECUTE}),
        ),
        (
            "/usr/lib64",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR, AccessRight.EXECUTE}),
        ),
        # ld-linux + base ELF libs (libc, libgcc) — Chromium's interpreter line.
        (
            "/lib",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR, AccessRight.EXECUTE}),
        ),
        (
            "/lib64",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR, AccessRight.EXECUTE}),
        ),
        # The REAL Chromium binary + its bundled shared libraries / ICU / locales
        # ship under the Playwright tree; /usr/bin/chromium-browser resolves here.
        # EXECUTE so the kernel allows the execv; READ so the loader maps the libs.
        (
            "/ms-playwright",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR, AccessRight.EXECUTE}),
        ),
        (
            "/usr/share/fonts",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        (
            "/usr/share/fontconfig",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        (
            "/etc/fonts",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        # Locale / timezone data Chromium reads at startup; without READ it logs
        # noisily but still runs — kept minimal (no EXECUTE, no write).
        (
            "/usr/share/locale",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        (
            "/etc/ssl/certs",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        (
            "/usr/share/ca-certificates",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        # The browser-jail shim reads its seccomp profile (chromium-browser.json)
        # AFTER applying this Landlock ruleset — load_and_apply("BROWSER") runs
        # before _install_seccomp() in hermes-browser-launcher. Without READ here
        # the open() → EACCES (Errno 13), the jail exits 1, CDP never starts and
        # every browse call fails-closed ("el sistema bloquea el navegador").
        (
            "/usr/share/hermes/seccomp",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        # Runtime pseudo-FS Chromium reads (process self-introspection, entropy,
        # /dev/null). Deny-by-default would otherwise block these and abort the
        # browser at startup. /proc here is READ-only (no write/exec); the
        # /proc/<daemon>/fd exfil vector is closed at the DAC layer (the browser
        # runs as hermes-sandbox, not the daemon's hermes) + ProtectProc=invisible.
        (
            "/proc",
            frozenset({AccessRight.READ_FILE, AccessRight.READ_DIR}),
        ),
        (
            "/dev/null",
            frozenset({AccessRight.READ_FILE, AccessRight.WRITE_FILE}),
        ),
        (
            "/dev/urandom",
            frozenset({AccessRight.READ_FILE}),
        ),
        (
            "/dev/random",
            frozenset({AccessRight.READ_FILE}),
        ),
        # /dev/shm: Chromium IPC shared memory. The launcher passes
        # --disable-dev-shm-usage so this is a fallback, but grant the writable
        # shm segment so a non-fallback build still renders.
        (
            "/dev/shm",
            frozenset(
                {
                    AccessRight.READ_FILE,
                    AccessRight.READ_DIR,
                    AccessRight.WRITE_FILE,
                    AccessRight.MAKE_REG,
                    AccessRight.TRUNCATE,
                }
            ),
        ),
        # La sesión concreta se inyecta en build() via {session}.
        # El builder base deja el placeholder; BrowserLandlockRulesetBuilder lo resuelve.
        (
            "/var/lib/hermes/browser-sessions/{session}",
            frozenset(
                {
                    AccessRight.READ_FILE,
                    AccessRight.READ_DIR,
                    AccessRight.WRITE_FILE,
                    AccessRight.MAKE_REG,
                    AccessRight.MAKE_DIR,
                    AccessRight.TRUNCATE,
                    # Chromium's ProcessSingleton creates an AF_UNIX socket
                    # (SingletonSocket) + lock/cookie files in its profile dir;
                    # without MAKE_SOCK it aborts with "Failed to create socket
                    # directory" (status 21). The browser is fully confined to
                    # this dir by Landlock, so granting full file-type control
                    # INSIDE its own profile is correct — it cannot escape the dir.
                    AccessRight.MAKE_SOCK,
                    AccessRight.MAKE_FIFO,
                    AccessRight.MAKE_SYM,
                    AccessRight.REMOVE_FILE,
                    AccessRight.REMOVE_DIR,
                }
            ),
        ),
    ),
    # P0-2: el daemon se AUTOCONFINA (defense-in-depth, 2ª capa LSM sobre systemd).
    # Amplio a propósito — cubre TODO lo que el daemon necesita en runtime
    # (python/libs/certs en /usr, vault+DB en /var/lib/hermes, socket+notify en
    # /run, D-Bus, /tmp privado) → NO puede romperlo. A nivel kernel deniega lo
    # que systemd deja read-only o no toca: /boot /opt /mnt /srv /media /home
    # /root y cualquier ruta fuera del árbol legítimo. fail-closed vía el
    # ExecStartPre=hermes-landlock-assert (si no hay Landlock, el daemon no arranca).
    Capability.RUNTIME: (
        ("/usr", _RUNTIME_RX), ("/etc", _RUNTIME_RX), ("/bin", _RUNTIME_RX),
        ("/sbin", _RUNTIME_RX), ("/lib", _RUNTIME_RX), ("/lib64", _RUNTIME_RX),
        ("/proc", _RUNTIME_RX), ("/sys", _RUNTIME_RX), ("/var", _RUNTIME_RX),
        ("/var/lib/hermes", _RUNTIME_RW), ("/run", _RUNTIME_RW),
        ("/tmp", _RUNTIME_RW), ("/dev", _RUNTIME_RW),
    ),
}


@dataclass(slots=True)
class LandlockRulesetBuilder:
    """Construye un RulesetSpec por capability.

    Para Capability.BROWSER el template `{session}` permanece sin resolver
    a menos que se use BrowserLandlockRulesetBuilder con session_name explícito.
    En CI (sin sesión real) usar BrowserLandlockRulesetBuilder("test-session").
    """

    user_home_user: str = "hermes"
    session_name: str = ""  # resuelve {session} en paths BROWSER

    def build(self, capability: Capability) -> RulesetSpec:
        if capability not in _CAPABILITY_PATHS:
            raise ValueError(
                f"capability {capability!r} no tiene template Landlock — "
                "DENY por default (FR-052)"
            )
        templates = _CAPABILITY_PATHS[capability]
        rules = tuple(
            PathRule(
                path=str(
                    Path(
                        template.format(
                            user=self.user_home_user,
                            session=self.session_name,
                        )
                    )
                ),
                accesses=accesses,
            )
            for template, accesses in templates
        )
        handled: set[AccessRight] = set()
        for rule in rules:
            handled.update(rule.accesses)
        return RulesetSpec(
            capability=capability,
            handled_access_fs=frozenset(handled),
            rules=rules,
            description=f"Landlock ruleset for capability={capability.value}",
        )

    def build_aggregated(
        self, capabilities: frozenset[Capability]
    ) -> tuple[RulesetSpec, ...]:
        """Construye un ruleset por capability — NO los junta.

        Landlock soporta múltiples rulesets stackeados; el loader los
        carga uno a uno con prctl. Cada uno restringe; combinar reglas
        amplía permisos (Landlock es deny-by-default por ruleset y el
        kernel intersecta).
        """
        return tuple(self.build(cap) for cap in sorted(capabilities, key=str))


def build_browser_ruleset(session_name: str) -> RulesetSpec:
    """Helper: construye el RulesetSpec para Capability.BROWSER con session_name concreto.

    Resuelve el placeholder `{session}` en el path de la sesión del navegador.
    Llamado por el loader Landlock y por el jail script antes de execv.

    Nota: deny_all_network=True es informativo; la red la restringe netns+nft.
    """
    builder = LandlockRulesetBuilder(session_name=session_name)
    return builder.build(Capability.BROWSER)


def serialize_for_audit(spec: RulesetSpec) -> dict:
    """Volcado JSON-serializable para audit_entries (hash chain)."""
    return {
        "capability": spec.capability.value,
        "handled_access_fs": sorted(a.value for a in spec.handled_access_fs),
        "rules": [
            {"path": rule.path, "accesses": sorted(a.value for a in rule.accesses)}
            for rule in spec.rules
        ],
        "deny_all_network": spec.deny_all_network,
        "description": spec.description,
    }
