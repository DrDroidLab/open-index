"""Server construction, the bearer gate, and the stdio/HTTP entry points.

The auth middleware is the only thing standing between a networked brain and
anyone who can reach the port, so it is exercised directly at the ASGI layer.
"""

import asyncio

import pytest

pytest.importorskip("mcp")

from open_index.mcp_server import (  # noqa: E402
    _bearer_auth_middleware,
    _load_server_class,
    build_server,
    serve,
    serve_http,
)


# -- SDK compatibility shim ---------------------------------------------------


def test_load_server_class_returns_a_usable_class():
    cls = _load_server_class()
    assert hasattr(cls, "tool")


def test_falls_back_to_fastmcp_on_older_sdks(monkeypatch):
    """mcp 1.x exposes FastMCP and no MCPServer. The installed SDK may be 2.x,
    so stand in a fake 1.x-shaped module rather than importing one."""
    import sys
    import types

    import mcp.server

    monkeypatch.delattr(mcp.server, "MCPServer", raising=False)
    fake = types.ModuleType("mcp.server.fastmcp")
    fake.FastMCP = type("FastMCP", (), {})
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake)

    assert _load_server_class() is fake.FastMCP


def test_raises_a_helpful_error_when_no_sdk_is_present(monkeypatch):
    """Neither entry point available — point the user at the extra to install."""
    import sys
    import types

    import mcp.server

    monkeypatch.delattr(mcp.server, "MCPServer", raising=False)
    # A fastmcp module that exists but has no FastMCP attribute reproduces the
    # "installed but unusable" case as an ImportError from the `from` import.
    monkeypatch.setitem(
        sys.modules, "mcp.server.fastmcp", types.ModuleType("mcp.server.fastmcp")
    )
    with pytest.raises(SystemExit, match="open-index\\[mcp\\]"):
        _load_server_class()


def test_read_only_server_is_named_as_such(brain):
    assert "read-only" in build_server(brain, read_only=True).name


# -- bearer auth --------------------------------------------------------------


class _Recorder:
    """Minimal ASGI downstream app + response collector."""

    def __init__(self):
        self.called = False
        self.messages = []

    async def app(self, scope, receive, send):
        self.called = True

    async def send(self, message):
        self.messages.append(message)

    async def receive(self):  # pragma: no cover - never awaited in these tests
        return {"type": "http.request"}

    @property
    def status(self):
        return next((m["status"] for m in self.messages if "status" in m), None)


def _call(app, headers):
    rec = _Recorder()
    wrapped = _bearer_auth_middleware(rec.app, "goodtoken")
    scope = {"type": "http", "headers": headers}
    asyncio.new_event_loop().run_until_complete(wrapped(scope, rec.receive, rec.send))
    return rec


def test_correct_token_passes_through():
    rec = _call(None, [(b"authorization", b"Bearer goodtoken")])
    assert rec.called is True
    assert rec.status is None


def test_missing_header_is_rejected():
    rec = _call(None, [])
    assert rec.called is False
    assert rec.status == 401


def test_wrong_token_is_rejected():
    assert _call(None, [(b"authorization", b"Bearer nope")]).status == 401


def test_bare_token_without_the_bearer_scheme_is_rejected():
    assert _call(None, [(b"authorization", b"goodtoken")]).status == 401


def test_rejection_body_is_sent():
    rec = _call(None, [])
    bodies = [m["body"] for m in rec.messages if m["type"] == "http.response.body"]
    assert bodies == [b"unauthorized"]


def test_non_http_scopes_bypass_the_gate():
    """Lifespan events carry no headers and must not be 401'd."""
    rec = _Recorder()
    wrapped = _bearer_auth_middleware(rec.app, "goodtoken")
    asyncio.new_event_loop().run_until_complete(
        wrapped({"type": "lifespan"}, rec.receive, rec.send)
    )
    assert rec.called is True


def test_scope_without_a_headers_key_is_rejected_not_crashed():
    rec = _Recorder()
    wrapped = _bearer_auth_middleware(rec.app, "goodtoken")
    asyncio.new_event_loop().run_until_complete(
        wrapped({"type": "http"}, rec.receive, rec.send)
    )
    assert rec.status == 401


# -- entry points -------------------------------------------------------------


def test_serve_stdio_runs_the_server(brain, monkeypatch):
    ran = {}
    monkeypatch.setattr(
        "open_index.mcp_server.build_server",
        lambda b, read_only=False: type("S", (), {"run": lambda self: ran.update(ok=True)})(),
    )
    serve(str(brain.config.root))
    assert ran == {"ok": True}


def _stub_uvicorn(monkeypatch):
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: captured.update(
        app=app, host=host, port=port))
    return captured


