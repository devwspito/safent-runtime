"""BrowserLauncherClient — daemon-side async AF_UNIX client to hermes-browser-launcher.

Runs inside hermes-runtime.service (unprivileged hermes daemon) and communicates
with hermes-browser-launcher.service (root helper) over the AF_UNIX socket at
/run/hermes/browser-launch.sock.

The root launcher owns the socket (root:hermes, 0660). The daemon (User=hermes,
group hermes) can connect because it is in the hermes group. The server validates
via SO_PEERCRED (gid check); the daemon need not present any additional token.

Wire protocol: 4-byte big-endian length-prefix + UTF-8 JSON.
  Request:  {"session_name": "<exec-...>", "domains_whitelist": ["a.com", ...]}
  Response: {"ok": true, "session_name": "<name>"} | {"ok": false, "error": "<reason>"}

This client sends ONE request, reads ONE response, closes. Stateless and crash-safe.

The launcher validates session_name format (^exec-[a-z0-9]+$) and rejects
unknown fields — the caller cannot influence the systemd-run property template.

Capa: infrastructure (security / privilege bridge).
"""

from __future__ import annotations

import asyncio
import json
import struct
import logging
from pathlib import Path

logger = logging.getLogger("hermes.security.browser_launcher_client")

_SOCKET_PATH = Path("/run/hermes/browser-launch.sock")
_CONNECT_TIMEOUT_S: float = 5.0
_IO_TIMEOUT_S: float = 30.0
_MAX_FRAME_BYTES: int = 8 * 1024


class BrowserLauncherUnavailable(RuntimeError):
    """The browser launcher root helper is not reachable (unit not started or socket missing)."""


class BrowserLauncherError(RuntimeError):
    """The launcher returned ok=False."""


class BrowserLauncherClient:
    """Async client for hermes-browser-launcher root helper.

    Usage:
        client = BrowserLauncherClient()
        await client.launch(session_name="exec-abc123", domains_whitelist=("example.com",))

    Raises BrowserLauncherUnavailable when the socket is absent.
    Raises BrowserLauncherError when the launcher returns ok=False.
    """

    def __init__(self, *, socket_path: Path = _SOCKET_PATH) -> None:
        self._socket_path = socket_path

    async def launch(
        self,
        *,
        session_name: str,
        domains_whitelist: tuple[str, ...] = (),
    ) -> None:
        """Request the root launcher to spawn the browser scope.

        Only session_name and domains_whitelist travel over the wire. ALL
        systemd-run properties AND the full Chromium argv are built server-side
        in the launcher (the privilege boundary) — this client cannot widen the
        scope nor influence the browser's confinement flags (security HIGH-1).

        Args:
            session_name: must match ^exec-[a-z0-9]+$ (validated server-side too).
            domains_whitelist: informational, passed to the proxy policy separately.

        Raises:
            BrowserLauncherUnavailable: socket absent or connection failed.
            BrowserLauncherError: launcher returned ok=False.
        """
        request: dict = {
            "session_name": session_name,
            "domains_whitelist": list(domains_whitelist),
        }
        response = await self._roundtrip(request)
        if not response.get("ok", False):
            error = response.get("error", "unknown error from launcher")
            raise BrowserLauncherError(
                f"hermes-browser-launcher returned error: {error}"
            )
        logger.info(
            "browser_launcher_client.launched session=%s", session_name
        )

    async def _roundtrip(self, request: dict) -> dict:
        if not self._socket_path.exists():
            raise BrowserLauncherUnavailable(
                f"Browser launcher socket not found: {self._socket_path}"
            )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)),
                timeout=_CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise BrowserLauncherUnavailable(
                f"Cannot connect to browser launcher at {self._socket_path}: {exc}"
            ) from exc

        try:
            await asyncio.wait_for(_send_frame(writer, request), timeout=_IO_TIMEOUT_S)
            response = await asyncio.wait_for(_read_frame(reader), timeout=_IO_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise BrowserLauncherUnavailable("Browser launcher I/O timeout") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

        if response is None:
            raise BrowserLauncherUnavailable(
                "Browser launcher closed connection without response"
            )
        return response


# ── Framing helpers ────────────────────────────────────────────────────────────


async def _send_frame(writer: asyncio.StreamWriter, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> dict | None:
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    length = struct.unpack(">I", header)[0]
    if length > _MAX_FRAME_BYTES:
        raise BrowserLauncherUnavailable(f"Response frame too large: {length}")
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))
