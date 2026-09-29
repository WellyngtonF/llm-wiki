"""Give back what a test made read-only on purpose, so the tree can be deleted.

The archive grants its BagIt packages owner-read-only ACLs and the caches are narrowed
to their owner. pytest's cleanup clears the read-only attribute, not an ACL, so such a
tree became an undeletable `garbage-*` directory in `%TEMP%`, and Windows walks every file
in `%TEMP%` at logon. See
`docs/research/2026-09-29-a-test-tree-can-always-be-deleted.md`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Resetting a whole pytest root is one icacls walk; generous, because a root that
# already holds old garbage is walked once more before pytest deletes it.
RELEASE_TIMEOUT_SECONDS = 900


def release_test_tree(tree: Path) -> None:
    """Reset every ACL under `tree` to the one it inherits. Never raises."""
    if os.name != "nt" or not tree.exists():
        return
    try:
        subprocess.run(
            ["icacls", str(tree), "/reset", "/T", "/C", "/Q"],
            capture_output=True,
            check=False,
            timeout=RELEASE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return
