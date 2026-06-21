"""ProtectedServiceDenylist — denylist dura anti-autopirateo (CTRL-P2-2/3).

Rechaza CUALQUIER operación sobre servicios que son los propios frenos del
agente. Decisión TERMINAL e inapelable por HITL (NFR-002). Evaluada ANTES de
cualquier llamada a systemd (FR-008/FR-009).

CTRL-P2-3 — resolución por identidad canónica:
    Normaliza la cadena pedida (sufijo .service implícito + case-insensitive)
    antes de comparar. En producción, la canonicalización real se haría vía
    `systemctl show -p Id,Names <unit>` para resolver aliases y symlinks.
    En entornos sin systemd (tests/CI) se usa la normalización por sufijo
    + lowercasing + alias-table (documentado aquí).

El conjunto protegido es configurable pero con un default endurecido que
representa el mínimo inviolable del sistema. NO es editable por el agente.

Capa: infrastructure (implementa ProtectedServiceDenylistPort del contrato).
Sin framework. Sin I/O en el path caliente.
"""

from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger("hermes.capabilities.protected_service_denylist")

# ---------------------------------------------------------------------------
# Conjunto mínimo inviolable (F-1 del threat-model, firmado por security-engineer)
# Los 5 servicios cuyos nombres canónicos son raíz de la denylist.
# ---------------------------------------------------------------------------
_DEFAULT_PROTECTED_BASES: Final[frozenset[str]] = frozenset({
    "hermes-runtime",
    "hermes-shell-server",
    "hermes-consent",
    "hermes-audit",
    "hermes-keygen",
    # hermes-llm (P3): the agent has no legitimate need to stop/restart its own
    # inference brain. Protecting it removes the DoS-restart vector and the
    # self-deafening footgun (security review P3, HIGH defense-in-depth).
    "hermes-llm",
    # Kernel confinement services — stopping these silences the network sandbox
    # (netns/nftables egress control) and the browser isolation layer.
    # Added as part of the terminal self-jailbreak hardening (anti-autopirateo).
    "hermes-browser-netns",
    "hermes-egress-proxy",
})


def _canonicalize(unit: str) -> str:
    """Normaliza una cadena de nombre de unit a su forma canónica base.

    Pasos:
    1. Strip whitespace.
    2. Lowercase.
    3. Eliminar sufijo .service si presente (la base es el nombre sin sufijo).

    Nota sobre prod vs tests:
        En producción se debería usar `systemctl show -p Id,Names <unit>` para
        obtener el Id canónico y compararlo contra el set protegido. Esta función
        implementa la normalización léxica que cubre el 99% de los casos
        (hermes-runtime, hermes-runtime.service, Hermes-Runtime, etc.).
        Los aliases verdaderos de systemd (distintos del nombre base + .service)
        se cubrirían con `systemctl show` en el adaptador de producción.
    """
    name = unit.strip().lower()
    if name.endswith(".service"):
        name = name[: -len(".service")]
    return name


class ProtectedServiceDenylist:
    """Denylist dura anti-autopirateo. Implementa ProtectedServiceDenylistPort.

    Args:
        extra_protected: nombres adicionales a proteger (sin sufijo .service).
            Se fusionan con el default mínimo inviolable. No puede reducir
            el conjunto default — solo ampliarlo.
    """

    def __init__(self, *, extra_protected: frozenset[str] | None = None) -> None:
        base = _DEFAULT_PROTECTED_BASES
        if extra_protected:
            base = base | frozenset(
                _canonicalize(n) for n in extra_protected
            )
        # El conjunto canónico es inmutable en tiempo de ejecución.
        self._protected: Final[frozenset[str]] = frozenset(
            _canonicalize(n) for n in base
        )

    def is_protected(self, unit: str) -> bool:
        """True si `unit` resuelve a un servicio protegido (CTRL-P2-2/3).

        Fail-closed: si la canonicalización produce cadena vacía, trata como
        protegido (no se puede canonicalizar = dudoso = deniega).
        """
        canonical = _canonicalize(unit)
        if not canonical:
            return True  # fail-closed
        return canonical in self._protected

    def is_protected_canonical(self, unit: str) -> bool:
        """True si `unit` resuelve a un servicio protegido usando identidad canónica completa.

        CONDITION-2 (FR-009): intenta `systemctl show -p Id,Names <unit>` para
        resolver aliases y symlinks reales. Extrae el Id canónico y Names y los
        compara contra el conjunto protegido.

        Fail-closed: si la resolución via systemd falla por cualquier razón
        (systemd no disponible, timeout, error de parsing), cae al método
        léxico `is_protected(unit)`. Si este tampoco puede resolver (cadena
        vacía), trata como protegido.
        """
        output = None
        try:
            output = self._systemctl_show_id_names(unit)
        except Exception:  # noqa: BLE001 — any error falls through to lexical
            logger.debug("hermes.denylist.systemctl_show_failed: %r", unit)
        if output:
            return self._check_ids_from_show_output(output)
        # Fallback: lexical resolution (covers 99% of cases + fail-closed on empty).
        return self.is_protected(unit)

    def _systemctl_show_id_names(self, unit: str) -> str | None:
        """Runs `systemctl show -p Id,Names <unit>` and returns raw output.

        Returns None if systemctl is not available or fails.
        Separated for mockability in tests (CONDITION-2).
        """
        import subprocess  # noqa: PLC0415
        try:
            result = subprocess.run(  # noqa: S603 — trusted system binary; unit validated by caller
                ["/usr/bin/systemctl", "show", "-p", "Id,Names", unit],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    def _check_ids_from_show_output(self, output: str) -> bool:
        """Extracts Id= and Names= from systemctl show output and checks against protected set."""
        props: dict[str, str] = {}
        for line in output.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()

        # Check the canonical Id field.
        canonical_id = _canonicalize(props.get("Id", ""))
        if canonical_id and canonical_id in self._protected:
            return True

        # Check all Names (space-separated aliases).
        for name in props.get("Names", "").split():
            if _canonicalize(name) in self._protected:
                return True

        # If we got output but no match, the unit is not protected.
        # (Fallback to lexical for empty output handled by is_protected_canonical caller.)
        return False

    def protected_canonical_names(self) -> frozenset[str]:
        """Devuelve el conjunto canónico de nombres protegidos (observabilidad)."""
        return self._protected
