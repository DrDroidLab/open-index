#!/usr/bin/env python3
"""Run several open-index brains on one host, behind one Caddy.

Each index gets its own brain container (its own doc_types, entities, token and
read/write policy) but they all share one OpenSearch cluster, with one cluster
index per brain. That sharing is the point: the cluster is the expensive part
(~1GB), so adding an index costs ~250MB rather than another cluster.

Caddy maps /<name>/ to that brain, terminates TLS, and serves each index's UI
    <public_base_url>/<name>/mcp

Commands:
    ./fleet.py up                 render config, create missing brains, start
    ./fleet.py add <name>         append an index to indexes.yml, then up
    ./fleet.py tokens             print connection details for every index
    ./fleet.py render             write the generated files without starting
    ./fleet.py status             what is running
    ./fleet.py logs [name]        tail logs

Everything is regenerated from indexes.yml, so that file is the source of
truth — edit it rather than the generated compose/Caddy config.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CONFIG = HERE / "indexes.yml"
COMPOSE = HERE / "docker-compose.generated.yml"
CADDY_DIR = HERE / "caddy"
CADDY_FILE = CADDY_DIR / "Caddyfile"
ENV_FILE = HERE / ".env"

# The uid the container runs as (see Dockerfile). Bind-mounted brain directories
# must be writable by it or indexing fails on the first write.
CONTAINER_UID = 10001

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def load_config() -> dict:
    try:
        import yaml
    except ImportError:
        die("pyyaml is required:  pip install pyyaml   (or: apt install python3-yaml)")
    if not CONFIG.exists():
        die(f"no {CONFIG.name} — copy the example and edit it:\n"
            f"  cp {CONFIG.with_name('indexes.example.yml').name} {CONFIG.name}")
    config = yaml.safe_load(CONFIG.read_text()) or {}

    config.setdefault("public_base_url", "http://localhost")
    config.setdefault("brains_root", str(Path.home() / "brains"))
    config.setdefault("http_port", 80)
    config.setdefault("opensearch_heap", "512m")
    # Auth on by default. Turning it off is a deliberate choice for a public
    # demo whose data is synthetic — it opens both the UI and, unless the index
    # is read_only, the write tools to anyone who has the URL.
    config.setdefault("auth", True)
    config["public_base_url"] = config["public_base_url"].rstrip("/")

    indexes = config.get("indexes") or []
    if not indexes:
        die("indexes.yml lists no indexes")

    seen = set()
    for entry in indexes:
        name = entry.get("name", "")
        # The name becomes a URL path, a container name and an OpenSearch index,
        # so keep it to the intersection of what all three accept.
        if not NAME_RE.match(name):
            die(f"invalid index name {name!r} — use lowercase letters, digits and "
                "hyphens (2-40 chars, not starting or ending with a hyphen)")
        if name in seen:
            die(f"duplicate index name {name!r}")
        seen.add(name)
    return config


# -- tokens -------------------------------------------------------------------


def env_var(name: str) -> str:
    return "OPEN_INDEX_TOKEN_" + name.upper().replace("-", "_")


def ui_pw_var(name: str) -> str:
    return "OPEN_INDEX_UI_PASSWORD_" + name.upper().replace("-", "_")


def caddy_password_hash(password: str) -> str:
    """A bcrypt hash in the form Caddy's basicauth expects.

    Produced by Caddy itself (`caddy hash-password`) rather than a Python bcrypt
    dependency — the image is already being pulled for the proxy.
    """
    result = subprocess.run(
        docker() + ["run", "--rm", "caddy:2.8-alpine",
                    "caddy", "hash-password", "--plaintext", password],
        capture_output=True, text=True)
    if result.returncode != 0:
        die(f"could not hash the UI password via caddy: {result.stderr.strip()}")
    return result.stdout.strip()


def load_env() -> dict:
    if not ENV_FILE.exists():
        return {}
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def ensure_secrets(config: dict) -> dict:
    """One MCP token and one UI password per index, generated once.

    Both are only created when missing, so re-running `up` never rotates a
    credential behind your back and invalidates clients.
    """
    values = load_env()
    if not config.get("auth", True):
        # Nothing to generate, and nothing to leave lying around either.
        ENV_FILE.write_text("# auth: false in indexes.yml — no credentials in use.\n")
        ENV_FILE.chmod(0o600)
        return values
    created = []
    for entry in config["indexes"]:
        name = entry["name"]
        if not values.get(env_var(name)):
            values[env_var(name)] = secrets.token_hex(32)
            created.append(f"{name} (token)")
        # The UI has no auth of its own, so it gets a password whenever it is
        # exposed; Caddy gets the bcrypt hash via the environment.
        if entry.get("ui", True) and not values.get(ui_pw_var(name)):
            password = secrets.token_urlsafe(18)
            values[ui_pw_var(name)] = password
            # Caddy needs the hash, we keep the plaintext so `tokens` can print
            # something a human can actually type.
            values["UI_HASH_" + name.upper().replace("-", "_")] = \
                caddy_password_hash(password)
            created.append(f"{name} (ui password)")

    lines = ["# Generated by fleet.py. One bearer token and one UI password per index.",
             "# Deleting a line regenerates that credential on the next `up`.", ""]
    lines += [f"{k}={v}" for k, v in sorted(values.items())]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)
    if created:
        print(f"  generated: {', '.join(created)}")
    return values


# -- rendering ----------------------------------------------------------------


def render_compose(config: dict) -> str:
    """Three containers, whatever the index count.

    Previously this generated a brain container and a UI container per index —
    each carrying its own Python runtime and its own ~250MB resident embedding
    model, which caps a modest host at a handful of indexes. `serve --brains`
    and the explorer's brains-root mode both serve every brain from one process
    with the model loaded once, so the cost of an index drops to a couple of MB
    and hundreds fit where a handful did.
    """
    root = config["brains_root"].rstrip("/")
    base = config["public_base_url"]
    auth = config.get("auth", True)

    token_env = ""
    if auth:
        for entry in config["indexes"]:
            var = env_var(entry["name"])
            token_env += f"      {var}: ${{{var}}}\n"

    return f"""# GENERATED BY fleet.py — do not edit. Change indexes.yml and re-run `fleet.py up`.
