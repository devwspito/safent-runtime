"""The real CLI update must not prune host-global image/build resources."""

import pytest

from tests.unit.ops.test_safent_cli_backup_restore import _run_safent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("pull_fails", [False, True])
def test_update_never_prunes_shared_engine_resources(tmp_path, pull_fails):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    engine = binaries / "podman"
    engine.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_PODMAN_LOG"
case "$1" in
  machine)
    case "$2" in list) echo safent-test-engine ;; inspect) echo true ;; esac ;;
  inspect)
    case "$*" in *'{{.Image}}'*) printf 'sha256:%064d\\n' 1 ;; *) echo true ;; esac ;;
  images)
    printf 'sha256:%064d ghcr.io/devwspito/safent\\n' 1
    printf 'sha256:%064d ghcr.io/devwspito/safent\\n' 2 ;;
  pull) [ "$FAKE_PULL_FAILS" != true ] || exit 125 ;;
  run) case "$*" in *--entrypoint*) echo '{}' ;; esac ;;
  exec) case "$*" in *is-active*) echo active ;; *) echo synthetic-bootstrap ;; esac ;;
esac
exit 0
''')
    engine.chmod(0o755)
    # Neither macOS launchctl nor Linux systemctl may reach a real user service.
    # curl is blocked even though self-update is disabled, so this cannot fetch
    # code or a seccomp file accidentally. All state is in the test home.
    for name in ("launchctl", "systemctl", "curl"):
        executable = binaries / name
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o755)
    log = tmp_path / "engine.log"
    result = _run_safent(
        "update", "--no-companion", fake_bin_dir=binaries,
        home_dir=tmp_path / "home", podman_log=log,
        extra_env={
            "SAFENT_NO_SELF_UPDATE": "1", "SAFENT_NO_BROWSER": "1",
            "FAKE_PULL_FAILS": "true" if pull_fails else "false",
        },
    )
    calls = log.read_text().splitlines()
    assert not any(c.startswith(("image prune", "builder prune", "volume prune", "rmi "))
                   for c in calls), calls
    assert any(c.startswith("pull ") for c in calls)
    if pull_fails:
        assert result.returncode != 0
        assert not any(c.startswith(("rm ", "run ")) for c in calls)
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(c.startswith("run -d ") for c in calls)
