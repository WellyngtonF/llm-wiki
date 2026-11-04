"""OPS-01: the local HTTP MCP surface, and the boundary that makes it safe.

Every test here states a property of the transport, not of a tool. That the
tool surface is the *same* surface is proved by comparing an envelope taken
over HTTP with the envelope the stdio funnel produces for the same call.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

pytest.importorskip("mcp")
pytest.importorskip("starlette")
httpx = pytest.importorskip("httpx")

PROTOCOL_VERSION = "2025-11-25"
TOKEN = "test-token-value-not-a-real-secret"
ENDPOINT = "http://127.0.0.1:8765/mcp"
ACCEPT = "application/json, text/event-stream"


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")


def _headers(**overrides) -> dict:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": ACCEPT,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    headers.update(overrides)
    return {name: value for name, value in headers.items() if value is not None}


def _initialize_body() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ops-01-test", "version": "1"},
        },
    }


def _built_app():
    import mcp_http

    return mcp_http.build_app(token=TOKEN, host="127.0.0.1", port=8765)


@contextlib.asynccontextmanager
async def _running(app):
    """Drive the ASGI lifespan by hand: httpx's ASGITransport does not.

    The session manager refuses to serve before `run()` has opened its task
    group, so a test that skipped the lifespan would be testing a different
    server from the one uvicorn starts.
    """
    inbox: asyncio.Queue = asyncio.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0"}},
            inbox.get,
            outbox.put,
        )
    )
    await inbox.put({"type": "lifespan.startup"})
    assert (await outbox.get())["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await outbox.get()
        await task


async def _post(body, **header_overrides):
    """Always against a *running* app.

    A refusal test that skipped the lifespan could not tell "the guard refused
    it" from "the transport blew up because it was never started" - both look
    like a failure, and only one of them is the property under test.
    """
    app = _built_app()
    async with _running(app), _client(app) as client:
        return await client.post(ENDPOINT, headers=_headers(**header_overrides), json=body)


def _refused_status(**header_overrides) -> int:
    response = asyncio.run(_post(_initialize_body(), **header_overrides))
    return response.status_code


# ---------------------------------------------------------------------------
# Bind address
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "localhost", ""])
def test_a_non_loopback_bind_is_refused(host):
    """A private vault must never be reachable off this machine."""
    import mcp_http

    with pytest.raises(mcp_http.HttpSurfaceError) as caught:
        mcp_http.require_loopback_host(host)
    assert "127.0.0.1" in str(caught.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_literal_loopback_is_accepted(host):
    import mcp_http

    assert mcp_http.require_loopback_host(host) == host


def test_build_app_refuses_a_non_loopback_host():
    """The refusal sits on the path that builds the server, not only in a helper."""
    import mcp_http

    with pytest.raises(mcp_http.HttpSurfaceError):
        mcp_http.build_app(token=TOKEN, host="0.0.0.0", port=8765)


def test_serve_refuses_a_non_loopback_host_before_minting_a_token(tmp_path):
    import mcp_http

    token_path = tmp_path / "token"
    with pytest.raises(mcp_http.HttpSurfaceError):
        mcp_http.serve("0.0.0.0", 8765, token_path)
    assert not token_path.exists()


# ---------------------------------------------------------------------------
# The bearer token
# ---------------------------------------------------------------------------


def test_the_token_file_is_private_and_survives_a_restart(tmp_path):
    import mcp_http

    path = tmp_path / "run" / "mcp-http" / "token"
    token = mcp_http.ensure_token(path)
    assert len(token) >= 32
    assert mcp_http.ensure_token(path) == token, "a restart must not invalidate agents"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_world_readable_token_file_is_refused(tmp_path):
    """A secret other users can read is not a secret; fail closed, do not warn."""
    import mcp_http

    if os.name != "posix":
        pytest.skip("file modes are not enforceable on this platform")
    path = tmp_path / "token"
    path.write_text("something\n", encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(mcp_http.HttpSurfaceError):
        mcp_http.ensure_token(path)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_an_unauthenticated_request_is_refused():
    """Loopback is not authentication: every local process can reach this port."""
    response = asyncio.run(_post(_initialize_body(), Authorization=None))
    assert response.status_code == 401
    assert "bearer" in response.headers.get("www-authenticate", "").lower()


@pytest.mark.parametrize(
    "authorization",
    ["Bearer wrong-token", "Bearer ", "Basic dXNlcjpwYXNz", TOKEN, "Bearer  "],
)
def test_a_wrong_credential_is_refused(authorization):
    assert _refused_status(Authorization=authorization) == 401


def test_a_browser_origin_is_refused_with_403():
    """DNS rebinding: a page the owner visits must not be able to drive the vault.

    Spec 2025-11-25 Security Warning: servers MUST validate Origin, and MUST
    answer an invalid one with 403. This is the hole behind CVE-2026-42559.
    """
    assert _refused_status(Origin="https://evil.example") == 403


def test_even_a_loopback_origin_is_refused():
    """This server serves no page, so no Origin can be legitimate."""
    assert _refused_status(Origin="http://127.0.0.1:8765") == 403


def test_a_rebound_host_header_is_refused():
    assert _refused_status(Host="attacker.example") == 421


def test_origin_is_judged_before_the_token_so_the_refusal_names_the_reason():
    """An unauthenticated browser gets 403, not 401: the browser is the problem."""
    status = _refused_status(Authorization=None, Origin="https://evil.example")
    assert status == 403


# ---------------------------------------------------------------------------
# The surface is the same surface
# ---------------------------------------------------------------------------


async def _open_session(client) -> str | None:
    """Initialize as a client does; a stateless server issues no session id."""
    response = await client.post(ENDPOINT, headers=_headers(), json=_initialize_body())
    assert response.status_code == 200, response.text
    session = response.headers.get("mcp-session-id")
    await client.post(
        ENDPOINT,
        headers=_headers(**{"mcp-session-id": session}),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return session


async def _rpc(client, session: str | None, method: str, params=None) -> dict:
    body = {"jsonrpc": "2.0", "id": 2, "method": method}
    if params is not None:
        body["params"] = params
    response = await client.post(
        ENDPOINT, headers=_headers(**{"mcp-session-id": session}), json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _tool_envelope(name: str, arguments: dict) -> dict:
    app = _built_app()
    async with _running(app), _client(app) as client:
        session = await _open_session(client)
        payload = await _rpc(
            client, session, "tools/call", {"name": name, "arguments": arguments}
        )
    return payload["result"]["structuredContent"]


async def _listed_tool_names() -> set:
    app = _built_app()
    async with _running(app), _client(app) as client:
        session = await _open_session(client)
        payload = await _rpc(client, session, "tools/list")
    return {tool["name"] for tool in payload["result"]["tools"]}


def test_the_http_surface_lists_the_same_thirteen_tools_as_stdio():
    import mcp_server

    over_http = asyncio.run(_listed_tool_names())
    assert over_http == {tool.name for tool in mcp_server._build_tool_definitions()}
    assert len(over_http) == 13


def test_a_tool_call_over_http_returns_the_stdio_envelope(monkeypatch):
    """The transport must not become a second dispatch path.

    The same arguments go through `_execute_tool_call` - the stdio funnel -
    and through HTTP; the envelopes must agree except where they are a clock.
    """
    import mcp_server

    monkeypatch.setattr(
        mcp_server, "_wiki_overview", lambda **_kwargs: {"pages": 7, "tier": "HYBRID"}
    )
    over_http = asyncio.run(_tool_envelope("wiki_overview", {}))
    over_stdio = json.loads(
        mcp_server._execute_tool_call("wiki_overview", {}, time.monotonic() + 30.0)
    )
    assert _without_clocks(over_http) == _without_clocks(over_stdio)
    assert (over_http["data"]["pages"], over_http["data"]["tier"]) == (7, "HYBRID")


def _without_clocks(envelope: dict) -> dict:
    """The envelope minus every field that is a clock.

    `data._meta.timestamp` is stamped to the second by each call; on a slow
    Windows runner the two calls straddled a second boundary (2026-09-07,
    22:51:29 against 22:51:30) and the transport was blamed for the clock.
    """
    volatile = {"generated_at", "answer_cost"}
    kept = {key: value for key, value in envelope.items() if key not in volatile}
    meta = dict(kept.get("data", {}).get("_meta") or {})
    meta.pop("timestamp", None)
    kept["data"] = {**kept["data"], "_meta": meta}
    return kept


def test_argument_validation_is_the_same_over_http():
    """A bad argument is refused by the shared validator, not by the SDK."""
    envelope = asyncio.run(
        _tool_envelope("recall", {"query": "x", "profile": "HYBRID"})
    )
    assert envelope["data"]["error"] == "argument 'profile' requires grounded=true"


def test_an_unknown_tool_is_refused_by_the_shared_boundary():
    envelope = asyncio.run(_tool_envelope("definitely_not_a_tool", {}))
    assert "Unknown tool" in envelope["data"]["error"]


def test_the_shared_server_factory_is_what_both_transports_build():
    """`build_server` is the single construction point; HTTP does not re-register."""
    import mcp_server

    server = mcp_server.build_server()
    assert server.name == "llm-wiki"
    assert any("CallTool" in str(key) for key in server.request_handlers)


# ---------------------------------------------------------------------------
# One process for every agent (ADR 0008)
# ---------------------------------------------------------------------------


def test_the_shared_server_is_stateless():
    """No session lives in the process, so a restart is invisible to a client."""

    async def scenario():
        app = _built_app()
        async with _running(app), _client(app) as client:
            response = await client.post(
                ENDPOINT, headers=_headers(), json=_initialize_body()
            )
            assert response.status_code == 200, response.text
            assert "mcp-session-id" not in response.headers
            # A fresh client that never initialized on this process still
            # gets an answer: what a client sees right after a restart.
            return await _rpc(client, None, "tools/list")

    payload = asyncio.run(scenario())
    assert len(payload["result"]["tools"]) == 13


def test_simultaneous_calls_from_two_agents_do_not_share_state(monkeypatch):
    """Two agents, one process: each call keeps its own argument and deadline.

    The barrier proves the calls overlapped inside the tool, so a result that
    came back with the other agent's slug or deadline would be a real leak,
    not two calls that happened one after the other.
    """
    import threading

    import mcp_server

    barrier = threading.Barrier(2, timeout=10)

    def fake_read_page(slug, *, deadline=None):
        barrier.wait()
        observed = mcp_server._OPERATION_DEADLINE.get()
        return {"slug": slug, "deadline_matches": observed == deadline}

    monkeypatch.setattr(mcp_server, "_read_page", fake_read_page)

    async def call(client, slug):
        body = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "read_page", "arguments": {"slug": slug}},
        }
        response = await client.post(ENDPOINT, headers=_headers(), json=body)
        assert response.status_code == 200, response.text
        return response.json()["result"]["structuredContent"]["data"]

    async def scenario():
        app = _built_app()
        async with _running(app), _client(app) as first, _client(app) as second:
            return await asyncio.gather(call(first, "alpha"), call(second, "beta"))

    alpha, beta = asyncio.run(scenario())
    assert (alpha["slug"], beta["slug"]) == ("alpha", "beta")
    assert alpha["deadline_matches"] and beta["deadline_matches"]


def _shutdown_app(stopped: list):
    import mcp_http

    return mcp_http.build_app(
        token=TOKEN, host="127.0.0.1", port=8765, on_shutdown=lambda: stopped.append(True)
    )


async def _post_shutdown(app, **header_overrides):
    async with _running(app), _client(app) as client:
        return await client.post(
            "http://127.0.0.1:8765/admin/shutdown", headers=_headers(**header_overrides)
        )


def test_the_shutdown_route_stops_the_server_for_a_valid_token():
    stopped: list = []
    response = asyncio.run(_post_shutdown(_shutdown_app(stopped)))
    assert response.status_code == 202
    assert stopped == [True]


def test_the_shutdown_route_is_behind_the_same_guard():
    stopped: list = []
    response = asyncio.run(_post_shutdown(_shutdown_app(stopped), Authorization=None))
    assert response.status_code == 401
    assert stopped == []


def test_the_shutdown_route_is_absent_without_a_stop_callback():
    response = asyncio.run(_post_shutdown(_built_app()))
    assert response.status_code == 404


def test_a_second_server_cannot_take_the_port():
    import mcp_http

    first = mcp_http.bind_server_socket("127.0.0.1", 0)
    try:
        port = first.getsockname()[1]
        with pytest.raises(OSError) as raised:
            mcp_http.bind_server_socket("127.0.0.1", port)
        assert mcp_http._address_in_use(raised.value)
        assert mcp_http._bound_socket("127.0.0.1", port, ensure=True) is None
        with pytest.raises(mcp_http.HttpSurfaceError):
            mcp_http._bound_socket("127.0.0.1", port, ensure=False)
    finally:
        first.close()


def test_ensure_exits_zero_when_the_port_is_taken(tmp_path, monkeypatch):
    import mcp_http

    monkeypatch.setattr(mcp_http, "_warm_shared_surface", lambda: pytest.fail("warmed"))
    holder = mcp_http.bind_server_socket("127.0.0.1", 0)
    try:
        port = holder.getsockname()[1]
        code = mcp_http.serve("127.0.0.1", port, tmp_path / "token", ensure=True)
    finally:
        holder.close()
    assert code == 0


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_restart_leaves_a_stopped_server_stopped(tmp_path):
    import mcp_endpoint

    outcome = mcp_endpoint.restart_shared_server("127.0.0.1", _free_port(), tmp_path / "t")
    assert outcome == {"status": "not_running"}


@pytest.mark.parametrize(
    ("url", "shared"),
    [
        ("http://127.0.0.1:8765/mcp", True),
        ("http://[::1]:9000/mcp", True),
        ("http://localhost:8765/mcp", False),
        ("http://127.0.0.1:8765/mcp/", False),
        ("http://127.0.0.1:/mcp", False),
        ("https://example.com/mcp", False),
        (None, False),
    ],
)
def test_the_shared_url_is_literal_loopback_only(url, shared):
    import mcp_endpoint

    assert mcp_endpoint.is_shared_url(url) is shared


def test_no_client_entry_holds_the_token():
    import mcp_endpoint

    entries = mcp_endpoint.client_entries()
    assert set(entries) == {"claude", "codex", "opencode"}
    assert all(mcp_endpoint.is_shared_url(entry["url"]) for entry in entries.values())
    assert mcp_endpoint.TOKEN_ENV in json.dumps(entries)
    assert entries["codex"]["bearer_token_env_var"] == mcp_endpoint.TOKEN_ENV


@pytest.mark.parametrize(
    ("raw", "slots"),
    [(None, 8), ("", 8), ("12", 12), ("0", 1), ("999", 32), ("many", 8)],
)
def test_the_shared_server_has_its_own_bounded_slot_count(monkeypatch, raw, slots):
    import mcp_http

    if raw is None:
        monkeypatch.delenv(mcp_http.WORKER_SLOTS_ENV, raising=False)
    else:
        monkeypatch.setenv(mcp_http.WORKER_SLOTS_ENV, raw)
    assert mcp_http.shared_worker_slots() == slots