#
# One MCP process serving every brain, one explorer serving every brain, and
# Caddy in front doing TLS and routing. Adding an index adds no containers.

services:
  mcp:
    image: open-index:local
    build:
      context: ../..
      args:
        EXTRAS: serve,opensearch,ui,semantic
    container_name: oi-mcp
    command: ["serve", "--brains", "/brains", "--host", "0.0.0.0", "--port", "8080"]
    environment:
      OPEN_INDEX_PUBLIC_URL: {base}
      OPEN_INDEX_EMBEDDING_CACHE: /home/openindex/model-cache
{token_env}    volumes:
      - {root}:/brains
      - model-cache:/home/openindex/model-cache
    restart: unless-stopped

  ui:
    image: open-index:local
    container_name: oi-ui
    command: ["ui", "--port", "8501"]
    environment:
      # The explorer discovers every brain under this root and offers a picker.
      OPEN_INDEX_BRAINS_ROOT: /brains
      OPEN_INDEX_EMBEDDING_CACHE: /home/openindex/model-cache
      STREAMLIT_SERVER_BASE_URL_PATH: ui
      STREAMLIT_SERVER_HEADLESS: "true"
      STREAMLIT_SERVER_ADDRESS: 0.0.0.0
      # The browser's Origin is the proxy's, not Streamlit's; its XSRF check
      # rejects that and the session never connects.
      STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION: "false"
      STREAMLIT_SERVER_ENABLE_CORS: "false"
      STREAMLIT_BROWSER_GATHER_USAGE_STATS: "false"
    volumes:
      - {root}:/brains
      - model-cache:/home/openindex/model-cache
    restart: unless-stopped

  caddy:
    image: caddy:2.8-alpine
    container_name: oi-caddy
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      # Certificates and ACME account state. Losing this means re-issuing on
      # every restart, which will hit Let's Encrypt rate limits.
      - caddy-data:/data
      - caddy-config:/config
    ports:
      - "80:80"      # ACME HTTP-01 challenge + redirect to HTTPS
      - "443:443"
    depends_on:
      - mcp
      - ui
    restart: unless-stopped

