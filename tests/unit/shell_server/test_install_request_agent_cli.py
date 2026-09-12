"""install_request_agent_cli (T016) — the host agent's own thin CLI reader
of the install-request marker, invoked via `podman exec $NAME python3 -m
hermes.shell_server.install_request_agent_cli ...` by the `safent` host
CLI. Covers only the CLI's OWN contract (argument parsing, exit codes,
stdout shape) — `claim_request`/`resolve_request` themselves are covered in
tests/unit/agents_os/test_install_requests.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.shell_server import install_request_agent_cli as cli
from hermes.shell_server import install_requests as ir

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    instance_dir = tmp_path / "instance"
    monkeypatch.setattr(ir, "_INSTANCE_DIR", instance_dir)
    return instance_dir


class TestCmdClaim:
    def test_unknown_verb_exits_2_with_no_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.cmd_claim("bogus_verb", "agent-1")
        assert rc == 2
        assert capsys.readouterr().out == ""

    def test_nothing_live_exits_1_with_no_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["claim-ads", "--claimant", "agent-1"])
        assert rc == 1
        assert capsys.readouterr().out == ""

    def test_claimed_request_exits_0_and_prints_the_slug(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ir.create_request("install_companion", slug="safent-ads")

        rc = cli.main(["claim-ads", "--claimant", "agent-1"])

        assert rc == 0
        output = capsys.readouterr().out.split()
        assert output[0] == "install_companion" and len(output[1]) == 32

    def test_verb_with_no_slug_prints_an_empty_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ir.create_request("update_system")

        rc = cli.cmd_claim("update_system", "agent-1")

        assert rc == 0
        assert capsys.readouterr().out == "\n"


class TestCmdResolve:
    def test_unknown_verb_exits_2(self) -> None:
        assert cli.cmd_resolve("bogus_verb", success=True) == 2

    def test_success_consumes_the_marker(self) -> None:
        ir.create_request("install_companion", slug="safent-ads")
        claim = ir.claim_request("install_companion", claimant="agent-1")
        assert claim is not None
        rc = cli.main(
            [
                "resolve-ads",
                "install_companion",
                "--claimant",
                "agent-1",
                "--request-id",
                claim.request_id,
                "--success",
            ]
        )

        assert rc == 0
        assert not ir._marker_path("install_companion").exists()

    def test_failure_keeps_the_marker_for_a_retry(self) -> None:
        ir.create_request("install_companion", slug="safent-ads")
        claim = ir.claim_request("install_companion", claimant="agent-1")
        assert claim is not None
        rc = cli.main(
            [
                "resolve-ads",
                "install_companion",
                "--claimant",
                "agent-1",
                "--request-id",
                claim.request_id,
                "--failure",
            ]
        )

        assert rc == 0
        assert ir._marker_path("install_companion").exists()


class TestMainDispatch:
    def test_claim_subcommand_requires_claimant(self) -> None:
        with pytest.raises(SystemExit):
            cli.main(["claim", "install_companion"])

    def test_resolve_subcommand_requires_success_or_failure(self) -> None:
        with pytest.raises(SystemExit):
            cli.main(["resolve", "install_companion"])

    def test_resolve_rejects_both_success_and_failure_together(self) -> None:
        with pytest.raises(SystemExit):
            cli.main(["resolve", "install_companion", "--success", "--failure"])

    def test_claim_dispatches_to_cmd_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cli, "cmd_claim", lambda verb, claimant: calls.append((verb, claimant)) or 0
        )

        rc = cli.main(["claim", "install_companion", "--claimant", "agent-1"])

        assert rc == 0
        assert calls == [("install_companion", "agent-1")]

    def test_resolve_dispatches_to_cmd_resolve_with_success_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            cli, "cmd_resolve", lambda verb, *, success: calls.append((verb, success)) or 0
        )

        rc = cli.main(["resolve", "install_companion", "--success"])

        assert rc == 0
        assert calls == [("install_companion", True)]
