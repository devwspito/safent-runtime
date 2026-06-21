"""Landlock loader — aplica un RulesetSpec Landlock al proceso actual.

Invocable como módulo hijo:
    python3 -m hermes.security.landlock_loader BROWSER
    python3 -m hermes.security.landlock_loader TERMINAL

Contrato de salida:
    exit 0  — Landlock aplicado OK, o Landlock ausente (soft-degrade, logged).
    exit 2  — argumento inválido o capability desconocida.
    exit 3  — error duro de aplicación (syscall falló de forma no-degradable).

Diseño (spec 009 §4):
  1. Lee RulesetSpec(capability) desde LandlockRulesetBuilder.
  2. Detecta si Landlock está activo (/sys/kernel/security/lsm) y la ABI disponible.
  3. Si activo: landlock_create_ruleset → landlock_add_rule × paths → prctl(PR_SET_NO_NEW_PRIVS) → landlock_restrict_self.
  4. Si ausente: log landlock_unavailable_degraded y exit 0 (el confinamiento systemd-run sigue).
  5. execv hacia el navegador NO ocurre aquí — este loader es importado por browser-jail (script de shell);
     el loader aplica el sandbox y retorna; el jail llama exec() desde C/shell.

IMPORTANTE: este módulo se ejecuta como proceso HIJO del jail. NUNCA se importa
por el daemon hermes-runtime. El daemon NO llama a este módulo.

ABI Landlock (kernel ≥ 5.13):
  ABI 1 (5.13): FS path rules.
  ABI 2 (5.19): REFER.
  ABI 3 (6.0):  TRUNCATE.
  ABI 4 (6.8+): reglas de red (LANDLOCK_ACCESS_NET_BIND_TCP / CONNECT_TCP).
  → La red la hace netns+nft; aquí solo usamos reglas FS (ABI 1-3 sólidas en Fedora 41).

Números de syscall (x86_64 / aarch64):
  landlock_create_ruleset  = 444 (x86_64), 444 (aarch64)
  landlock_add_rule        = 445 (x86_64), 445 (aarch64)
  landlock_restrict_self   = 446 (x86_64), 446 (aarch64)
  (ref: torvalds/linux include/uapi/linux/landlock.h + arch/x86/entry/syscalls/syscall_64.tbl)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
import struct
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Syscall numbers per arch
# ---------------------------------------------------------------------------

_SYSCALL_TABLE: dict[str, dict[str, int]] = {
    "x86_64": {
        "landlock_create_ruleset": 444,
        "landlock_add_rule": 445,
        "landlock_restrict_self": 446,
    },
    "aarch64": {
        "landlock_create_ruleset": 444,
        "landlock_add_rule": 445,
        "landlock_restrict_self": 446,
    },
}

# ---------------------------------------------------------------------------
# Landlock constants (include/uapi/linux/landlock.h)
# ---------------------------------------------------------------------------

LANDLOCK_CREATE_RULESET_VERSION: int = 1 << 0

LANDLOCK_RULE_PATH_BENEATH: int = 1

# access_fs bitmask values (ABI 1–3)
_ACCESS_FS_MAP = {
    "execute":      1 << 0,
    "write_file":   1 << 1,
    "read_file":    1 << 2,
    "read_dir":     1 << 3,
    "remove_dir":   1 << 4,
    "remove_file":  1 << 5,
    "make_char":    1 << 6,
    "make_dir":     1 << 7,
    "make_reg":     1 << 8,
    "make_sock":    1 << 9,
    "make_fifo":    1 << 10,
    "make_block":   1 << 11,
    "make_sym":     1 << 12,
    "refer":        1 << 13,   # ABI 2+
    "truncate":     1 << 14,   # ABI 3+
}

# Access rights introduced per ABI version (cumulative max bitmask).
_ABI_MAX_ACCESS_FS: dict[int, int] = {
    1: (1 << 13) - 1,              # bits 0–12  (no refer/truncate)
    2: (1 << 14) - 1,              # bits 0–13
    3: (1 << 15) - 1,              # bits 0–14
}

PR_SET_NO_NEW_PRIVS: int = 38

# ---------------------------------------------------------------------------
# Low-level ctypes/syscall interface
# ---------------------------------------------------------------------------


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    if name is None:
        raise OSError("libc not found")
    return ctypes.CDLL(name, use_errno=True)


def _syscall_nr(name: str) -> int:
    machine = platform.machine()
    table = _SYSCALL_TABLE.get(machine)
    if table is None:
        raise UnsupportedArchError(f"Landlock syscall numbers not known for arch={machine!r}")
    return table[name]


class UnsupportedArchError(RuntimeError):
    """Arquitectura sin tabla de syscall Landlock."""


class LandlockSyscallError(OSError):
    """Una syscall Landlock falló de forma no-degradable."""


def _raw_syscall(nr: int, *args: int) -> int:
    """Invoca syscall(nr, ...) via libc.syscall(). Devuelve el valor de retorno."""
    lib = _libc()
    lib.syscall.restype = ctypes.c_long
    lib.syscall.argtypes = [ctypes.c_long] + [ctypes.c_long] * len(args)
    return int(lib.syscall(nr, *args))


def _errno() -> int:
    return ctypes.get_errno()


# ---------------------------------------------------------------------------
# Landlock ABI detection
# ---------------------------------------------------------------------------

# Sentinel: Landlock EXISTE en el kernel pero un filtro seccomp BLOQUEA los
# syscalls landlock_* (EPERM/EACCES). Distinto de None (= kernel sin Landlock).
# V-5: este caso debe FALLAR CERRADO, NO degradar — si no, un perfil seccomp que
# olvide allowlistar landlock_* dejaría correr el navegador con credenciales SIN
# jaula Landlock, en silencio.
_ABI_BLOCKED = -1


def _detect_abi() -> int | None:
    """Detecta la versión ABI Landlock disponible en el kernel.

    Devuelve:
      - int ≥ 1      → versión ABI (Landlock disponible y aplicable).
      - None         → Landlock NO disponible (kernel sin Landlock / sin lsm).
      - _ABI_BLOCKED → Landlock existe pero seccomp bloquea landlock_* (EPERM).
    """
    try:
        nr = _syscall_nr("landlock_create_ruleset")
    except (UnsupportedArchError, KeyError):
        return None

    # landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)
    # Con estos args el kernel devuelve la versión ABI (int positivo) sin crear fd.
    ret = _raw_syscall(nr, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    if ret < 0:
        err = _errno()
        if err in (1, 13):    # EPERM/EACCES → bloqueado por seccomp (Landlock SÍ existe)
            return _ABI_BLOCKED
        # EINVAL(22)/ENOSYS(38)/otros → kernel sin Landlock (genuinamente ausente)
        return None
    return int(ret)


# ---------------------------------------------------------------------------
# Landlock struct (path_beneath_attr)
# ---------------------------------------------------------------------------

def _build_path_beneath_attr(allowed_access: int, parent_fd: int) -> bytes:
    """Construye landlock_path_beneath_attr como bytes para ctypes."""
    # struct landlock_path_beneath_attr { __u64 allowed_access; __s32 parent_fd; }
    # sizeof = 12 bytes (8 + 4), padding: compilador puede añadir 4 bytes al final → 16
    # Usar pack exacto que el kernel espera.
    return struct.pack("=QI", allowed_access, parent_fd)


# ---------------------------------------------------------------------------
# Core: apply ruleset
# ---------------------------------------------------------------------------

def _access_mask_for_rules(
    rules_accesses: list[frozenset[str]], abi_version: int
) -> int:
    """Calcula el handled_access_fs mask considerando el ABI disponible.

    Las access rights que exceden el ABI se descartan (degrade silencioso).
    """
    max_mask = _ABI_MAX_ACCESS_FS.get(abi_version, _ABI_MAX_ACCESS_FS[1])
    mask = 0
    for accesses in rules_accesses:
        for right in accesses:
            bit = _ACCESS_FS_MAP.get(right, 0)
            if bit & max_mask:
                mask |= bit
    return mask


def apply_ruleset(spec: object, abi_version: int) -> None:
    """Aplica el RulesetSpec al proceso actual via syscalls Landlock.

    Pasos:
      1. Construye handled_access_fs mask (degrada derechos fuera del ABI).
      2. landlock_create_ruleset → fd.
      3. Por cada PathRule: abre el path O_PATH, landlock_add_rule, cierra fd.
      4. prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0).
      5. landlock_restrict_self(fd, 0).

    Raises:
        LandlockSyscallError: si alguna syscall falla de forma no-degradable.
    """
    nr_create = _syscall_nr("landlock_create_ruleset")
    nr_add = _syscall_nr("landlock_add_rule")
    nr_restrict = _syscall_nr("landlock_restrict_self")

    rules = list(spec.rules)  # type: ignore[attr-defined]
    all_accesses = [r.accesses for r in rules]
    handled_mask = _access_mask_for_rules(
        [frozenset(str(a) for a in acc) for acc in all_accesses],
        abi_version,
    )

    if handled_mask == 0:
        # No hay reglas aplicables con este ABI — degrade completo pero seguro.
        logger.warning(
            "landlock_loader.no_applicable_rules abi=%d capability=%s",
            abi_version,
            getattr(spec, "capability", "unknown"),
        )
        return

    # 1. Crear ruleset
    # landlock_ruleset_attr { __u64 handled_access_fs; }
    attr_bytes = struct.pack("=Q", handled_mask)
    attr_c = ctypes.create_string_buffer(attr_bytes)
    ruleset_fd = _raw_syscall(
        nr_create,
        ctypes.addressof(attr_c),
        len(attr_bytes),
        0,
    )
    if ruleset_fd < 0:
        raise LandlockSyscallError(
            f"landlock_create_ruleset failed errno={_errno()} "
            f"handled_mask=0x{handled_mask:x}"
        )

    try:
        _add_path_rules(nr_add, ruleset_fd, rules, handled_mask, abi_version)
        _prctl_no_new_privs()
        _restrict_self(nr_restrict, ruleset_fd)
    finally:
        os.close(ruleset_fd)

    logger.info(
        "landlock_loader.applied abi=%d capability=%s rules=%d mask=0x%x",
        abi_version,
        getattr(spec, "capability", "unknown"),
        len(rules),
        handled_mask,
    )


def _add_path_rules(
    nr_add: int,
    ruleset_fd: int,
    rules: list[object],
    handled_mask: int,
    abi_version: int,
) -> None:
    max_mask = _ABI_MAX_ACCESS_FS.get(abi_version, _ABI_MAX_ACCESS_FS[1])

    for rule in rules:
        path = str(getattr(rule, "path", ""))
        accesses = getattr(rule, "accesses", frozenset())

        access_mask = 0
        for right in accesses:
            bit = _ACCESS_FS_MAP.get(str(right), 0)
            if bit & max_mask:
                access_mask |= bit

        if access_mask == 0:
            logger.debug("landlock_loader.rule_skipped_no_abi_support path=%s", path)
            continue

        if not Path(path).exists():
            # An EXECUTE rule that goes missing is more serious than a missing
            # read-only data dir: if the real Chromium / python interpreter tree
            # is absent here, the post-restrict_self execv will hit EACCES and the
            # browser fails to start. Log at ERROR so the operator sees WHY the
            # FS jail bound nothing for the executable (red-team PASS-2). We still
            # skip rather than abort — a missing data path must not break a build
            # variant that ships Chromium elsewhere; the loader stays fail-closed
            # because the kernel simply won't grant EXECUTE on an unlisted tree.
            grants_execute = bool(access_mask & _ACCESS_FS_MAP["execute"])
            log = logger.error if grants_execute else logger.warning
            log(
                "landlock_loader.rule_path_missing path=%s execute=%s — skipped",
                path,
                grants_execute,
            )
            continue

        try:
            # O_PATH (no O_NOFOLLOW) follows symlinks so the rule is anchored at
            # the RESOLVED real file/dir — e.g. /usr/bin/chromium-browser ->
            # /ms-playwright/chromium-*/chrome-linux/chrome and /usr/bin/python3 ->
            # the real interpreter. This is what makes the ruleset bind the ACTUAL
            # Chromium process image after execv.
            parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError as exc:
            logger.warning(
                "landlock_loader.rule_path_open_failed path=%s error=%s — skipped",
                path,
                exc,
            )
            continue

        try:
            attr_bytes = _build_path_beneath_attr(access_mask, parent_fd)
            attr_c = ctypes.create_string_buffer(attr_bytes)
            ret = _raw_syscall(
                nr_add,
                ruleset_fd,
                LANDLOCK_RULE_PATH_BENEATH,
                ctypes.addressof(attr_c),
                0,
            )
            if ret != 0:
                logger.warning(
                    "landlock_loader.add_rule_failed path=%s errno=%d — skipped",
                    path,
                    _errno(),
                )
        finally:
            os.close(parent_fd)


def _prctl_no_new_privs() -> None:
    lib = _libc()
    lib.prctl.restype = ctypes.c_int
    lib.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    ret = lib.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if ret != 0:
        raise LandlockSyscallError(f"prctl(PR_SET_NO_NEW_PRIVS) failed errno={_errno()}")


def _restrict_self(nr_restrict: int, ruleset_fd: int) -> None:
    ret = _raw_syscall(nr_restrict, ruleset_fd, 0)
    if ret != 0:
        raise LandlockSyscallError(
            f"landlock_restrict_self failed errno={_errno()}"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_and_apply(capability_name: str) -> int:
    """Carga y aplica el RulesetSpec para *capability_name*.

    Para Capability.BROWSER lee HERMES_BROWSER_SESSION del entorno para
    resolver el path de sesión concreto en el ruleset.

    Devuelve:
        0  — éxito o degrade (Landlock ausente/kernel viejo).
        2  — capability_name inválido.
        3  — error duro de syscall.
    """
    from hermes.agents_os.application.consent_manager import Capability  # noqa: PLC0415
    from hermes.agents_os.infrastructure.landlock_ruleset_builder import (  # noqa: PLC0415
        LandlockRulesetBuilder,
        build_browser_ruleset,
    )

    try:
        cap = Capability(capability_name.lower())
    except ValueError:
        logger.error(
            "landlock_loader.unknown_capability capability=%s", capability_name
        )
        return 2

    if cap == Capability.BROWSER:
        session_name = os.environ.get("HERMES_BROWSER_SESSION", "default")
        spec = build_browser_ruleset(session_name)
    else:
        spec = LandlockRulesetBuilder().build(cap)

    abi = _detect_abi()
    if abi == _ABI_BLOCKED:
        # V-5: Landlock EXISTE pero seccomp lo bloquea. NO degradar — fallar cerrado.
        # El jail (hermes-browser-jail) trata exit != 0 como fail-closed y NO lanza
        # el navegador con credenciales sin Landlock. Arreglo: allowlistar landlock_*
        # en el perfil seccomp.
        logger.error(
            "landlock_loader.blocked_by_seccomp capability=%s — FAIL-CLOSED: "
            "seccomp bloquea landlock_* (EPERM). Allowlista landlock_create_ruleset/"
            "add_rule/restrict_self en el perfil seccomp.",
            capability_name,
        )
        return 2
    if abi is None:
        logger.warning(
            "landlock_loader.unavailable_degraded capability=%s "
            "— kernel sin Landlock; confinamiento solo via systemd-run scope",
            capability_name,
        )
        return 0

    logger.info("landlock_loader.detected_abi abi=%d capability=%s", abi, capability_name)

    try:
        apply_ruleset(spec, abi)
    except UnsupportedArchError as exc:
        logger.warning("landlock_loader.unsupported_arch error=%s — degrade", exc)
        return 0
    except LandlockSyscallError as exc:
        logger.error("landlock_loader.syscall_error error=%s", exc)
        return 3

    return 0


# ---------------------------------------------------------------------------
# __main__ entrypoint
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        logger.error("usage: python3 -m hermes.security.landlock_loader <CAPABILITY>")
        return 2
    return load_and_apply(args[0])


if __name__ == "__main__":
    sys.exit(main())