volumes:
  model-cache:
  caddy-data:
  caddy-config:
"""


def render_caddyfile(config: dict) -> str:
    """One MCP upstream for every index, one explorer, automatic HTTPS.

    The MCP process already routes /<name>/mcp internally, so Caddy passes the
    path through untouched rather than mounting each index separately.
    """
    from urllib.parse import urlparse

    base = config["public_base_url"]
    host = urlparse(base).netloc or base

    listing = {}
    for e in config["indexes"]:
        listing[e["name"]] = {
            "mcp": f"{base}/{e['name']}/mcp",
            "ui": f"{base}/ui/?brain={e['name']}",
            "description": e.get("description", ""),
        }
    directory = json.dumps(listing, indent=2).replace("`", "'")

    index_paths = " ".join(f"/{e['name']}/*" for e in config["indexes"])

    return f"""# GENERATED BY fleet.py — do not edit. Change indexes.yml and re-run `fleet.py up`.

{host} {{
	# Load-balancer probe: unauthenticated, and free of any index detail.
	handle /healthz {{
		respond "ok" 200
	}}

	# The explorer. One process for every brain; ?brain=<name> picks one.
	# No prefix stripping — Streamlit runs with baseUrlPath=ui.
	handle /ui* {{
		reverse_proxy ui:8501
	}}

	# Every index's MCP endpoint. The MCP process routes /<name>/mcp itself, so
	# the path is passed through rather than stripped.
	@indexes path {index_paths}
	handle @indexes {{
		reverse_proxy mcp:8080 {{
			# Streamable HTTP holds responses open; never buffer them.
			flush_interval -1
		}}
	}}

	# Directory of what this host serves.
	handle {{
		header Content-Type application/json
		respond `{directory}` 200
	}}
}}
"""


# -- brains -------------------------------------------------------------------


def reapply_permissions(name: str, brains_root: Path) -> None:
    """Give the container ownership and keep group write for the host user.

    Re-applied on every `up`, not just at creation: the container writes with
    umask 022, so any directory it creates (a new entities/<doc_type>/, the
    files an agent writes back) loses group write and the operator can no longer
    drop a CSV in or rsync data across without sudo.
    """
    path = brains_root / name
    run(["sudo", "chown", "-R", f"{CONTAINER_UID}:{os.getgid()}", str(path)], quiet=True)
    run(["sudo", "chmod", "-R", "g+rwX", str(path)], quiet=True)
    run(["sudo", "chmod", "g+s", str(path)], quiet=True)


def ensure_brain(name: str, brains_root: Path) -> bool:
    """Create and permission a brain directory if it doesn't exist yet."""
    path = brains_root / name
    if (path / "brain.yaml").exists():
        return False

    # Ownership has to be set before the container writes anything: it runs as
    # uid 10001 and cannot create files in a directory owned by the host user.
    path.mkdir(parents=True, exist_ok=True)
    run(["sudo", "chown", "-R", f"{CONTAINER_UID}:{os.getgid()}", str(path)])
    run(["sudo", "chmod", "-R", "g+rwX", str(path)])
    run(["sudo", "chmod", "g+s", str(path)])
    run(docker() + ["run", "--rm", "--entrypoint", "open-index",
                    "-v", f"{path}:/brain", "open-index:local",
                    "init", name, "/brain"], quiet=True)
    print(f"  created brain: {path}")
    return True


# -- docker -------------------------------------------------------------------


def docker() -> list[str]:
    """`docker` if the user can reach the daemon, otherwise `sudo docker`."""
    probe = subprocess.run(["docker", "info"], capture_output=True)
    return ["docker"] if probe.returncode == 0 else ["sudo", "docker"]


