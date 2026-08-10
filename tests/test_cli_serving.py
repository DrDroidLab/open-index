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


# -- serving many brains from one process --------------------------------------


@pytest.fixture
def brains_root(tmp_path):
    from open_index.brain import Brain

    root = tmp_path / "brains"
    root.mkdir()
    for name in ("alpha", "beta"):
        shutil.copytree(EXAMPLE, root / name)
        Brain.open(root / name).index()
    return root


@pytest.fixture
def served_multi(monkeypatch):
    captured = {}
    monkeypatch.setattr("open_index.mcp_server.serve_http_multi",
                        lambda root, **kw: captured.update(root=root, **kw))
    return captured


def test_brains_flag_serves_every_brain(brains_root, served_multi):
    result = run("serve", "--brains", brains_root)
    assert result.exit_code == 0
    assert served_multi["root"] == str(brains_root)


def test_multi_banner_lists_each_brain_url(brains_root, served_multi):
    result = run("serve", "--brains", brains_root, "--public-url", "https://x.example.com")
    assert "2 brains" in result.stdout
    assert "https://x.example.com/alpha/mcp" in result.stdout
    assert "https://x.example.com/beta/mcp" in result.stdout


def test_multi_banner_flags_ungated_brains(brains_root, served_multi, monkeypatch):
    monkeypatch.delenv("OPEN_INDEX_TOKEN", raising=False)
    result = run("serve", "--brains", brains_root)
    assert "OPEN" in result.stdout, "an unauthenticated brain must be called out"


def test_multi_banner_shows_a_gated_brain_as_tokened(brains_root, served_multi, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_TOKEN_ALPHA", "t")
    result = run("serve", "--brains", brains_root)
    assert "token" in result.stdout


def test_multi_banner_points_at_the_directory_and_health(brains_root, served_multi):
    result = run("serve", "--brains", brains_root)
    assert "directory:" in result.stdout and "healthz" in result.stdout


def test_brains_root_with_no_brains_is_a_clean_error(tmp_path, served_multi):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run("serve", "--brains", empty)
    assert result.exit_code == 1
    assert "no brains under" in result.output


def test_serve_http_multi_starts_uvicorn(brains_root, monkeypatch):
    import uvicorn

    from open_index import mcp_server

    captured = {}
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, host, port: captured.update(app=app, port=port))
    mcp_server.serve_http_multi(str(brains_root), port=9001)
    assert captured["port"] == 9001
    assert captured["app"] is not None


def test_serve_http_multi_refuses_an_empty_root(tmp_path, monkeypatch):
    import uvicorn

    from open_index import mcp_server

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no brains found"):
        mcp_server.serve_http_multi(str(empty))
