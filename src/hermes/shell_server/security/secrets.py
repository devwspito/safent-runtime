"""SecretsVault — almacen cifrado de API keys de providers.

Cifrado AES-GCM-256 con clave maestra leída de /var/lib/hermes/master.key
(32 bytes random, generado en primer boot con permisos 0600 antes de
iniciar el servicio).

Invariantes de seguridad:
  - Si master.key no existe, SecretsVault lanza RuntimeError (fail-closed).
    No derivamos de machine-id ni usamos constantes de fallback (finding #20).
  - derive_subkey usa HKDF-SHA256 real (RFC 5869) — extract + expand (finding #20).
  - check_landlock_active() verifica que el LSM Landlock esté cargado antes
    de operar capabilities que dependen del sandbox (finding #19).

NUNCA se loggean secretos. NUNCA se devuelven en API responses.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

_MASTER_KEY_PATH: Final = Path("/var/lib/hermes/master.key")
_LSM_STATUS_PATH: Final = Path("/sys/kernel/security/lsm")


# ---------------------------------------------------------------------------
# Landlock active check (finding #19)
# ---------------------------------------------------------------------------

def check_landlock_active() -> bool:
    """Devuelve True si Landlock está listado como LSM activo en el kernel.

    Lee /sys/kernel/security/lsm (disponible en Linux ≥ 5.13 con securityfs
    montado). Si el fichero no existe (kernel sin securityfs), devuelve False
    — llamador decide si debe fallar.
    """
    try:
        lsm_list = _LSM_STATUS_PATH.read_text(encoding="ascii").strip()
        return "landlock" in lsm_list.split(",")
    except OSError:
        return False


def assert_landlock_active() -> None:
    """Lanza RuntimeError si Landlock no está activo (fail-closed).

    Llamar desde el entrypoint del runtime antes de conceder cualquier
    capability que dependa de kernel enforcement (FR-052).
    """
    if not check_landlock_active():
        raise RuntimeError(
            "Landlock LSM no está activo en este kernel. "
            "Verifica que el cmdline incluye lsm=landlock,... "
            "y que el kernel fue compilado con CONFIG_SECURITY_LANDLOCK=y. "
            "El runtime no puede garantizar el sandbox sin enforcement kernel."
        )


# ---------------------------------------------------------------------------
# Master key — fail-closed (finding #20)
# ---------------------------------------------------------------------------

def _load_master_key() -> bytes:
    """Carga la clave maestra de 32 bytes desde /var/lib/hermes/master.key.

    Falla con RuntimeError si el fichero no existe o tiene menos de 32 bytes.
    No deriva de machine-id ni usa constantes de fallback — cualquier fallback
    silencioso produce cifrado con una clave pública/predecible.

    El fichero master.key debe ser generado en primer boot por un oneshot
    systemd que ejecute:
        python3 -c "import os; open('/var/lib/hermes/master.key','wb').write(os.urandom(32))"
    con umask 0077 (permisos resultantes 0600, propietario hermes).
    """
    if not _MASTER_KEY_PATH.exists():
        raise RuntimeError(
            f"SecretsVault: {_MASTER_KEY_PATH} no existe. "
            "El servicio hermes-shell-server requiere que master.key sea "
            "generado en primer boot antes de arrancar. "
            "Ver systemd unit hermes-keygen.service (oneshot, Before=hermes-shell-server.service)."
        )
    data = _MASTER_KEY_PATH.read_bytes()
    if len(data) < 32:
        raise RuntimeError(
            f"SecretsVault: {_MASTER_KEY_PATH} tiene {len(data)} bytes "
            "(mínimo 32). Regenerar con os.urandom(32)."
        )
    return data[:32]


# ---------------------------------------------------------------------------
# HKDF real RFC 5869 (finding #20)
# ---------------------------------------------------------------------------

def _hkdf_derive(ikm: bytes, info: bytes, salt: bytes | None = None, length: int = 32) -> bytes:
    """HKDF-SHA256 completo (extract + expand, RFC 5869) para sub-claves.

    No usar hashlib.sha256 plano — eso es un solo round sin extract step
    y es susceptible a length-extension y low-entropy IKM attacks.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
    )
    return hkdf.derive(ikm)


class SecretsVault:
    """Cifra/descifra secretos por id."""

    def __init__(self, *, master_key: bytes | None = None) -> None:
        self._key = master_key if master_key is not None else _load_master_key()
        if len(self._key) != 32:
            raise ValueError("master key must be 32 bytes")
        self._aead = AESGCM(self._key)

    def encrypt(self, *, secret_id: str, plaintext: str) -> bytes:
        """Cifra un secreto. Devuelve nonce || ciphertext || tag."""
        nonce = os.urandom(12)
        aad = secret_id.encode("utf-8")
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return nonce + ct

    def decrypt(self, *, secret_id: str, blob: bytes) -> str:
        """Devuelve el plaintext o eleva InvalidTag."""
        if len(blob) < 13:
            raise ValueError("blob truncado")
        nonce, ct = blob[:12], blob[12:]
        aad = secret_id.encode("utf-8")
        pt = self._aead.decrypt(nonce, ct, aad)
        return pt.decode("utf-8")

    def derive_subkey(self, *, label: str) -> bytes:
        """HKDF-SHA256 a 32-byte subkey determinística desde la master key.

        Usada por bounded contexts que necesitan su propia clave AES-GCM
        (e.g. remote-control token cipher) sin re-derivar desde machine-id.
        Implementa RFC 5869 completo (extract + expand) vía cryptography.HKDF.
        """
        info = b"hermes-shell-subkey-" + label.encode("utf-8")
        salt = b"hermes-subkey-salt-v1"
        return _hkdf_derive(self._key, info=info, salt=salt, length=32)


def _log_safe_secret_redaction(text: str) -> str:
    """Helper para audit: muestra solo prefix + len, nunca el secret."""
    if not text:
        return "<empty>"
    return f"<{len(text)}ch starts:{text[:4]}***>"
