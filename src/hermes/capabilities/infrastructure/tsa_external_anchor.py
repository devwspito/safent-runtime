"""T042 — Implementaciones de ExternalAnchorPort (CTRL-8/AUD-2).

WormFileAnchor:
  Append-only a un fichero local (una línea por ancla, con timestamp).
  Simple y funcional en P0. Mitiga tampering accidental y herramientas
  básicas de modificación. No protege contra root con acceso al fichero.
  Útil como primera capa antes de TSA.

TsaExternalAnchor:
  RFC-3161 (Time Stamping Authority) contra freeTSA.org. Provee no-repudio
  fuerte: la TSA firma el hash con su clave ECDSA+SHA-512 y su timestamp —
  ningún atacante con root local puede retroactivamente forjar esa firma.

  Librería elegida: rfc3161ng==2.1.3 (MIT, mantenida, Python 3.12 OK).
  rfc3161ng.check_timestamp no soporta ECDSA (hardcodea RSA+PKCS1v15).
  FreeTSA.org usa ECDSA con P-384 y SHA-512 para la firma CMS — verificamos
  la firma directamente con `cryptography` (ya instalado, Tier 0 dep).

  Postura de red (fail-open / best-effort):
  anchor() encola el head y trata de vaciar la cola (POST al TSA). Si la
  red falla, el head queda en _pending_queue.json y anchor() devuelve
  "pending:<hash>". El append de audit NO se bloquea.
  verify() retorna False para heads pendientes (no hay TST firmado aún);
  la ventana de no-repudio débil está acotada por la siguiente llamada
  exitosa a anchor(). Los heads en cola se loguean como ERROR para que
  el operador lo detecte.

CompositeExternalAnchor:
  Combina WormFileAnchor (local, detección rápida de truncado sin red)
  con TsaExternalAnchor (externo, no-repudio fuerte). anchor() llama a
  ambos; verify() requiere que AMBOS confirmen para retornar True.

Capa: infrastructure (I/O: fichero local, HTTP externo).
"""

from __future__ import annotations

import hmac
import json
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from hermes.capabilities.application.external_anchor import ExternalAnchorPort

if TYPE_CHECKING:
    pass

logger = logging.getLogger("hermes.capabilities.tsa_anchor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FREETSA_URL = "https://freetsa.org/tsr"

# Bundled CA cert for freeTSA.org (self-signed root, valid until 2041).
# Obtained from https://freetsa.org/tsa.crt on 2026-05-31.
# This file is at src/hermes/capabilities/infrastructure/; five .parent hops
# reach the repo root, where ops/audit/freetsa_tsa.crt lives.
_BUNDLED_CERT_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "ops"
    / "audit"
    / "freetsa_tsa.crt"
)

_HTTP_TIMEOUT_S = 15.0
_PENDING_REF_PREFIX = "pending:"


# ---------------------------------------------------------------------------
# Helper — UTC clock (injectable in tests)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Transport protocol — injectable for tests
# ---------------------------------------------------------------------------


class TsaTransport(Protocol):
    """HTTP transport for TSA requests. Injected for testability."""

    async def post_timestamp_query(
        self, *, url: str, body: bytes, timeout_s: float
    ) -> bytes:
        """POST body to url, return raw response bytes.

        Raises:
            TsaNetworkError: on connection / timeout failures.
        """
        ...


class TsaNetworkError(OSError):
    """Raised when the TSA endpoint cannot be reached."""


class TsaProtocolError(ValueError):
    """Raised when the TSA response is malformed or status != granted."""


# ---------------------------------------------------------------------------
# Default httpx transport (production)
# ---------------------------------------------------------------------------


class _HttpxTsaTransport:
    """Production transport using httpx async client."""

    async def post_timestamp_query(
        self, *, url: str, body: bytes, timeout_s: float
    ) -> bytes:
        import httpx  # deferred — not at module level to keep domain pure  # noqa: PLC0415

        headers = {"Content-Type": "application/timestamp-query"}
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                response = await client.post(url, content=body, headers=headers)
                response.raise_for_status()
                return response.content
        except httpx.TimeoutException as exc:
            raise TsaNetworkError(f"TSA request timed out: {url}") from exc
        except httpx.RequestError as exc:
            raise TsaNetworkError(f"TSA network error: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise TsaProtocolError(
                f"TSA returned HTTP {exc.response.status_code}"
            ) from exc


