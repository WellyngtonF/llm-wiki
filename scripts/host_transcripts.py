"""Where the hosts keep their session transcripts.

Both hosts let the user move their directory — `CLAUDE_CONFIG_DIR` for Claude
Code, `CODEX_HOME` for Codex — and a hook inherits the host's environment, so the
variable names the directory this very host writes to. The defaults stay on the
list because the scheduler's environment is not the host's. See
`docs/research/2026-09-17-a-moved-host-directory-is-still-the-host.md`.

Antigravity keeps a conversation under `<gemini dir>/<product>/brain/<id>/`, and
its gemini directory is `~/.gemini`, the one `GEMINI_HOME` names, or one per T3
Code provider instance under `~/.t3/userdata/providers/antigravity/` (ADR 0010).
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# (environment variable, default directory under home, where transcripts live inside it)
_HOSTS = (
    ("CLAUDE_CONFIG_DIR", ".claude", "projects"),
    ("CODEX_HOME", ".codex", "sessions"),
)
ANTIGRAVITY_PRODUCTS = ("antigravity", "antigravity-cli", "antigravity-ide", "antigravity-acp")
T3_ANTIGRAVITY_PROVIDERS = Path(".t3") / "userdata" / "providers" / "antigravity"


def _configured(environ: Mapping[str, str], variable: str) -> list[Path]:
    value = environ.get(variable, "").strip()
    return [Path(value).expanduser()] if value else []


def t3_antigravity_dirs(home: Path) -> list[Path]:
    """Each T3 Code Antigravity provider instance's gemini directory, sorted."""
    providers = home / T3_ANTIGRAVITY_PROVIDERS
    try:
        found = [entry for entry in providers.iterdir() if entry.is_dir()]
    except OSError:
        return []
    return sorted(entry for entry in found if not entry.is_symlink())


def antigravity_gemini_dirs(environ: Mapping[str, str], home: Path) -> list[Path]:
    """`~/.gemini`, the one `GEMINI_HOME` names, then T3 Code's, without repeats."""
    dirs = [home / ".gemini", *_configured(environ, "GEMINI_HOME"), *t3_antigravity_dirs(home)]
    return list(dict.fromkeys(dirs))


def host_transcript_roots(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> tuple[Path, ...]:
    """The default transcript directories, then the configured ones, without repeats."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    roots: list[Path] = []
    for variable, directory, inner in _HOSTS:
        bases = [home / directory, *_configured(environ, variable)]
        roots.extend(base / inner for base in bases)
    for gemini in antigravity_gemini_dirs(environ, home):
        roots.extend(gemini / product / "brain" for product in ANTIGRAVITY_PRODUCTS)
    return tuple(dict.fromkeys(roots))
