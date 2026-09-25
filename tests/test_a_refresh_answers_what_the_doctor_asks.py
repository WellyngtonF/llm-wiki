"""The doctor asks for a refresh only when the nightly refresh would build one.

Measured on a live vault on 2026-09-24. The doctor reported "Evidence generation
requires refresh" all day while every nightly refresh answered `current`: the
checkout had committed since the active generation was built, and the doctor read
the moved `git_commit` (`repository_scope: superseded`) as staleness, which the
refresh deliberately does not (NEW-138: the commit is provenance, not identity).
A refresh that cannot change anything was the repair the doctor recommended, so
the finding never cleared. A generation older than a day with nothing new to
index was the same contradiction.

The same nightly logged `PENDING: ...: registered but never activated` for the
vault's own code generations. Those are never activated by design and repository
retention owns them; they are named as code generations, not as a publication
waiting on an activation that will never come.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"
for directory in (SCRIPTS, TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_repository_index import ALPHA, _git, _repository, vault  # noqa: E402,F401

from tests.slow_machine import LONG_TIMEOUT  # noqa: E402

TWO_DAYS_SECONDS = 2 * 24 * 60 * 60


def _refresh(root: Path, state: Path) -> dict:
    """The entry point the nightly's generation step calls."""
    import doctor

    return doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=LONG_TIMEOUT, max_sources=100
    )


def _doctor(root: Path, state: Path) -> dict:
    import doctor

    return doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + LONG_TIMEOUT,
        max_sources=100,
    )


def _memory_vault(root: Path) -> None:
    _repository(
        root,
        {
            "knowledge/notes/alpha.md": "---\ntype: concept\n---\n# Alpha\n\nA fact.\n",
            "scripts/alpha.py": ALPHA,
        },
    )


def _commit_code(root: Path, name: str) -> None:
    (root / "scripts" / f"{name}.py").write_text(f"def {name}():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", name)


def _age_active(state: Path, generation_id: str, seconds: int) -> None:
    manifest = state / "cache" / "evidence-graph" / "generations" / generation_id / "manifest.json"
    stamp = time.time() - seconds
    os.utime(manifest, (stamp, stamp))


def test_a_commit_that_moved_no_memory_leaves_the_generation_healthy(vault):  # noqa: F811
    root, state = vault
    _memory_vault(root)
    built = _refresh(root, state)
    _commit_code(root, "beta")

    refreshed = _refresh(root, state)
    report = _doctor(root, state)

    assert (built["status"], refreshed["status"]) == ("built", "current")
    assert (report["status"], report["message"]) == ("ok", "Evidence generation is healthy.")
    assert (report["details"]["repository_scope"], report["details"]["freshness"]) == (
        "superseded",
        "fresh",
    )


def test_a_generation_with_nothing_new_to_index_is_not_stale_by_age(vault):  # noqa: F811
    root, state = vault
    _memory_vault(root)
    built = _refresh(root, state)
    _age_active(state, built["generation_id"], TWO_DAYS_SECONDS)

    refreshed = _refresh(root, state)
    report = _doctor(root, state)

    assert refreshed["status"] == "current"
    assert report["status"] == "ok"
    assert report["details"]["age_seconds"] >= TWO_DAYS_SECONDS


def test_a_stale_generation_is_reported_and_the_refresh_clears_it(vault):  # noqa: F811
    root, state = vault
    _memory_vault(root)
    _refresh(root, state)
    (root / "knowledge/notes/beta.md").write_text(
        "---\ntype: concept\n---\n# Beta\n\nAnother fact.\n", encoding="utf-8"
    )

    before = _doctor(root, state)
    refreshed = _refresh(root, state)
    after = _doctor(root, state)

    assert (before["status"], before["details"]["unindexed_delta"]) == ("degraded", 1)
    assert refreshed["status"] == "built"
    assert (after["status"], after["details"]["active_generation"]) == (
        "ok",
        refreshed["generation_id"],
    )


def _nightly_prune(root: Path, state: Path) -> str:
    """The nightly's prune step, run the way the nightly runs it."""
    environment = {**os.environ, "LLM_WIKI_ROOT": str(root), "LLM_WIKI_STATE_ROOT": str(state)}
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "prune_generations.py"), "--apply"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=LONG_TIMEOUT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_the_nightly_prune_names_a_code_generation_and_not_a_pending_publication(vault):  # noqa: F811
    import repository_index

    root, state = vault
    _memory_vault(root)
    code = repository_index.index_repository(root, roots=["scripts"], state_root=state)["generation_id"]
    memory = _refresh(root, state)["generation_id"]

    output = _nightly_prune(root, state)

    assert f"keeping {memory}" in output
    assert "PENDING" not in output
    assert f"CODE: {code}: a repository code generation; repository retention retires it" in output
    assert output.strip().splitlines()[-1] == (
        "prune_generations: 0 failed, 0 unpaired, 0 pending activation, 1 code generation(s)"
    )


def test_a_publication_that_was_never_activated_is_still_pending(vault):  # noqa: F811
    import generation_catalog
    from test_prune_generations import _publish

    root, state = vault
    _memory_vault(root)
    memory = _refresh(root, state)["generation_id"]
    catalog = generation_catalog.GenerationCatalog(state)
    _publish(catalog, "gen-flight", parent=memory)
    catalog.register("gen-flight")

    output = _nightly_prune(root, state)

    assert "PENDING: gen-flight: registered but never activated" in output
    assert "CODE:" not in output
    assert output.strip().splitlines()[-1].endswith("1 pending activation, 0 code generation(s)")