# ---------------------------------------------------------------------------
# RFC-3161 codec helpers
# ---------------------------------------------------------------------------


def _build_timestamp_request(digest_hex: str) -> bytes:
    """Build a DER-encoded TimeStampReq for digest_hex (SHA-256 imprint)."""
    import rfc3161ng  # noqa: PLC0415
    from pyasn1.codec.der import encoder  # type: ignore[import-untyped]  # noqa: PLC0415

    digest_bytes = bytes.fromhex(digest_hex)
    req = rfc3161ng.make_timestamp_request(
        digest=digest_bytes,
        hashname="sha256",
        include_tsa_certificate=True,
    )
    return encoder.encode(req)  # type: ignore[no-any-return]


def _decode_timestamp_response_from_bytes(raw: bytes) -> object:
    """Decode DER bytes into a rfc3161ng.TimeStampResp."""
    import rfc3161ng  # noqa: PLC0415

    return rfc3161ng.decode_timestamp_response(raw)


def _extract_tst_bytes(tsr: object) -> bytes:
    """Extract the DER-encoded TimeStampToken from a decoded TSR.

    Raises:
        TsaProtocolError: if status != granted or token is absent.
    """
    from pyasn1.codec.der import encoder  # type: ignore[import-untyped]  # noqa: PLC0415

    # PKIStatusInfo.status: 0=granted, 1=grantedWithMods, 2+=rejection.
    # Accessed via getComponentByName for compatibility with pyasn1 and fakes.
    pki_status = tsr.status  # type: ignore[attr-defined]
    try:
        status_int = int(pki_status.getComponentByName("status"))
    except AttributeError:
        status_int = int(pki_status)
    if status_int not in (0, 1):  # noqa: PLR2004
        raise TsaProtocolError(f"TSA rejected request, status={status_int}")
    tst = tsr.time_stamp_token  # type: ignore[attr-defined]
    if tst is None:
        raise TsaProtocolError("TSA response missing TimeStampToken")
    return bytes(encoder.encode(tst))


def _verify_cert_chain(tsa_cert: object, ca_cert_pem: bytes) -> bool:
    """Verify TSA signing cert is issued by the CA (issuer name + RSA sig)."""
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.backends import default_backend  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding  # noqa: PLC0415

    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem, default_backend())
    tsa = tsa_cert  # type: ignore[assignment]
    if tsa.issuer != ca_cert.subject:  # type: ignore[union-attr]
        logger.warning(
            "hermes.tsa.cert_chain_mismatch: issuer %s != CA subject %s",
            tsa.issuer.rfc4514_string(),  # type: ignore[union-attr]
            ca_cert.subject.rfc4514_string(),
        )
        return False
    try:
        ca_cert.public_key().verify(  # type: ignore[union-attr]
            tsa.signature,  # type: ignore[union-attr]
            tsa.tbs_certificate_bytes,  # type: ignore[union-attr]
            asym_padding.PKCS1v15(),
            tsa.signature_hash_algorithm,  # type: ignore[union-attr]
        )
        return True
    except Exception as exc:
        logger.warning("hermes.tsa.ca_chain_invalid: %s", exc)
        return False


def _verify_cms_signature(*, tsa_cert: object, signed_data: object) -> bool:
    """Verify CMS signer signature (ECDSA+SHA-512) on authenticated attributes."""
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from pyasn1.codec.der import encoder  # type: ignore[import-untyped]  # noqa: PLC0415
    from pyasn1.type import univ  # type: ignore[import-untyped]  # noqa: PLC0415

    signer_info = signed_data["signerInfos"][0]  # type: ignore[index]
    signature = bytes(signer_info["encryptedDigest"])
    auth_attrs = signer_info["authenticatedAttributes"]
    attr_set = univ.SetOf()
    for i, attr in enumerate(auth_attrs):
        attr_set.setComponentByPosition(i, attr)
    signed_blob = encoder.encode(attr_set)

    try:
        tsa_cert.public_key().verify(signature, signed_blob, ec.ECDSA(hashes.SHA512()))  # type: ignore[union-attr]
        return True
    except Exception as exc:
        logger.warning("hermes.tsa.signature_invalid: %s", exc)
        return False


