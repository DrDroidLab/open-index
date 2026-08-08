"""The `mcp-config` and `serve` commands — how a user gets connected.

`serve` is stubbed at the uvicorn boundary so the banner (the part users read to
find their URL) is asserted without binding a port.
"""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_index.cli import app

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"
runner = CliRunner()


@pytest.fixture
def brain_dir(tmp_path):
    dst = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, dst)
    return dst


@pytest.fixture
def served(monkeypatch):
    """Capture what `serve` would have handed to the HTTP server."""
    captured = {}
    monkeypatch.setattr(
        "open_index.mcp_server.serve_http",
        lambda d, **kw: captured.update(brain_dir=d, **kw),
    )
    return captured


def run(*args, **kwargs):
    return runner.invoke(app, [str(a) for a in args], **kwargs)


# -- mcp-config ---------------------------------------------------------------


def test_mcp_config_local_emits_valid_json(brain_dir):
    result = run("mcp-config", "--brain", brain_dir)
    assert result.exit_code == 0
    entry = json.loads(result.stdout)["mcpServers"]["open-index"]
    assert entry["command"] == "open-index"
    assert Path(entry["args"][-1]).is_absolute()


def test_mcp_config_remote_includes_token(brain_dir):
    result = run("mcp-config", "--url", "host:8080", "--token", "s3cret")
    entry = json.loads(result.stdout)["mcpServers"]["open-index"]
    assert entry["url"] == "http://host:8080/mcp"
    assert entry["headers"]["Authorization"] == "Bearer s3cret"


def test_mcp_config_remote_reads_token_from_env(monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_TOKEN", "from-env")
    result = run("mcp-config", "--url", "host:8080")
    entry = json.loads(result.stdout)["mcpServers"]["open-index"]
    assert entry["headers"]["Authorization"] == "Bearer from-env"


def test_mcp_config_custom_server_name(brain_dir):
    result = run("mcp-config", "--brain", brain_dir, "--name", "acme")
    assert "acme" in json.loads(result.stdout)["mcpServers"]


def test_mcp_config_cli_flag_emits_a_command(brain_dir):
    result = run("mcp-config", "--brain", brain_dir, "--cli")
    assert result.stdout.startswith("claude mcp add open-index --")


def test_mcp_config_cli_flag_remote(brain_dir):
    result = run("mcp-config", "--url", "https://b.acme.com/mcp", "--token", "t", "--cli")
    assert "--transport http" in result.stdout
    assert 'Authorization: Bearer t' in result.stdout


def test_mcp_config_rejects_a_missing_brain(tmp_path):
    result = run("mcp-config", "--brain", tmp_path / "nope")
    assert result.exit_code == 1
    assert "no brain.yaml" in result.output


def test_mcp_config_does_not_open_the_backend(brain_dir, monkeypatch):
    """It must work when the search backend is unreachable — that's often *why*
    someone is fetching connection details."""
    monkeypatch.setenv("OPEN_INDEX_SEARCH_BACKEND", "opensearch")
    monkeypatch.setenv("OPEN_INDEX_OPENSEARCH_HOSTS", "http://127.0.0.1:1")
    assert run("mcp-config", "--brain", brain_dir).exit_code == 0


def test_mcp_config_output_is_pipeable(brain_dir, tmp_path):
    """`open-index mcp-config > .mcp.json` has to produce a usable file."""
    result = run("mcp-config", "--brain", brain_dir)
    target = tmp_path / ".mcp.json"
    target.write_text(result.stdout)
    assert json.loads(target.read_text())["mcpServers"]


# -- serve banner -------------------------------------------------------------


def test_serve_never_advertises_the_bind_address(brain_dir, served):
    """0.0.0.0 is not connectable; printing it is the original bug."""
    result = run("serve", "--brain", brain_dir)
    assert result.exit_code == 0
    connectable = [ln for ln in result.stdout.splitlines() if "/mcp" in ln]
    assert connectable, "expected at least one connectable URL"
    assert all("0.0.0.0" not in ln for ln in connectable)
    assert "http://127.0.0.1:8080/mcp" in result.stdout


def test_serve_warns_loudly_without_a_token(brain_dir, served):
    result = run("serve", "--brain", brain_dir)
    assert "auth: NONE" in result.stdout
    assert "read AND WRITE" in result.stdout


def test_serve_read_only_warning_says_read(brain_dir, served):
    result = run("serve", "--brain", brain_dir, "--read-only")
    assert "read-only" in result.stdout
    assert "read AND WRITE" not in result.stdout
    assert served["read_only"] is True


def test_serve_masks_the_token(brain_dir, served, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_TOKEN", "supersecretvalue")
    result = run("serve", "--brain", brain_dir)
    assert "supersecretvalue" not in result.stdout
    assert "auth: Authorization: Bearer" in result.stdout
    assert served["token"] == "supersecretvalue"


def test_serve_public_url_replaces_local_addresses(brain_dir, served):
    result = run("serve", "--brain", brain_dir, "--public-url", "https://b.acme.com")
    assert "https://b.acme.com/mcp" in result.stdout
    assert "127.0.0.1" not in result.stdout


def test_serve_suggests_the_mcp_config_command(brain_dir, served, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_TOKEN", "t0ken")
    result = run("serve", "--brain", brain_dir)
    assert "open-index mcp-config --url" in result.stdout
    assert "--token $OPEN_INDEX_TOKEN" in result.stdout


def test_serve_banner_reports_the_active_backend(brain_dir, served):
    result = run("serve", "--brain", brain_dir)
    assert "search backend: sqlite" in result.stdout


def test_serve_honours_explicit_host_and_port(brain_dir, served):
    result = run("serve", "--brain", brain_dir, "--host", "10.0.1.42", "--port", "9999")
    assert "http://10.0.1.42:9999/mcp" in result.stdout
    assert served["host"] == "10.0.1.42"
    assert served["port"] == 9999


def test_serve_suppresses_the_duplicate_library_warning(brain_dir, served):
    """The CLI prints its own richer warning, so serve_http must not repeat it."""
    run("serve", "--brain", brain_dir)
    assert served["warn_unauthenticated"] is False


def test_serve_fails_cleanly_on_a_missing_brain(tmp_path, served):
    result = run("serve", "--brain", tmp_path / "nope")
    assert result.exit_code == 1
    assert "no brain.yaml" in result.output
