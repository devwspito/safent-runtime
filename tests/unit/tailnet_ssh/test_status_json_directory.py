"""StatusJsonTailnetDirectory — fail-closed reader of the ops lane's
`/run/hermes/tailscale/status.json`."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.tailnet_ssh.application.ports import TailnetPeer
from hermes.tailnet_ssh.domain.errors import TailnetDirectoryUnavailableError
from hermes.tailnet_ssh.infrastructure.status_json_directory import StatusJsonTailnetDirectory

pytestmark = pytest.mark.unit


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


class TestStatusJsonTailnetDirectory:
    def test_reads_well_formed_status(self, tmp_path: Path) -> None:
        status_path = tmp_path / "status.json"
        _write(status_path, (
            '{"node_name": "safent-agent", "magicdns_suffix": "tailxxxx.ts.net", '
            '"online": true, "peers": [{"name": "db1", "online": true}]}'
        ))
        directory = StatusJsonTailnetDirectory(status_path)

        status = directory.read()

        assert status.node_name == "safent-agent"
        assert status.magicdns_suffix == "tailxxxx.ts.net"
        assert status.online is True
        assert status.peers == (TailnetPeer(name="db1", online=True),)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        directory = StatusJsonTailnetDirectory(tmp_path / "missing.json")
        with pytest.raises(TailnetDirectoryUnavailableError):
            directory.read()

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        status_path = tmp_path / "status.json"
        _write(status_path, "{not json")
        directory = StatusJsonTailnetDirectory(status_path)
        with pytest.raises(TailnetDirectoryUnavailableError):
            directory.read()

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        status_path = tmp_path / "status.json"
        _write(status_path, '{"node_name": "x"}')
        directory = StatusJsonTailnetDirectory(status_path)
        with pytest.raises(TailnetDirectoryUnavailableError):
            directory.read()

    def test_non_object_json_raises(self, tmp_path: Path) -> None:
        status_path = tmp_path / "status.json"
        _write(status_path, "[]")
        directory = StatusJsonTailnetDirectory(status_path)
        with pytest.raises(TailnetDirectoryUnavailableError):
            directory.read()

    def test_peers_not_a_list_raises(self, tmp_path: Path) -> None:
        status_path = tmp_path / "status.json"
        _write(status_path, (
            '{"node_name": "x", "magicdns_suffix": "s", "online": true, "peers": {}}'
        ))
        directory = StatusJsonTailnetDirectory(status_path)
        with pytest.raises(TailnetDirectoryUnavailableError):
            directory.read()

    def test_empty_peers_defaults_to_empty_tuple(self, tmp_path: Path) -> None:
        status_path = tmp_path / "status.json"
        _write(status_path, (
            '{"node_name": "x", "magicdns_suffix": "s", "online": false}'
        ))
        directory = StatusJsonTailnetDirectory(status_path)
        assert directory.read().peers == ()
