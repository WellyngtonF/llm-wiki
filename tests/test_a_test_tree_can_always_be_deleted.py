"""What a test made read-only on purpose can still be deleted after the session.

The archive makes a BagIt package immutable with an owner-read-only ACL, and caches are
narrowed to their owner. pytest's cleanup clears the read-only attribute but not an ACL,
so every such tree became a `garbage-*` directory under `%TEMP%\\pytest-of-<user>`: 49 of
them, about 900 000 files, on the owner's machine by 2026-09-29. Windows walks every file
in `%TEMP%` at logon, which took the owner's logon from 20 s to 17 minutes. Research:
`docs/research/2026-09-29-a-test-tree-can-always-be-deleted.md`.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from tests.deletable_trees import release_test_tree  # noqa: E402

windows_only = pytest.mark.skipif(os.name != "nt", reason="the ACLs are the Windows path")


@windows_only
def test_a_tree_the_archive_made_read_only_is_deleted_after_release(tmp_path):
    from archive_daily import DailyArchiver

    package = tmp_path / "tree" / "bag"
    package.mkdir(parents=True)
    (package / "bagit.txt").write_text("BagIt-Version: 1.0\n", encoding="utf-8")
    for path in (package / "bagit.txt", package):
        DailyArchiver._windows_read_only_acl(path)
    with pytest.raises(PermissionError):
        shutil.rmtree(tmp_path / "tree")

    release_test_tree(tmp_path / "tree")

    shutil.rmtree(tmp_path / "tree")
    assert not (tmp_path / "tree").exists()


def test_releasing_a_missing_tree_is_not_an_error(tmp_path):
    release_test_tree(tmp_path / "absent")


def test_the_suite_keeps_only_the_trees_of_failed_tests():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'tmp_path_retention_policy = "failed"' in pyproject
