"""Core netns forwarding is a creation property, never a host-wide sysctl."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("path", ["safent", "ops/container/run-safent.sh"])
def test_hardened_launch_sets_only_core_namespace_forwarding(path: str) -> None:
    source = (_ROOT / path).read_text()
    flag = "--sysctl net.ipv4.ip_forward=1"
    assert source.count(flag) == 1
    launch = source[source.index('run -d --name "$NAME" --systemd=always'):]
    launch = launch.split('\n\n', 1)[0]
    assert flag in launch
    assert "--privileged" not in launch
    assert "--network host" not in launch
    assert "--network=host" not in launch
    assert "/proc/sys:" not in launch
    assert "--cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ" in launch
    assert "sysctl -w net.ipv4.ip_forward" not in source
