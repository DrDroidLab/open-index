"""Connection details an agent needs to reach a brain.

The failure these guard against is a user pasting something that cannot work —
a bind address, a URL missing the /mcp path, or a relative brain path that
resolves against the agent's working directory instead of the brain.
"""

import json
import shutil
from pathlib import Path

import pytest

from open_index.mcp_config import (
    advertised_urls,
    as_claude_cli,
    as_json,
    local_stdio_config,
    mask_token,
    normalize_mcp_url,
    remote_http_config,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"


# -- URL normalization --------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("host:8080", "http://host:8080/mcp"),
        ("http://host:8080", "http://host:8080/mcp"),
        ("http://host:8080/", "http://host:8080/mcp"),
        ("http://host:8080/mcp", "http://host:8080/mcp"),
        ("http://host:8080/mcp/", "http://host:8080/mcp"),
        ("https://brain.acme.com/mcp", "https://brain.acme.com/mcp"),
        ("  https://brain.acme.com  ", "https://brain.acme.com/mcp"),
    ],
)
def test_normalize_mcp_url_accepts_what_people_actually_type(given, expected):
    assert normalize_mcp_url(given) == expected


def test_normalize_mcp_url_preserves_a_nested_path():
    """Behind a proxy the endpoint may not be at the root."""
    assert normalize_mcp_url("https://acme.com/brains/team-a/mcp") == (
        "https://acme.com/brains/team-a/mcp"
    )


def test_normalize_mcp_url_rejects_empty():
    with pytest.raises(ValueError):
        normalize_mcp_url("   ")


# -- advertised URLs ----------------------------------------------------------


def test_wildcard_bind_is_never_advertised():
    """0.0.0.0 is a bind address, not somewhere a client can connect."""
    urls = [url for _label, url in advertised_urls("0.0.0.0", 8080)]
    assert urls, "expected at least loopback"
    assert all("0.0.0.0" not in url for url in urls)
    assert "http://127.0.0.1:8080/mcp" in urls


def test_concrete_bind_host_is_advertised_as_given():
    assert advertised_urls("10.0.1.42", 9000) == [("", "http://10.0.1.42:9000/mcp")]


def test_public_url_overrides_everything():
    """Behind a proxy, only the operator knows the reachable name."""
    assert advertised_urls("0.0.0.0", 8080, public_url="https://brain.acme.com") == [
        ("public", "https://brain.acme.com/mcp")
    ]


# -- config blocks ------------------------------------------------------------


def test_local_config_absolutizes_the_brain_path(tmp_path, monkeypatch):
    """A relative --brain resolves against the agent's cwd, not the brain."""
    dst = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, dst)
    monkeypatch.chdir(tmp_path)

    entry = local_stdio_config("support-brain")["mcpServers"]["open-index"]

    path = Path(entry["args"][entry["args"].index("--brain") + 1])
    assert path.is_absolute()
    assert (path / "brain.yaml").exists()


def test_remote_config_carries_the_bearer_header():
    entry = remote_http_config("host:8080", token="s3cret")["mcpServers"]["open-index"]
    assert entry["type"] == "http"
    assert entry["url"] == "http://host:8080/mcp"
    assert entry["headers"] == {"Authorization": "Bearer s3cret"}


def test_remote_config_omits_headers_without_a_token():
    entry = remote_http_config("host:8080")["mcpServers"]["open-index"]
    assert "headers" not in entry


def test_server_name_is_configurable():
    """Two brains registered in one agent need distinct names."""
    config = remote_http_config("host:8080", server_name="acme-brain")
    assert "acme-brain" in config["mcpServers"]


def test_configs_are_valid_json(tmp_path):
    dst = tmp_path / "b"
    shutil.copytree(EXAMPLE, dst)
    for config in (local_stdio_config(dst), remote_http_config("h:1", token="t")):
        assert json.loads(as_json(config)) == config


# -- rendering ----------------------------------------------------------------


def test_claude_cli_http_form():
    cmd = as_claude_cli(remote_http_config("https://brain.acme.com/mcp", token="t0k"))
    assert cmd == (
        "claude mcp add --transport http open-index https://brain.acme.com/mcp "
        '--header "Authorization: Bearer t0k"'
    )


def test_claude_cli_stdio_form(tmp_path):
    dst = tmp_path / "b"
    shutil.copytree(EXAMPLE, dst)
    cmd = as_claude_cli(local_stdio_config(dst))
    assert cmd.startswith("claude mcp add open-index -- open-index mcp --brain /")


def test_lan_ip_returns_an_address_or_none():
    from open_index.mcp_config import lan_ip

    ip = lan_ip()
    assert ip is None or ip.count(".") == 3


def test_lan_ip_survives_a_hostless_network(monkeypatch):
    """No route to the outside world must degrade to loopback-only, not crash."""
    import socket

    from open_index import mcp_config

    def no_network(*_a, **_k):
        raise OSError("network unreachable")

    monkeypatch.setattr(socket, "socket", no_network)
    assert mcp_config.lan_ip() is None
    # ...and the caller still advertises something usable.
    assert advertised_urls("0.0.0.0", 8080) == [
        ("on this machine", "http://127.0.0.1:8080/mcp")
    ]


def test_mask_token_does_not_leak_the_secret():
    assert "supersecretvalue" not in mask_token("supersecretvalue")
    assert mask_token("supersecretvalue").startswith("supe")
    assert mask_token("") == ""
    assert "ab" not in mask_token("ab")
