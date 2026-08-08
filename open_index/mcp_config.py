"""Connection details for pointing an agent at a brain's MCP server.

The question every new user hits is "what exactly do I paste into my agent?" —
especially once the brain runs on another machine, where the bind address the
server prints (`0.0.0.0`) is not an address anything can connect to.

This module builds the answer as data, so the same config can be printed by
`open-index mcp-config`, echoed in the `open-index serve` banner, and tested.

Two shapes, matching the two ways to run a brain:

* **stdio** — the agent spawns `open-index mcp` itself. Local only; no URL, no
  token. This is what `open-index init` writes into `.mcp.json`.
* **http** — the agent connects to a running `open-index serve` by URL, with an
  optional `Authorization: Bearer <token>` header. This is the remote shape.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Optional

DEFAULT_SERVER_NAME = "open-index"

# The path `serve` mounts the streamable-HTTP endpoint on.
MCP_PATH = "/mcp"


# -- URLs ---------------------------------------------------------------------


def normalize_mcp_url(url: str) -> str:
    """Turn whatever the user typed into a full MCP endpoint URL.

    Accepts `host:8080`, `http://host:8080`, or `https://host/mcp` and always
    returns a scheme-qualified URL ending in the MCP path. People habitually
    paste the host or the base URL and then wonder why the agent can't connect.
    """
    u = url.strip()
    if not u:
        raise ValueError("empty MCP url")
    if "://" not in u:
        u = "http://" + u
    u = u.rstrip("/")
    if not u.endswith(MCP_PATH):
        u += MCP_PATH
    return u


def lan_ip() -> Optional[str]:
    """This machine's outbound IP, or None if it can't be determined.

    Opens a UDP socket toward a public address — no packets are actually sent;
    it just asks the routing table which local interface would be used. That is
    the address another machine on the network should connect to.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


def advertised_urls(
    host: str, port: int, public_url: Optional[str] = None
) -> list[tuple[str, str]]:
    """Return [(label, url)] a client could actually connect to.

    `host` is the *bind* address, which is often `0.0.0.0` — a wildcard meaning
    "every interface", not a reachable destination. In that case we advertise
    loopback plus the LAN address instead. `public_url` overrides everything:
    behind a reverse proxy, tunnel, or load balancer, only the operator knows
    the externally reachable name.
    """
    if public_url:
        return [("public", normalize_mcp_url(public_url))]

    if host in ("0.0.0.0", "::", "*", ""):
        urls = [("on this machine", normalize_mcp_url(f"http://127.0.0.1:{port}"))]
        ip = lan_ip()
        if ip:
            urls.append(("from another machine", normalize_mcp_url(f"http://{ip}:{port}")))
        return urls

    return [("", normalize_mcp_url(f"http://{host}:{port}"))]


# -- config blocks ------------------------------------------------------------


def local_stdio_config(
    brain_dir: str | Path, server_name: str = DEFAULT_SERVER_NAME
) -> dict:
    """MCP client config for a brain on this machine (agent spawns the server).

    The brain path is absolutized: the agent's working directory is rarely the
    brain directory, and a relative `--brain .` silently opens the wrong place.
    """
    root = Path(brain_dir).expanduser().resolve()
    return {
        "mcpServers": {
            server_name: {
                "command": "open-index",
                "args": ["mcp", "--brain", str(root)],
            }
        }
    }


def remote_http_config(
    url: str, token: Optional[str] = None, server_name: str = DEFAULT_SERVER_NAME
) -> dict:
    """MCP client config for a brain reachable over HTTP."""
    entry: dict = {"type": "http", "url": normalize_mcp_url(url)}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return {"mcpServers": {server_name: entry}}


# -- rendering ----------------------------------------------------------------


def mask_token(token: Optional[str], keep: int = 4) -> str:
    """Render a token safely for a server log/banner."""
    if not token:
        return ""
    if len(token) <= keep:
        return "*" * len(token)
    return token[:keep] + "…" + "*" * 6


def as_json(config: dict) -> str:
    """The `.mcp.json` / `claude_desktop_config.json` block."""
    return json.dumps(config, indent=2)


def as_claude_cli(config: dict, server_name: str = DEFAULT_SERVER_NAME) -> str:
    """The equivalent `claude mcp add` one-liner."""
    entry = config["mcpServers"][server_name]
    if "url" in entry:
        cmd = f"claude mcp add --transport http {server_name} {entry['url']}"
        for key, value in (entry.get("headers") or {}).items():
            cmd += f' --header "{key}: {value}"'
        return cmd
    args = " ".join(entry.get("args", []))
    return f"claude mcp add {server_name} -- {entry['command']} {args}".rstrip()
