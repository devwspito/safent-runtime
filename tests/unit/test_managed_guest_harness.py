"""Host-runner safety, not a substitute for executed guest proof."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "integration/managed_guest"
SPEC = importlib.util.spec_from_file_location("managed_guest_runner", HARNESS / "run_guest.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_only_private_explicit_scratch():
    with tempfile.TemporaryDirectory(prefix="safent-managed-guest.", dir="/tmp") as directory:
        path = Path(directory)
        assert runner.validate_scratch(path) == path.resolve()
        path.chmod(0o755)
        with pytest.raises(ValueError, match="private"):
            runner.validate_scratch(path)
        path.chmod(0o700)


def test_reject_general_temp_directory():
    with (
        tempfile.TemporaryDirectory(prefix="unrelated-", dir="/tmp") as directory,
        pytest.raises(ValueError, match="dedicated"),
    ):
        runner.validate_scratch(Path(directory))


def test_reject_symlink_even_to_owned_scratch(tmp_path):
    with tempfile.TemporaryDirectory(prefix="safent-managed-guest.", dir="/tmp") as directory:
        link = tmp_path / "scratch-link"
        link.symlink_to(directory, target_is_directory=True)
        with pytest.raises(ValueError, match="symlink"):
            runner.validate_scratch(link)


def test_disk_serial_fits_virtio_limit_and_matches_guest_guard():
    # A too-long serial is truncated by virtio, and preparation must then fail
    # before formatting any device. The first real run caught that harness bug.
    import ast

    source = (HARNESS / "run_guest.py").read_text()
    constants = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    device = next(
        value for value in constants if value.startswith("virtio-blk-pci,drive=runtime,serial=")
    )
    serial = device.split("serial=", 1)[1]
    assert len(serial.encode()) <= 20
    assert f"disk=/dev/disk/by-id/virtio-{serial}" in (HARNESS / "prepare_guest.sh").read_text()


def test_runner_has_no_nic_host_shares_or_unbounded_process():
    import ast

    source = (HARNESS / "run_guest.py").read_text()
    tree = ast.parse(source)
    constants = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "-nic" in constants and "none" in constants
    assert "virt,accel=kvm" in constants
    for forbidden in ("-virtfs", "-fsdev", "-netdev", "-net", "-vhost", "-object"):
        assert forbidden not in constants
    assert "process.wait(timeout=600)" in source
    assert 'disk.open("xb")' in source


@pytest.mark.parametrize(
    "payload",
    [
        "",
        'SAFENT_GUEST_REPORT {"ready": false}',
        'SAFENT_GUEST_REPORT {"ready": true, "managed_checks_error": "failed"}',
        'SAFENT_GUEST_REPORT {"ready": true}\nSAFENT_GUEST_REPORT {"ready": true}',
    ],
)
def test_failed_guest_is_not_successful_runner_exit(payload):
    with pytest.raises(RuntimeError):
        runner.validate_runtime_report(payload)


def test_successful_report_accepts_only_observed_ready():
    assert runner.validate_runtime_report('SAFENT_GUEST_REPORT {"ready": true}') == {"ready": True}
