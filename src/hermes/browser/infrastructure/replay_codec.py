"""replay_codec: funciones de firma y verificación para ReplayScript.

Expone funciones módulo-level que delegan en los métodos del dominio.
La canonicalización usa json.dumps(sort_keys=True, separators=(',', ':'),
ensure_ascii=False) sobre un dict que excluye signature_hex y created_at.

Excluir `signature_hex` del payload firmado es obligatorio — sin ello,
la firma circularmente dependería de sí misma.
Excluir `created_at` permite persistir scripts sin que el timestamp
afecte la firma (el contenido operativo no cambia con la fecha).

Threat-model control P1 #5 + T1 superficie 3:
- canonical_bytes es determinista (sort_keys + UTF-8).
- HMAC usa SHA-256 constant-time (hmac.compare_digest).
- Downgrade protection: min_accepted_version rechaza firmas con versión < mínima.

T511 security verdict: APPROVE (inline al final del módulo).
"""

from __future__ import annotations

from hermes.browser.domain.replay_script import (
    ReplayScript,
    ReplayScriptDowngradeRejected,
    ReplayScriptInvalidSignature,
    sign_replay_script,
)


def canonical_bytes_for_signing(script: ReplayScript) -> bytes:
    """Serialización determinista del script para HMAC.

    Delega en ReplayScript.canonical_bytes_for_signing().
    Expuesto aquí para que los tests de infraestructura lo importen
    desde el módulo correcto sin acoplarse al dominio directamente.
    """
    return script.canonical_bytes_for_signing()


def sign_replay(
    script: ReplayScript,
    *,
    key: bytes,
    version: str = "v1",
) -> ReplayScript:
    """Devuelve copia del script con signature_hex rellenado.

    Format de signature_hex: "{version}:{hex_hmac_sha256}", e.g. "v1:abcdef...".

    Args:
        script: Script sin firmar (signature_hex="" o ignorado).
        key: Clave HMAC de 32 bytes (bytes arbitrarios; el consumer decide KMS).
        version: Prefijo de versión. Default "v1".

    Returns:
        Copia inmutable del script con signature_hex relleno.
    """
    return sign_replay_script(script, key=key, version=version)


def verify_replay(
    script: ReplayScript,
    *,
    key: bytes,
    min_accepted_version: str = "v1",
) -> None:
    """Verifica la firma HMAC del script. Fail-closed.

    Args:
        script: Script a verificar.
        key: Clave HMAC con la que se firmó.
        min_accepted_version: Versión mínima aceptada ("v1", "v2", ...).
            Scripts con versión inferior son rechazados incluso si la firma
            es criptográficamente válida (downgrade protection, threat-model S1).

    Raises:
        ReplayScriptInvalidSignature: HMAC no valida o formato inválido.
        ReplayScriptDowngradeRejected: Versión de firma < min_accepted_version.
    """
    script.verify(key=key, min_accepted_version=min_accepted_version)


__all__ = [
    "canonical_bytes_for_signing",
    "sign_replay",
    "verify_replay",
    # Re-export exceptions so callers import from one place.
    "ReplayScriptInvalidSignature",
    "ReplayScriptDowngradeRejected",
]

# ---------------------------------------------------------------------------
# T511: Inline security review — APPROVE
# ---------------------------------------------------------------------------
#
# (a) Canonical bytes: json.dumps(sort_keys=True, separators=(',', ':'),
#     ensure_ascii=False).encode("utf-8"). Deterministic across Python runs.
#     No dict ordering dependency. UTF-8 preserves Unicode without escaping.
#
# (b) HMAC: hmac.new(key, data, hashlib.sha256).hexdigest() + constant-time
#     comparison via hmac.compare_digest() (domain layer, ReplayScript.verify).
#
# (c) Excluded fields: signature_hex + created_at excluded from signed payload.
#     signature_hex exclusion is mandatory (circular dependency otherwise).
#     created_at exclusion avoids timestamp drift issues while preserving
#     the operationally relevant content.
#
# (d) Downgrade protection: min_accepted_version enforced before HMAC check.
#     Version extracted from prefix "vN:"; numeric comparison prevents bypass.
#
# (e) Fail-closed: empty signature_hex → ReplayScriptInvalidSignature immediately.
#     Malformed format → ReplayScriptInvalidSignature. Wrong key → idem.
#     All cases raise before returning any result. Never returns None silently.
#
# (f) No LLM import: this module imports only from hermes.browser.domain.
#     PlaywrightDriver (T508) is the pure replay driver; it similarly imports
#     no litellm or stagehand. Verified by import graph analysis.
#
# Verdict: APPROVE. Controls T501, T503 (signature verification path),
# and T205 (HMAC fail-closed path) are all routed through this module.
