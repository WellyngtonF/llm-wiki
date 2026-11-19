#!/usr/bin/env python3
"""Find Antigravity installations and give each one the vault's MCP server (ADR 0010).

An installation is a gemini directory: `~/.gemini` when an Antigravity product
directory sits inside it, and every T3 Code provider instance under
`~/.t3/userdata/providers/antigravity/`. Antigravity reads MCP servers from
`<gemini dir>/config/mcp_config.json` and hooks from `<gemini dir>/config/hooks.json`.

The hooks are owned by the install control plane (`antigravity_hooks_resource`);
the MCP entry is written here, once, like the other hosts' MCP entries, and an
uninstall names it instead of taking it back (`unowned_agent_registrations`).
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SERVER_NAME = "llm-wiki"


def installations(home: Path) -> list[Path]:
    """Each Antigravity gemini directory under `home`, standalone first."""
    from host_transcripts import ANTIGRAVITY_PRODUCTS, t3_antigravity_dirs

    gemini = Path(home) / ".gemini"
    standalone = [gemini] if any((gemini / name).is_dir() for name in ANTIGRAVITY_PRODUCTS) else []
    return [*standalone, *t3_antigravity_dirs(Path(home))]


def server_args(root: Path) -> list[str]:
    return [
        "run",
        "--locked",
        "--no-sync",
        "--directory",
        str(root),
        "python",
        "scripts/mcp_server.py",
    ]


def expected_entry(root: Path, uv_path: Path) -> dict[str, Any]:
    """An absolute `uv`: Antigravity started from the Dock has no login-shell PATH."""
    return {"command": str(uv_path), "args": server_args(root)}


def entry_state(entry: object, root: Path) -> str:
    """`current` when the entry starts this vault's server through some `uv`."""
    if entry is None:
        return "missing"
    if not isinstance(entry, Mapping):
        return "elsewhere"
    command = entry.get("command")
    is_uv = isinstance(command, str) and Path(command).name in {"uv", "uv.exe"}
    if is_uv and entry.get("args") == server_args(root):
        return "current"
    return "elsewhere"


def _read_document(path: Path) -> dict[str, Any]:
    from integration_hook_config import MAX_CONFIG_BYTES

    if path.is_symlink():
        raise ValueError("Antigravity MCP config must not be a symlink")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("Antigravity MCP config is not a bounded regular file")
    text = path.read_text(encoding="utf-8")
    value = json.loads(text) if text.strip() else {}
    if not isinstance(value, dict):
        raise ValueError("Antigravity MCP config root must be an object")
    return value


def _servers(document: dict[str, Any]) -> dict[str, Any]:
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Antigravity mcpServers must be an object")
    return servers


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def mcp_config_path(gemini: Path) -> Path:
    return Path(gemini) / "config" / "mcp_config.json"


def mcp_state(gemini: Path, root: Path) -> str:
    """`current`, `missing`, `elsewhere`, or `invalid` for one installation."""
    try:
        document = _read_document(mcp_config_path(gemini))
        return entry_state(_servers(document).get(SERVER_NAME), root)
    except (OSError, ValueError):
        return "invalid"


def configure_mcp(gemini: Path, root: Path, uv_path: Path) -> str:
    """Add the vault's entry when there is none; never replace someone else's."""
    path = mcp_config_path(gemini)
    try:
        document = _read_document(path)
        servers = _servers(document)
    except (OSError, ValueError):
        return "invalid"
    state = entry_state(servers.get(SERVER_NAME), root)
    if state != "missing":
        return state
    servers[SERVER_NAME] = expected_entry(root, uv_path)
    text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _atomic_write(path, text.encode("utf-8"))
    return "current"


def configure_all(home: Path, root: Path, uv_path: Path) -> dict[str, object]:
    found = installations(home)
    results = [
        {"path": str(gemini), "mcp": configure_mcp(gemini, root, uv_path)} for gemini in found
    ]
    if not results:
        status = "not_detected"
    elif all(item["mcp"] == "current" for item in results):
        status = "active"
    else:
        status = "conflict"
    return {"status": status, "installations": results}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect = subparsers.add_parser("detect")
    detect.add_argument("--home", type=Path, required=True)
    mcp = subparsers.add_parser("mcp")
    mcp.add_argument("--home", type=Path, required=True)
    mcp.add_argument("--root", type=Path, required=True)
    mcp.add_argument("--uv-path", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "detect":
        print(json.dumps({"installations": [str(path) for path in installations(args.home)]}))
        return 0
    result = configure_all(args.home.resolve(), args.root.resolve(), args.uv_path.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