def _verify_tst(
    *, tst_bytes: bytes, expected_digest_hex: str, ca_cert_pem: bytes
) -> bool:
    """Verify a TST against the TSA CA cert and the expected message imprint.

    Returns True only if all of the following hold:
    1. The message imprint in the TST matches expected_digest_hex (SHA-256).
    2. The TSA signing cert is issued by the CA (issuer name match + RSA sig).
    3. The TST CMS signature verifies with the TSA signing cert (ECDSA+SHA-512).

    FreeTSA.org specifics:
    - TSA signing cert: ECDSA P-384, signed by RSA CA.
    - CMS signer digest algo: SHA-512 (OID 2.16.840.1.101.3.4.2.3).
    - CA cert: RSA self-signed root bundled at ops/audit/freetsa_tsa.crt.
    """
    import rfc3161ng  # noqa: PLC0415
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.backends import default_backend  # noqa: PLC0415
    from pyasn1.codec.der import decoder, encoder  # type: ignore[import-untyped]  # noqa: PLC0415
    from pyasn1.type import univ  # type: ignore[import-untyped]  # noqa: PLC0415

    expected_digest = bytes.fromhex(expected_digest_hex)

    try:
        tst, _ = decoder.decode(tst_bytes, asn1Spec=rfc3161ng.TimeStampToken())
    except Exception as exc:
        logger.warning("hermes.tsa.tst_decode_error: %s", exc)
        return False
    signed_data = tst.content  # type: ignore[attr-defined]

    # 1. Verify message imprint matches expected hash.
    tstinfo_raw = signed_data["contentInfo"]["content"]
    tstinfo_oct, _ = decoder.decode(bytes(tstinfo_raw), asn1Spec=univ.OctetString())
    tstinfo, _ = decoder.decode(bytes(tstinfo_oct), asn1Spec=rfc3161ng.TSTInfo())
    mi = tstinfo["messageImprint"]
    hashed_msg = bytes(mi["hashedMessage"])
    if not hmac.compare_digest(hashed_msg, expected_digest):
        logger.warning("hermes.tsa.imprint_mismatch: TST message imprint != expected")
        return False

    # 2. Extract TSA signing cert (position 0 in CMS certificates).
    certs = signed_data["certificates"]
    if len(certs) < 1:
        logger.warning("hermes.tsa.no_cert_in_token")
        return False
    tsa_cert_der = encoder.encode(certs[0][0])
    tsa_cert = x509.load_der_x509_certificate(tsa_cert_der, default_backend())

    # 3. Verify cert chain and CMS signature.
    return _verify_cert_chain(tsa_cert, ca_cert_pem) and _verify_cms_signature(
        tsa_cert=tsa_cert, signed_data=signed_data
    )


# ---------------------------------------------------------------------------
# WormFileAnchor
# ---------------------------------------------------------------------------


class WormFileAnchor:
    """Ancla append-only sobre un fichero local.

    Cada llamada a anchor() añade una línea:
        {iso_timestamp} {head_hash_hex}

    get_latest() devuelve el hash de la última línea.
    verify() compara el head local con la última ancla.

    El fichero NUNCA se trunca — el llamador NO debe sobrescribirlo.
    La atomicidad en appends es suficiente para P0 single-process.

    Args:
        anchor_path: ruta al fichero ancla. Se crea si no existe.
                     Debe estar en una ruta distinta a la DB principal.
    """

    def __init__(self, *, anchor_path: Path) -> None:
        self._path = anchor_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch(mode=0o600)

    async def anchor(self, head_hash_hex: str) -> str:
        """Añade el hash al fichero ancla. Devuelve una ref con timestamp."""
        ts = datetime.now(tz=UTC).isoformat()
        line = f"{ts} {head_hash_hex}\n"
        with self._path.open("a") as f:
            f.write(line)
        return ts

    async def get_latest(self) -> str | None:
        """Devuelve el hash de la última línea del fichero ancla."""
        try:
            text = self._path.read_text()
        except OSError:
            return None
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        last = lines[-1]
        parts = last.split(" ", 1)
        return parts[1] if len(parts) == 2 else None  # noqa: PLR2004

    async def verify(self, local_head: str) -> bool:
        """True si local_head coincide con la última ancla.

        Fail-closed: False si no hay anclas o si divergen.
        """
        latest = await self.get_latest()
        if latest is None:
            return False
        return hmac.compare_digest(local_head, latest)