def compose_cmd() -> list[str]:
    return docker() + ["compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE)]


def run(cmd: list[str], quiet: bool = False, check: bool = True) -> int:
    result = subprocess.run(cmd, capture_output=quiet)
    if check and result.returncode != 0:
        if quiet and result.stderr:
            sys.stderr.write(result.stderr.decode())
        die(f"command failed: {' '.join(cmd)}")
    return result.returncode


# -- commands -----------------------------------------------------------------


def cmd_render(config: dict, values: dict | None = None) -> None:
    # Rendering needs the UI hashes for the Caddy env, so generate any that
    # are missing rather than failing on a half-populated .env.
    if values is None:
        values = ensure_secrets(config)
    CADDY_DIR.mkdir(exist_ok=True)
    COMPOSE.write_text(render_compose(config))
    CADDY_FILE.write_text(render_caddyfile(config))
    print(f"  wrote {COMPOSE.name} and caddy/Caddyfile")


def cmd_up(config: dict) -> None:
    names = [e["name"] for e in config["indexes"]]
    values = ensure_secrets(config)
    cmd_render(config, values)

    # The image must exist before `init` can run in it.
    print("  building image ...")
    run(docker() + ["build", "-q", "-t", "open-index:local", str(REPO)], quiet=True)

    brains_root = Path(config["brains_root"])
    brains_root.mkdir(parents=True, exist_ok=True)
    for name in names:
        ensure_brain(name, brains_root)
        reapply_permissions(name, brains_root)

    print("  starting ...")
    run(compose_cmd() + ["up", "-d", "--remove-orphans"])
    print()
    cmd_tokens(config)


def cmd_add(config: dict, name: str, description: str) -> None:
    if not NAME_RE.match(name):
        die(f"invalid index name {name!r}")
    if any(e["name"] == name for e in config["indexes"]):
        die(f"index {name!r} already exists")
    entry = f"\n  - name: {name}\n"
    if description:
        entry += f"    description: {description}\n"
    CONFIG.write_text(CONFIG.read_text().rstrip() + "\n" + entry)
    print(f"  added {name!r} to indexes.yml")
    cmd_up(load_config())


def cmd_tokens(config: dict) -> None:
    values = load_env()
    base = config["public_base_url"]
    print("Indexes on this host:\n")
    for entry in config["indexes"]:
        name = entry["name"]
        mode = "read-only" if entry.get("read_only") else "read+write"
        print(f"  {name}  ({mode})")
        print(f"    url:   {base}/{name}/mcp")
        print(f"    token: {values.get(env_var(name), '(not generated yet)')}")
        print()
    first = config["indexes"][0]["name"]
    print("Add one to an agent (from this directory):")
    print(f"  source .env")
    print(f"  open-index mcp-config --url {base}/{first}/mcp "
          f"--token ${env_var(first)} > .mcp.json")


def cmd_status(config: dict) -> None:
    run(compose_cmd() + ["ps", "--format",
                         "  {{.Name}}\t{{.Status}}"], check=False)


def cmd_logs(config: dict, name: str | None) -> None:
    target = [f"brain-{name}"] if name else []
    run(compose_cmd() + ["logs", "--tail", "60"] + target, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="render, create missing brains, start everything")
    sub.add_parser("render", help="write the generated files only")
    sub.add_parser("tokens", help="print connection details for every index")
    sub.add_parser("status", help="what is running")
    p_add = sub.add_parser("add", help="add an index and start it")
    p_add.add_argument("name")
    p_add.add_argument("--description", default="")
    p_logs = sub.add_parser("logs", help="tail logs")
    p_logs.add_argument("name", nargs="?")

    args = parser.parse_args()
    if shutil.which("docker") is None:
        die("docker is not installed on this host")

    config = load_config()
    if args.command == "up":
        cmd_up(config)
    elif args.command == "render":
        cmd_render(config)
    elif args.command == "tokens":
        cmd_tokens(config)
    elif args.command == "status":
        cmd_status(config)
    elif args.command == "add":
        cmd_add(config, args.name, args.description)
    elif args.command == "logs":
        cmd_logs(config, args.name)


if __name__ == "__main__":
    main()
