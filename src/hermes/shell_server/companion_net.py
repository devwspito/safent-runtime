"""companion_net — shared TLS-pinned HTTP plumbing for reaching a companion
(024/026) by its validated `CompanionEndpoint`, reused by BOTH the daemon's
health probe (`agents_os.infrastructure.companion_health_check`, T004) and
the shell-server's session-bridge proxy (`shell_server.ads_bridge`, T005) —
one implementation, not two copies of the same DNS-pinning trick.

`ads.safent.internal` is NOT resolvable by the container's normal DNS (only
ever reached via the fixed companion IP, 10.201.0.0/24). `FixedIpResolver`
is the aiohttp equivalent of curl's `--resolve`: it pins the TCP destination
to the validated IP while the request URL keeps the real hostname, so TLS
SNI/hostname verification still checks the certificate's actual SAN
(`DNS:ads.safent.internal`) against `ca_path`.
"""

from __future__ import annotations

import socket

import aiohttp


class FixedIpResolver(aiohttp.abc.AbstractResolver):
    """Resolve *hostname* to a single pinned IP, everything else untouched."""

    def __init__(self, *, hostname: str, ip: str) -> None:
        self._hostname = hostname
        self._ip = ip

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,  # noqa: ARG002 — AbstractResolver signature
    ) -> list[aiohttp.abc.ResolveResult]:
        target_ip = self._ip if host == self._hostname else host
        return [
            {
                "hostname": host,
                "host": target_ip,
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self) -> None:
        return None
