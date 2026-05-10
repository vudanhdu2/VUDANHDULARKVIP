"""Tests cho CLI — chỉ verify command structure + --help, không test logic."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from waytoagi.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.mark.unit
class TestCLIVersion:
    def test_version(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "2.0.0" in result.output

    def test_help_lists_commands(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in (
            "preflight", "schema-migrate", "crawl", "pipeline",
            "mirror", "sync", "reorder", "orchestrate", "status",
            "audit", "resume",
        ):
            assert cmd in result.output


@pytest.mark.unit
class TestCLICommandHelp:
    @pytest.mark.parametrize("cmd", [
        "preflight", "schema-migrate", "crawl", "pipeline",
        "mirror", "sync", "reorder", "orchestrate",
        "status", "audit", "resume",
    ])
    def test_each_command_help(self, runner: CliRunner, cmd: str) -> None:
        result = runner.invoke(cli, [cmd, "--help"])
        assert result.exit_code == 0
        # Every command should have common options
        assert "--dry-run" in result.output
        assert "--workers" in result.output
        assert "--verbose" in result.output


@pytest.mark.unit
class TestCLIDryRun:
    @pytest.mark.parametrize("cmd", [
        "preflight", "crawl", "pipeline", "mirror", "sync",
    ])
    def test_dry_run_runs_clean(self, runner: CliRunner, cmd: str) -> None:
        """--dry-run không touching Lark — chỉ in placeholder messages."""
        result = runner.invoke(cli, [cmd, "--dry-run"])
        # Should exit 0 (placeholder text only)
        assert result.exit_code == 0


@pytest.mark.unit
class TestAuditCommand:
    def test_audit_kind_choices(self, runner: CliRunner) -> None:
        # Kind invalid → exit non-zero
        result = runner.invoke(cli, ["audit", "invalid"])
        assert result.exit_code != 0

    @pytest.mark.parametrize("kind", ["ui", "translation", "tree", "all"])
    def test_audit_valid_kind(self, runner: CliRunner, kind: str) -> None:
        result = runner.invoke(cli, ["audit", kind])
        assert result.exit_code == 0