assert isinstance(
    WormFileAnchor(
        anchor_path=Path(tempfile.gettempdir()) / "__hermes_worm_anchor_typecheck.tmp"
    ),
    ExternalAnchorPort,
)


# ---------------------------------------------------------------------------
# TsaExternalAnchor
# ---------------------------------------------------------------------------


class TsaExternalAnchor:
    """RFC-3161 anchor against freeTSA.org. Implements ExternalAnchorPort.

    Persists DER-encoded TimeStampTokens (.tsr files) keyed by head_hash_hex
    under token_dir. Maintains a JSON pending queue for heads not yet anchored
    due to network failures.

    Args:
        tsa_url:      TSA endpoint. Default = freeTSA.org.
        token_dir:    Directory for persisting .tsr token files and the
                      pending queue. Default = /var/lib/hermes/tsa_tokens.
        ca_cert_pem:  PEM bytes of the TSA CA cert for signature verification.
                      Default = bundled ops/audit/freetsa_tsa.crt.
        transport:    Injectable TsaTransport (default = httpx async client).
        clock:        Callable[[], str] returning UTC ISO timestamp (injectable).
    """

    def __init__(
        self,
        *,
        tsa_url: str = _FREETSA_URL,
        token_dir: Path | None = None,
        ca_cert_pem: bytes | None = None,
        transport: TsaTransport | None = None,
        clock: object = None,
    ) -> None:
        self._tsa_url = tsa_url
        requested_dir = token_dir or Path("/var/lib/hermes/tsa_tokens")
        # Degrade like the rest of the entrypoint: never crash construction on a
        # non-writable token dir. Fall back to a tempdir so the daemon still boots
        # (anchoring then fails-open and is logged) instead of taking down the loop.
        try:
            requested_dir.mkdir(parents=True, exist_ok=True)
            self._token_dir = requested_dir
        except OSError as exc:  # PermissionError, read-only fs, etc.
            fallback = Path(tempfile.gettempdir()) / "hermes_tsa_tokens"
            fallback.mkdir(parents=True, exist_ok=True)
            self._token_dir = fallback
            logger.error(
                "hermes.tsa.token_dir_unwritable: %s not writable (%s), "
                "falling back to %s",
                requested_dir,
                exc,
                fallback,
            )
        self._ca_cert_pem = ca_cert_pem or self._load_bundled_cert()
        self._transport: TsaTransport = transport or _HttpxTsaTransport()
        self._clock = clock if clock is not None else _utc_now_iso
        self._pending_queue_path = self._token_dir / "pending_queue.json"
        self._latest_path = self._token_dir / "latest.txt"
        # Observable degraded flag: True when pending queue >= _PENDING_ALARM_THRESHOLD.
        # Set by _flush_queue(); readable by monitoring surfaces.
        self.tsa_degraded: bool = False

    # ------------------------------------------------------------------
    # ExternalAnchorPort
    # ------------------------------------------------------------------

    async def anchor(self, head_hash_hex: str) -> str:
        """Anchor head_hash_hex via RFC-3161 TSA. Best-effort; fail-open.

        Returns:
            "tsa:<hex_prefix>" if anchored, or "pending:<hash>" if queued.
        """
        self._enqueue(head_hash_hex)
        await self._flush_queue()
        if self._tst_path(head_hash_hex).exists():
            return self._tst_ref(head_hash_hex)
        return f"{_PENDING_REF_PREFIX}{head_hash_hex}"

    async def get_latest(self) -> str | None:
        """Return the last successfully anchored head_hash_hex, or None."""
        if not self._latest_path.exists():
            return None
        try:
            value = self._latest_path.read_text().strip()
            return value or None
        except OSError:
            return None

    async def verify(self, local_head: str) -> bool:
        """Verify local_head has a valid TST anchored by the TSA.

        Returns False if no TST exists, if the message imprint mismatches,
        or if the TSA signature is invalid. Fail-closed on any error.
        """
        tst_path = self._tst_path(local_head)
        if not tst_path.exists():
            return False
        try:
            tst_bytes = tst_path.read_bytes()
            return _verify_tst(
                tst_bytes=tst_bytes,
                expected_digest_hex=local_head,
                ca_cert_pem=self._ca_cert_pem,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("hermes.tsa.verify_error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private — pending queue management
    # ------------------------------------------------------------------

    def _enqueue(self, head_hash_hex: str) -> None:
        """Add head_hash_hex to pending queue (idempotent)."""
        queue = self._load_queue()
        if head_hash_hex not in queue:
            queue.append(head_hash_hex)
            self._save_queue(queue)

    def _dequeue(self, head_hash_hex: str) -> None:
        queue = self._load_queue()
        if head_hash_hex in queue:
            queue.remove(head_hash_hex)
            self._save_queue(queue)

    def _load_queue(self) -> list[str]:
        if not self._pending_queue_path.exists():
            return []
        try:
            data = json.loads(self._pending_queue_path.read_text())
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_queue(self, queue: list[str]) -> None:
        self._pending_queue_path.write_text(json.dumps(queue))

    # ------------------------------------------------------------------
    # Private — network flush
    # ------------------------------------------------------------------

    # Entries pending TSA anchor beyond this threshold trigger a DEGRADED alert.
    _PENDING_ALARM_THRESHOLD: int = 50

    async def _flush_queue(self) -> None:
        """Attempt to anchor all pending heads. Stops on first network error.

        Fix (AUD-2 / Fix-3): after flushing, check the queue length against
        _PENDING_ALARM_THRESHOLD and emit ERROR + set _tsa_degraded flag so
        the condition is observable (not silently pending).  Non-blocking:
        the audit append is not blocked — only visibility is added.
        """
        queue = self._load_queue()
        for head in list(queue):
            if self._tst_path(head).exists():
                self._dequeue(head)
                continue
            success = await self._do_anchor(head)
            if not success:
                logger.error(
                    "hermes.tsa.anchor_queued: head=%s — "
                    "non-repudiation window open until TSA is reachable",
                    head[:16],
                )
                break

        # Alarm when pending count exceeds threshold (Fix-3).
        remaining = self._load_queue()
        pending_count = len(remaining)
        self.tsa_degraded: bool = pending_count >= self._PENDING_ALARM_THRESHOLD
        if self.tsa_degraded:
            logger.error(
                "hermes.tsa.degraded: %d entries pending TSA anchor "
                "(threshold=%d). Non-repudiation window is OPEN. "
                "Check TSA connectivity (HERMES_TSA_URL / freeTSA.org).",
                pending_count,
                self._PENDING_ALARM_THRESHOLD,
            )

    async def _do_anchor(self, head_hash_hex: str) -> bool:
        """POST to TSA and persist token. Returns True on success."""
        try:
            req_bytes = _build_timestamp_request(head_hash_hex)
            raw_resp = await self._transport.post_timestamp_query(
                url=self._tsa_url,
                body=req_bytes,
                timeout_s=_HTTP_TIMEOUT_S,
            )
            tsr = _decode_timestamp_response_from_bytes(raw_resp)
            tst_bytes = _extract_tst_bytes(tsr)
            self._persist_tst(head_hash_hex, tst_bytes)
            self._dequeue(head_hash_hex)
            self._update_latest(head_hash_hex)
            logger.info(
                "hermes.tsa.anchored: head=%s ref=%s",
                head_hash_hex[:16],
                self._tst_ref(head_hash_hex),
            )
            return True
        except TsaNetworkError as exc:
            logger.error("hermes.tsa.network_error: %s", exc)
            return False
        except TsaProtocolError as exc:
            logger.error("hermes.tsa.protocol_error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private — file I/O
    # ------------------------------------------------------------------

    def _tst_path(self, head_hash_hex: str) -> Path:
        return self._token_dir / f"{head_hash_hex}.tsr"

    def _persist_tst(self, head_hash_hex: str, tst_bytes: bytes) -> None:
        path = self._tst_path(head_hash_hex)
        path.write_bytes(tst_bytes)
        path.chmod(0o600)

    def _update_latest(self, head_hash_hex: str) -> None:
        self._latest_path.write_text(head_hash_hex)

    def _tst_ref(self, head_hash_hex: str) -> str:
        return f"tsa:{head_hash_hex[:32]}"

    @staticmethod
    def _load_bundled_cert() -> bytes:
        """Carga el cert CA de freeTSA, robusto en dev (repo) e instalado (/usr/lib).

        Orden de búsqueda (el bug: el path por __file__ ×5 .parent solo acierta en
        el layout del repo `src/hermes/...`; instalado en site-packages NO hay nivel
        `src/`, así que apuntaba a /usr/lib/python3.13/ops/audit y crasheaba el daemon):
          1. HERMES_TSA_CACERT (override explícito).
          2. package-data junto al módulo (importlib.resources) — funciona instalado.
          3. el path del repo (_BUNDLED_CERT_PATH) — funciona en dev/editable.
          4. ubicaciones de imagen conocidas (/usr/share/hermes, /etc/hermes).
        """
        import os  # noqa: PLC0415
        from importlib import resources  # noqa: PLC0415

        override = os.environ.get("HERMES_TSA_CACERT")
        if override and Path(override).exists():
            return Path(override).read_bytes()

        # package-data: el cert viaja DENTRO del paquete (ver pyproject package-data).
        try:
            res = resources.files("hermes.capabilities.infrastructure") / "freetsa_tsa.crt"
            if res.is_file():
                return res.read_bytes()
        except (ModuleNotFoundError, FileNotFoundError, OSError):
            pass

        candidates = [
            _BUNDLED_CERT_PATH,
            Path("/usr/share/hermes/freetsa_tsa.crt"),
            Path("/etc/hermes/freetsa_tsa.crt"),
        ]
        for cand in candidates:
            if cand.exists():
                return cand.read_bytes()

        raise FileNotFoundError(
            "freeTSA CA cert no encontrado (HERMES_TSA_CACERT, package-data, "
            f"{_BUNDLED_CERT_PATH}, /usr/share/hermes, /etc/hermes). "
            "Bakear el cert como package-data o en /usr/share/hermes/freetsa_tsa.crt."
        )


assert isinstance(
    TsaExternalAnchor(
        token_dir=Path(tempfile.gettempdir()) / "__hermes_tsa_typecheck",
        ca_cert_pem=b"placeholder_not_verified_at_import",
    ),
    ExternalAnchorPort,
)


# ---------------------------------------------------------------------------
# CompositeExternalAnchor
# ---------------------------------------------------------------------------


class CompositeExternalAnchor:
    """Composite anchor: WormFile (local) + TSA (external non-repudiation).

    Rationale:
    - WormFileAnchor detects local truncation/reset fast (no network needed).
    - TsaExternalAnchor provides cryptographic non-repudiation against root.
    - anchor() calls both layers; verify() requires both to agree (True).

    If either layer fails to anchor, the failure is logged but does NOT block
    the audit append (fail-open per CTRL-8). verify() returns True only when
    both layers confirm — conservative by design.

    Args:
        worm:  WormFileAnchor instance.
        tsa:   TsaExternalAnchor instance.
    """

    def __init__(self, *, worm: WormFileAnchor, tsa: TsaExternalAnchor) -> None:
        self._worm = worm
        self._tsa = tsa

    async def anchor(self, head_hash_hex: str) -> str:
        worm_ref = await self._worm.anchor(head_hash_hex)
        tsa_ref = await self._tsa.anchor(head_hash_hex)
        return f"worm:{worm_ref};{tsa_ref}"

    async def get_latest(self) -> str | None:
        """Return the latest hash anchored by the TSA (strongest guarantee)."""
        return await self._tsa.get_latest()

    async def verify(self, local_head: str) -> bool:
        """True only if both worm and TSA confirm local_head."""
        worm_ok = await self._worm.verify(local_head)
        tsa_ok = await self._tsa.verify(local_head)
        if not worm_ok:
            logger.warning(
                "hermes.composite_anchor.worm_mismatch: head=%s", local_head[:16]
            )
        if not tsa_ok:
            logger.warning(
                "hermes.composite_anchor.tsa_mismatch: head=%s", local_head[:16]
            )
        return worm_ok and tsa_ok


assert isinstance(
    CompositeExternalAnchor(
        worm=WormFileAnchor(
            anchor_path=Path(tempfile.gettempdir()) / "__hermes_composite_worm_typecheck.tmp"
        ),
        tsa=TsaExternalAnchor(
            token_dir=Path(tempfile.gettempdir()) / "__hermes_composite_tsa_typecheck",
            ca_cert_pem=b"placeholder_not_verified_at_import",
        ),
    ),
    ExternalAnchorPort,
)
