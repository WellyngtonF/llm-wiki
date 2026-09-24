"""The Claude settings fragment owns every environment key it writes.

`claude_settings_resource` writes the whole provider environment into the env
block, but the fragment was read back through four keys. With
`MEMORY_CODEX_MODEL` set the written fragment never read as itself: the install
failed its own verification, the rollback saw drift, and the live vault's install
was quarantined on 2026-09-24.
"""

from __future__ import annotations

import json
from pathlib import Path

from integration_hook_config import (
    CLAUDE_ENV_KEYS,
    PROVIDER_ENV_KEYS,
    claude_settings_resource,
    claude_settings_template,
)

ROOT = Path(__file__).resolve().parents[1]


def test_every_provider_key_is_owned() -> None:
    assert set(PROVIDER_ENV_KEYS) <= set(CLAUDE_ENV_KEYS)


def test_a_written_fragment_reads_back_as_itself(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "codex")
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "gpt-6-luna")
    monkeypatch.setenv("MEMORY_CODEX_REASONING", "max")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"UNRELATED": "kept"}}), encoding="utf-8")
    resource = claude_settings_resource(
        settings, claude_settings_template(ROOT), ROOT, ROOT, config_existed=True
    )

    resource.write_owned(resource.desired)

    written = json.loads(settings.read_text(encoding="utf-8"))
    assert resource.read_owned() == resource.desired
    assert (written["env"]["UNRELATED"], written["env"]["MEMORY_CODEX_MODEL"]) == ("kept", "gpt-6-luna")
