"""Where the shared MCP server listens, and how to stop or restart it.

Standard library only, so the nightly pass and the installer checks can use it
without importing the server, its SDK or a model. `mcp_http` serves; this
module only names the endpoint and talks to it. ADR 0008.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MCP_ENDPOINT_PATH = "/mcp"
SHUTDOWN_PATH = "/admin/shutdown"
# The clients read the bearer token from this user environment variable, so no
# agent configuration file holds the secret.
TOKEN_ENV = "LLMWIKI_MCP_TOKEN"
# How each client spells "the bearer token from TOKEN_ENV" in its own config.
CLAUDE_AUTHORIZATION = f"Bearer ${{{TOKEN_ENV}}}"
OPENCODE_AUTHORIZATION = f"Bearer {{env:{TOKEN_ENV}}}"
TASK_NAME = "LLMWiki-McpHttp"
# A stopping server finishes the calls it has, then settles inference for up to
# `mcp_server.SHUTDOWN_INFERENCE_SECONDS` (30 s). The port closes before that.
STOP_WAIT_SECONDS = 60.0
REQUEST_SECONDS = 10.0


def endpoint_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}{MCP_ENDPOINT_PATH}"


def is_shared_url(url: object) -> bool:
    """True for the shared server's URL on a literal loopback address."""
    if not isinstance(url, str):
        return False
    for host in ("127.0.0.1", "[::1]"):
        prefix = f"http://{host}:"
        if url.startswith(prefix) and url.endswith(MCP_ENDPOINT_PATH):
            port = url[len(prefix):-len(MCP_ENDPOINT_PATH)]
            return port.isdigit() and 0 < int(port) < 65536
    return False


def default_token_path() -> Path:
    """The token lives with the rest of the runtime state, under `run/`."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from memory_state import STATE_DIR

    return Path(STATE_DIR) / "mcp-http" / "token"


def client_entries(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict:
    """The `llm-wiki` entry each client registers to reach the shared server."""
    url = endpoint_url(host, port)
    return {
        "claude": {
            "type": "http",
            "url": url,
            "headers": {"Authorization": CLAUDE_AUTHORIZATION},
        },
        "codex": {"url": url, "bearer_token_env_var": TOKEN_ENV},
        "opencode": {
            "type": "remote",
            "url": url,
            "headers": {"Authorization": OPENCODE_AUTHORIZATION},
            "enabled": True,
        },
    }


def _read_token(token_path: Path) -> str | None:
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def request_shutdown(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token_path: Path | None = None,
) -> str:
    """Ask a running server to stop cleanly: `stopping`, `not_running` or a refusal."""
    path = token_path if token_path is not None else default_token_path()
    token = _read_token(Path(path))
    if token is None:
        return "not_running" if not is_listening(host, port) else "no_token"
    shown = f"[{host}]" if ":" in host else host
    request = urllib.request.Request(
        f"http://{shown}:{port}{SHUTDOWN_PATH}",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_SECONDS) as response:
            return "stopping" if response.status == 202 else f"refused_{response.status}"
    except urllib.error.HTTPError as error:
        return f"refused_{error.code}"
    except (urllib.error.URLError, OSError):
        return "not_running" if not is_listening(host, port) else "unreachable"


def is_listening(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


def wait_until_stopped(host: str, port: int, seconds: float = STOP_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_listening(host, port):
            return True
        time.sleep(0.5)
    return not is_listening(host, port)


def _run_scheduled_task() -> bool:
    """Start the logon task, so the new server runs where the old one did."""
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["schtasks", "/Run", "/TN", TASK_NAME],
            capture_output=True,
            timeout=REQUEST_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _detached_flags() -> int:
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    return flags


def _spawn_detached(host: str, port: int, log_path: Path) -> bool:
    """Start the server outside the caller's process tree when no task exists."""
    script = Path(__file__).resolve().parent / "mcp_http.py"
    command = [sys.executable, str(script), "--ensure", "--host", host, "--port", str(port)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        options = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
        if os.name == "nt":
            breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            try:
                subprocess.Popen(command, creationflags=_detached_flags() | breakaway, **options)
                return True
            except OSError:
                # A job that forbids breakaway keeps the child; it still serves.
                subprocess.Popen(command, creationflags=_detached_flags(), **options)
                return True
        subprocess.Popen(command, start_new_session=True, **options)
        return True


def server_log_path() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from memory_state import STATE_ROOT

    return Path(STATE_ROOT) / "logs" / "mcp-http.log"


def start_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    if _run_scheduled_task():
        return "task"
    _spawn_detached(host, port, server_log_path())
    return "detached"


def restart_shared_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token_path: Path | None = None,
) -> dict:
    """Bring a running shared server onto the code now on disk.

    A server that is not running stays stopped: only a running one is restarted,
    so an operator who never opted in never gets one. Stateless transport means
    clients keep their configuration and reconnect on their next call.
    """
    stopped = request_shutdown(host, port, token_path)
    if stopped == "not_running":
        return {"status": "not_running"}
    if stopped != "stopping":
        return {"status": "not_stopped", "reason": stopped}
    if not wait_until_stopped(host, port):
        return {"status": "not_stopped", "reason": "stop_timeout"}
    return {"status": "restarted", "via": start_server(host, port)}
