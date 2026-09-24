"""Codex runs `commandWindows` through PowerShell, so the template speaks PowerShell.

Observed on Windows with codex-cli 0.156.1 on 2026-09-24: every hook ran as
`powershell.exe -NoProfile -Command "<commandWindows>"`. PowerShell does not
expand `%LLM_WIKI_ROOT%`, so `uv run --directory "%LLM_WIKI_ROOT%"` failed with
"file not found" and every Codex hook was silently lost until the owner hand-wrote
absolute paths into `~/.codex/hooks.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _windows_commands() -> list[str]:
    template = json.loads((ROOT / "integrations/codex/hooks.json").read_text(encoding="utf-8"))
    return [
        hook["commandWindows"]
        for blocks in template["hooks"].values()
        for block in blocks
        for hook in block["hooks"]
    ]


def test_every_windows_command_reads_the_root_the_powershell_way() -> None:
    commands = _windows_commands()

    assert commands
    assert [command for command in commands if "%" in command] == []
    assert all(command.count("$env:LLM_WIKI_ROOT") == 2 for command in commands)
