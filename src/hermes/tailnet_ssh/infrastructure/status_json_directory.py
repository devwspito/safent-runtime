"""StatusJsonTailnetDirectory — reads the ops lane's
`/run/hermes/tailscale/status.json` (contract: `{node_name, magicdns_suffix,
online, peers:[{name, online}]}`). This module OWNS reading it; it never
writes it (that file belongs to the ops lane's `tailscaled` wiring).

Fail-closed: any missing/unreadable/malformed status raises
`TailnetDirectoryUnavailableError` — never a stale or partially-parsed guess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes.tailnet_ssh.application.ports import TailnetPeer, TailnetStatus
from hermes.tailnet_ssh.domain.errors import TailnetDirectoryUnavailableError

DEFAULT_STATUS_PATH = Path("/run/hermes/tailscale/status.json")


class StatusJsonTailnetDirectory:
    def __init__(self, status_path: Path = DEFAULT_STATUS_PATH) -> None:
        self._status_path = status_path

    def read(self) -> TailnetStatus:
        try:
            raw = json.loads(self._status_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TailnetDirectoryUnavailableError(
                f"status.json no existe: {self._status_path}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TailnetDirectoryUnavailableError(
                f"status.json ilegible/corrupto: {self._status_path}"
            ) from exc
        return _parse_status(raw)


def _parse_status(raw: Any) -> TailnetStatus:
    if not isinstance(raw, dict):
        raise TailnetDirectoryUnavailableError("status.json no es un objeto JSON")
    try:
        node_name = str(raw["node_name"])
        magicdns_suffix = str(raw["magicdns_suffix"])
        online = bool(raw["online"])
        peers_raw = raw.get("peers", [])
    except KeyError as exc:
        raise TailnetDirectoryUnavailableError(
            f"status.json le falta el campo {exc}"
        ) from exc
    if not isinstance(peers_raw, list):
        raise TailnetDirectoryUnavailableError("status.json: «peers» no es una lista")

    peers = tuple(_parse_peer(p) for p in peers_raw if isinstance(p, dict))
    return TailnetStatus(
        node_name=node_name, magicdns_suffix=magicdns_suffix, online=online, peers=peers
    )


def _parse_peer(raw: dict[str, Any]) -> TailnetPeer:
    return TailnetPeer(name=str(raw.get("name", "")), online=bool(raw.get("online", False)))
