"""Clave HMAC de firma del audit chain — sello estable inyectado (CTRL-7/AUD-1).

Threat-model: AUD-1 — elimina `build_signing_key(db_path)=sha256(path)` (CWE-321)
y el `secrets.token_bytes(32)` efímero del shell_server. La clave proviene
ÚNICAMENTE de un sello inyectado desde fuera del proceso (env var `HERMES_AUDIT_KEY`
en producción cargado desde un secreto LUKS/TPM2 por el unit de systemd).

En producción el sello se inyecta así (hardened systemd unit):
    [Service]
    EnvironmentFile=/run/credentials/hermes-runtime.service/audit_key
    # /run/credentials/... se monta desde el secreto LUKS/TPM sellado en el boot

En tests se inyecta con `monkeypatch.setenv("HERMES_AUDIT_KEY", key.hex())`.

NUNCA debe:
  - derivar la clave de una ruta del filesystem (sha256(db_path)).
  - generar la clave con secrets.token_bytes() en el arranque (efímera).
  - exponer la clave en logs o en mensajes de error.
"""

from __future__ import annotations

import os


class MissingAuditSeal(RuntimeError):
    """El sello de firma del audit chain no está configurado.

    Fail-closed: el daemon NO puede arrancar sin una clave estable y sellada.
    Configura HERMES_AUDIT_KEY con el hex de un secreto de 32+ bytes desde
    el mecanismo de bootstrap de secretos del SO (LUKS/TPM2/systemd credentials).
    """


_ENV_VAR = "HERMES_AUDIT_KEY"
_MIN_KEY_BYTES = 32


def load_signing_key() -> bytes:
    """Carga la clave HMAC del audit chain desde el sello inyectado.

    Fuente: variable de entorno `HERMES_AUDIT_KEY` (hex-encoded).
    En producción esta var la pone el unit de systemd desde /run/credentials/
    (secreto LUKS o TPM2 sellado).

    Returns:
        bytes de al menos 32 bytes listos para AuditHashChainSigner.

    Raises:
        MissingAuditSeal: si la variable no está, está vacía, o el valor
            hexadecimal es demasiado corto (< 32 bytes). Fail-closed.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        raise MissingAuditSeal(
            f"{_ENV_VAR} no está configurada. "
            "El daemon de audit NO puede arrancar sin una clave sellada. "
            "Configura el secreto desde LUKS/TPM2 vía systemd credentials."
        )

    key = _decode_hex_seal(raw)
    _assert_min_length(key)
    return key


def _decode_hex_seal(raw: str) -> bytes:
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise MissingAuditSeal(
            f"{_ENV_VAR} contiene un valor que no es hex válido."
        ) from exc


def _assert_min_length(key: bytes) -> None:
    if len(key) < _MIN_KEY_BYTES:
        raise MissingAuditSeal(
            f"{_ENV_VAR} demasiado corta: {len(key)} bytes < mínimo {_MIN_KEY_BYTES}. "
            "Usa un secreto de al menos 32 bytes."
        )


def load_signing_key_with_fallback() -> bytes:
    """Same priority chain as the main daemon's boot sequence:

      1. HERMES_AUDIT_KEY (hex) — systemd credentials, production.
      2. SecretsVault.derive_subkey("audit-chain") — HKDF from master.key
         (per-install, stable, same file every process on this host reads).

    This is the SAME key `AuditHashChainSigner` and `HitlApprovalMinter` use
    in the main runtime daemon (`runtime/__main__.py`'s `_load_signing_key_or_
    fail`, now a thin wrapper around this function). Extracted here (Fase 2
    Phase 4e) so a SEPARATE process — `hermes.config_sync`'s `remote_approvals`
    module runs as its own systemd unit, not in-process with the runtime
    daemon — can independently reproduce the IDENTICAL key (master.key is a
    shared per-install file, not process-local state) to mint an HITL approval
    token the main daemon's own gate can later verify.

    Raises:
        MissingAuditSeal: neither source produces a usable key (fail-closed).
    """
    try:
        return load_signing_key()
    except MissingAuditSeal:
        pass

    try:
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

        return SecretsVault().derive_subkey(label="audit-chain")
    except RuntimeError as exc:
        raise MissingAuditSeal(
            f"ni {_ENV_VAR} ni master.key están disponibles — "
            f"clave de firma no derivable. Detalle: {exc}"
        ) from exc
