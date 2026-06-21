"""NativeKeyStoreAdapter — KmsSigningKeyPort backed by SecretsVault.

P0-4: cablea la clave nativa del SO (derive_subkey del master.key generado
por hermes-keygen) en vez del HMAC derivado de la ruta del fichero de DB.

Diseño:
  - SecretsVault.derive_subkey(label="skill-signing-v2") entrega 32 bytes
    derivados con HKDF-SHA256 real (RFC 5869) desde master.key (32 bytes
    aleatorios generados en primer boot, permisos 0600 hermes:hermes).
  - KmsSigningKeyPort.get_signing_key ignora tenant_id porque en Agents OS
    single-tenant la clave es la misma para todos; multi-tenant introducirá
    key_id real más adelante.
  - Fail-closed: si master.key no existe (sin hermes-keygen), lanza
    SigningKeyError. El caller NO debe hacer fallback al HMAC-por-ruta.

Migración (v1 → v2):
  - Las skills existentes en skill_packages_view tienen signing_method='v1'
    (o NULL, tratado como v1).  Siguen siendo verificables con el helper
    build_signing_key_v1(db_path) de persist.py durante la ventana de
    migración.
  - Skills nuevas se firman siempre con v2 (native keystore).
  - NO invalidamos ni borramos skills v1 — la función de verificación
    read-only puede leer signing_method y elegir el método correcto.

Seguridad:
  - La clave ya NO es derivable conociendo la ruta del fichero DB (CWE-321).
  - Cada instancia de Agents OS tiene una clave distinta (master.key único).
  - El material no sale del proceso; no se loggea (CTRL-NOSECRET).
"""

from __future__ import annotations

import logging
from uuid import UUID

from hermes.shell_server.security.secrets import SecretsVault
from hermes.training.application.skill_signer import KmsSigningKeyPort, SigningKeyError

logger = logging.getLogger(__name__)

_SKILL_SIGNING_LABEL = "skill-signing-v2"


class NativeKeyStoreAdapter:
    """Implementa KmsSigningKeyPort delegando en SecretsVault.derive_subkey.

    La SecretsVault carga master.key en primer uso. Si master.key no existe
    (entorno sin hermes-keygen) el constructor falla con SigningKeyError para
    que el caller lo detecte en startup, no en el primer intento de firma.
    """

    def __init__(self) -> None:
        try:
            self._vault = SecretsVault()
        except RuntimeError as exc:
            raise SigningKeyError(
                "NativeKeyStoreAdapter: SecretsVault no pudo cargar master.key. "
                "Verifica que hermes-keygen.service haya completado antes de "
                "usar skill signing nativo."
            ) from exc

    async def get_signing_key(
        self,
        *,
        tenant_id: UUID,  # noqa: ARG002 — single-tenant en Agents OS Edition
        key_id: str,  # noqa: ARG002 — label fijo en v2; expandible a multi-key
    ) -> bytes:
        """Deriva la sub-clave de firma de skills desde master.key.

        Raises:
            SigningKeyError: si la derivación falla (master.key corrupta).
        """
        try:
            return self._vault.derive_subkey(label=_SKILL_SIGNING_LABEL)
        except Exception as exc:
            raise SigningKeyError(
                f"NativeKeyStoreAdapter: derive_subkey falló: {exc}"
            ) from exc

    def get_signing_key_sync(self) -> bytes:
        """Versión síncrona para los callers que no son async (persist.py).

        Raises:
            SigningKeyError: si la derivación falla.
        """
        try:
            return self._vault.derive_subkey(label=_SKILL_SIGNING_LABEL)
        except Exception as exc:
            raise SigningKeyError(
                f"NativeKeyStoreAdapter: derive_subkey falló: {exc}"
            ) from exc