def test_serve_http_wraps_the_app_when_a_token_is_set(brain, monkeypatch):
    captured = _stub_uvicorn(monkeypatch)
    serve_http(str(brain.config.root), token="t0k", port=1234)
    assert captured["port"] == 1234
    # The bearer wrapper is a plain function; the bare ASGI app is not.
    assert captured["app"].__name__ == "wrapped"


def test_serve_http_without_a_token_warns_and_does_not_wrap(brain, monkeypatch, capsys):
    captured = _stub_uvicorn(monkeypatch)
    serve_http(str(brain.config.root))
    assert "WARNING" in capsys.readouterr().err
    assert getattr(captured["app"], "__name__", "") != "wrapped"


def test_serve_http_can_suppress_its_warning(brain, monkeypatch, capsys):
    """The CLI prints a richer warning and passes warn_unauthenticated=False."""
    _stub_uvicorn(monkeypatch)
    serve_http(str(brain.config.root), warn_unauthenticated=False)
    assert "WARNING" not in capsys.readouterr().err


def test_serve_http_reports_a_missing_uvicorn(brain, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("no uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_uvicorn)
    with pytest.raises(SystemExit, match="open-index\\[serve\\]"):
        serve_http(str(brain.config.root))


# -- proxy / load-balancer host validation ------------------------------------
#
# The SDK turns on DNS-rebinding protection with a localhost-only allow-list, so
# every request arriving through a reverse proxy is rejected with
# "421 Invalid Host header". These pin the allow-list we build instead.


def test_public_url_host_is_allowed():
    from open_index.mcp_server import build_transport_security

    settings = build_transport_security(public_url="http://203.0.113.10/support/mcp")
    assert settings.enable_dns_rebinding_protection is True
    assert "203.0.113.10" in settings.allowed_hosts


def test_both_with_and_without_port_are_allowed():
    """A proxy drops the port when it is the scheme default; a direct client
    sends it. Both must pass or one of the two paths breaks."""
    from open_index.mcp_server import build_transport_security

    hosts = build_transport_security(public_url="https://brain.acme.com/mcp").allowed_hosts
    assert "brain.acme.com" in hosts
    assert "brain.acme.com:*" in hosts


def test_explicit_host_and_port_survives():
    from open_index.mcp_server import build_transport_security

    hosts = build_transport_security(
        public_url="https://brain.acme.com:8443/mcp").allowed_hosts
    assert "brain.acme.com:8443" in hosts
    assert "brain.acme.com" in hosts


def test_loopback_is_always_allowed():
    """Health checks and on-box debugging must keep working."""
    from open_index.mcp_server import build_transport_security

    hosts = build_transport_security(public_url="https://brain.acme.com/mcp").allowed_hosts
    assert {"127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"} <= set(hosts)


def test_extra_allowed_hosts_are_honoured():
    from open_index.mcp_server import build_transport_security

    hosts = build_transport_security(allowed_hosts=["lb.internal", "other:9000"]).allowed_hosts
    assert "lb.internal" in hosts and "other:9000" in hosts


def test_wildcard_disables_the_check():
    """Explicit opt-out for a trusted proxy that already validates Host."""
    from open_index.mcp_server import build_transport_security

    assert build_transport_security(
        allowed_hosts=["*"]).enable_dns_rebinding_protection is False


def test_origins_cover_http_and_https():
    from open_index.mcp_server import build_transport_security

    origins = build_transport_security(public_url="https://brain.acme.com/mcp").allowed_origins
    assert "https://brain.acme.com" in origins
    assert "http://brain.acme.com" in origins


def test_blank_entries_are_ignored():
    from open_index.mcp_server import build_transport_security

    settings = build_transport_security(allowed_hosts=["", "  "])
    assert settings.enable_dns_rebinding_protection is True


def test_serve_http_passes_the_allow_list_through(brain, monkeypatch):
    """The settings must actually reach streamable_http_app."""
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: None)

    from open_index import mcp_server

    real = mcp_server.build_server

    def spy(b, read_only=False):
        server = real(b, read_only=read_only)
        original = server.streamable_http_app

        def wrapper(**kwargs):
            captured.update(kwargs)
            return original(**kwargs)

        server.streamable_http_app = wrapper
        return server

    monkeypatch.setattr(mcp_server, "build_server", spy)
    mcp_server.serve_http(str(brain.config.root), host="0.0.0.0",
                          public_url="https://brain.acme.com/mcp",
                          warn_unauthenticated=False)

    assert captured["host"] == "0.0.0.0"
    assert "brain.acme.com" in captured["transport_security"].allowed_hosts
