"""Servidor del socket de control root ``/run/hermes/egress-proxy.sock``.

El socket escucha comandos JSON de un proceso root (el runtime del agente
al empujar política por-tarea).  El proceso del navegador (no privilegiado,
en netns hermes-browser) NO puede llegar a este socket:

  - El socket reside en el filesystem del host (``/run/hermes/``).
  - El netns hermes-browser no tiene acceso al filesystem de /run/hermes
    (montaje de solo lectura en el jail + ruta no mapeada).
  - Incluso si pudiera llegar, el socket tiene permisos 0600 y pertenece
    al usuario que arranca el proxy (``hermes-egress``), no al navegador.

Protocolo: cada comando es una línea JSON terminada en ``\\n``.  El servidor
responde ``OK\\n`` o ``ERR <mensaje>\\n``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path

from hermes.egress_proxy.application.control_command import (
    ControlCommandError,
    parse_control_command,
)
from hermes.egress_proxy.domain.policy import EgressPolicyEngine

logger = logging.getLogger("hermes.egress_proxy.control")

_MAX_COMMAND_BYTES = 65536
_COMMAND_TIMEOUT_S = 5.0


class ControlSocketServer:
    """Servidor asyncio del socket de control.

    Args:
        socket_path: ruta del UNIX domain socket (``/run/hermes/egress-proxy.sock``).
        policy_engine: motor de política compartido con el servidor proxy.
    """

    def __init__(
        self,
        *,
        socket_path: str | Path,
        policy_engine: EgressPolicyEngine,
    ) -> None:
        self._path = Path(socket_path)
        self._policy = policy_engine
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Arranca el servidor y asegura permisos 0600 en el socket."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Limpia socket huérfano de una ejecución anterior
        if self._path.exists():
            self._path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self._path),
        )
        # 0660 + group hermes-egress: ONLY the policy-setters (the daemon's browser
        # flow + the shell-server elevation API, both added to SupplementaryGroups=
        # hermes-egress) may CONNECT to push policy. NOT group `hermes` — the agent's
        # sandboxed terminal/browser run as plain `hermes` and would otherwise push
        # `open-logged` to DISABLE their own egress jail (self-widening; red-team
        # finding 2026-06-18). The agent's sandbox also has /run/hermes InaccessiblePaths
        # as belt-and-suspenders. 0600 (owner-only) would lock the setters out.
        try:
            import grp  # noqa: PLC0415

            os.chown(self._path, -1, grp.getgrnam("hermes-egress").gr_gid)
        except (KeyError, PermissionError, OSError) as exc:
            logger.warning("hermes.egress_proxy.control_socket_chown_failed: %s", exc)
        os.chmod(
            self._path,
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP,  # 0o660
        )
        logger.info(
            "hermes.egress_proxy.control_socket_ready",
            extra={"path": str(self._path)},
        )

    async def serve_forever(self) -> None:
        """Sirve hasta cancelación."""
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.read(_MAX_COMMAND_BYTES), timeout=_COMMAND_TIMEOUT_S
            )
            if not raw:
                return
            policy = parse_control_command(raw)
            # Un netns hermes-browser = un navegador = UNA política de egress. El
            # proxy evalúa por IP de cliente del netns, no por session_id, así que
            # la política empujada se aplica como GLOBAL (gobierna todas las
            # conexiones del navegador). session_id queda como etiqueta de auditoría.
            self._policy.replace_global(policy)
            logger.info(
                "hermes.egress_proxy.policy_updated",
                extra={
                    "session_id": policy.session_id,
                    "mode": policy.mode,
                    "domain_count": len(policy.domains_whitelist),
                },
            )
            writer.write(b"OK\n")
        except ControlCommandError as exc:
            logger.warning(
                "hermes.egress_proxy.control_command_error",
                extra={"error": str(exc)},
            )
            writer.write(f"ERR {exc}\n".encode())
        except asyncio.TimeoutError:
            logger.warning("hermes.egress_proxy.control_command_timeout")
            writer.write(b"ERR timeout\n")
        except Exception as exc:  # noqa: BLE001
            # Belt-and-suspenders (red-team fuzz 2026-06-19): a parser/handler bug must
            # NOT kill the control connection task and leave the proxy's control plane
            # wedged. Any unexpected error → log + ERR, the server keeps serving.
            logger.error("hermes.egress_proxy.control_handler_error: %r", exc)
            try:
                writer.write(b"ERR internal\n")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            writer.close()
