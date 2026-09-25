"""Regression tests: repository folder names, collision resolution, strict ownership.

Covers the Round 2 / Round 5 fixes to `session_start_project_state.py`, which since
Stage 2 (ADR 0002) name a registered repository's folder inside its project's folder:
  - Base slug sanitization (Cyrillic preservation, hyphens, edge cases).
  - Collision resolution within one project: base → parent-of-parent → git
    owner-repo → grandparent → path-hash.
  - Strict ownership: state.md without a `- Project root:` line is NOT owned.
  - Idempotency: re-compute returns the same folder.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from session_start_project_state import (
    _base_slug,
    _git_remote_slug,
    _path_hash_suffix,
    _render_new_state,
    _slug_owns_dir,
    repository_folder,
)
from work_state import Placement

# ---------- _base_slug ----------

def test_base_slug_lowercase():
    p = Path("/tmp/My-Project")
    assert _base_slug(p) == "my-project"


def test_base_slug_preserves_cyrillic():
    p = Path("/tmp/Тесты")
    assert _base_slug(p) == "тесты"


def test_base_slug_strips_unsafe_chars():
    # Any Path with weird basename characters
    class P:
        name = "foo:bar*baz"
    assert _base_slug(P()) == "foo-bar-baz"  # type: ignore[arg-type]


def test_base_slug_fallback_for_empty():
    class P:
        name = ""
    assert _base_slug(P()) == "root"  # type: ignore[arg-type]


def test_base_slug_fallback_for_dotdot():
    class P:
        name = ".."
    assert _base_slug(P()) == "root"  # type: ignore[arg-type]


# ---------- _git_remote_slug ----------

def test_git_remote_slug_parses_ssh(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:Owner/Repo.git\n',
        encoding="utf-8",
    )
    assert _git_remote_slug(tmp_path) == "owner-repo"


def test_git_remote_slug_parses_https(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/Alice/my-app\n',
        encoding="utf-8",
    )
    assert _git_remote_slug(tmp_path) == "alice-my-app"


def test_git_remote_slug_none_when_no_git(tmp_path: Path):
    assert _git_remote_slug(tmp_path) is None


def test_git_remote_slug_none_when_no_origin(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    assert _git_remote_slug(tmp_path) is None


# ---------- _path_hash_suffix ----------

def test_path_hash_deterministic(tmp_path: Path):
    assert _path_hash_suffix(tmp_path) == _path_hash_suffix(tmp_path)


def test_path_hash_length():
    assert len(_path_hash_suffix(Path("/any"))) == 6


# ---------- _slug_owns_dir (strict ownership, Round 5 #3) ----------

def test_slug_owns_empty_dir(tmp_path: Path):
    """Unused slug → free to take."""
    projects = tmp_path / "vault" / "knowledge" / "projects"
    projects.mkdir(parents=True)
    assert _slug_owns_dir("unused", Path("/any/project"), projects) is True


def test_slug_owns_matching_root(tmp_path: Path):
    projects = tmp_path / "vault" / "knowledge" / "projects"
    slug_dir = projects / "mine"
    slug_dir.mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    (slug_dir / "state.md").write_text(
        f"# mine — State\n- Project root: `{project}`\n", encoding="utf-8"
    )
    assert _slug_owns_dir("mine", project, projects) is True


def test_slug_owns_rejects_different_root(tmp_path: Path):
    projects = tmp_path / "vault" / "knowledge" / "projects"
    slug_dir = projects / "shared"
    slug_dir.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    mine = tmp_path / "mine"
    mine.mkdir()
    (slug_dir / "state.md").write_text(
        f"# shared — State\n- Project root: `{other}`\n", encoding="utf-8"
    )
    assert _slug_owns_dir("shared", mine, projects) is False


def test_slug_owns_strict_rejects_missing_source(tmp_path: Path):
    """Round 5 #3: state.md without `- Project root:` → NOT ours.

    Previously this returned True (assumed hand-edited, treat as ours),
    opening a collision hole where a second project could silently adopt
    a first project's state.md by having its Source section removed.
    """
    projects = tmp_path / "vault" / "knowledge" / "projects"
    slug_dir = projects / "ambiguous"
    slug_dir.mkdir(parents=True)
    (slug_dir / "state.md").write_text(
        "# ambiguous — State\n(no Source section whatsoever)\n",
        encoding="utf-8",
    )
    assert _slug_owns_dir("ambiguous", tmp_path / "someproj", projects) is False


# ---------- repository_folder end-to-end ----------

def _projects(tmp_path: Path) -> Path:
    projects = tmp_path / "vault" / "knowledge" / "projects"
    (projects / "product-a").mkdir(parents=True)
    return projects


def _folder(repository: Path, projects: Path) -> str:
    """The repository's folder inside project `product-a`."""
    return repository_folder(repository, projects / "product-a", projects)


def _claim(projects: Path, folder: str, root: Path | str) -> None:
    """What the first checkpoint leaves: a state.md recording its Project root."""
    (projects / "product-a" / folder).mkdir()
    (projects / "product-a" / folder / "state.md").write_text(
        f"# {folder}\n- Project root: `{root}`\n", encoding="utf-8"
    )


def test_repository_folder_unique(tmp_path: Path):
    """Clean folder name — base strategy wins."""
    projects = _projects(tmp_path)
    proj = tmp_path / "unique"
    proj.mkdir()
    assert _folder(proj, projects) == "unique"


def test_repository_folder_collision_gets_parent_of_parent(tmp_path: Path):
    """Two repositories of one project with the same basename → second gets pop suffix."""
    projects = _projects(tmp_path)

    front_a = tmp_path / "app-a" / "frontend"
    front_a.mkdir(parents=True)
    assert _folder(front_a, projects) == "frontend"
    _claim(projects, "frontend", front_a)

    front_b = tmp_path / "app-b" / "frontend"
    front_b.mkdir(parents=True)
    assert _folder(front_b, projects) == "frontend-app-b"


def test_a_collision_in_another_project_is_no_collision(tmp_path: Path):
    """Collisions are scoped within the project: another project's folder does not count."""
    projects = _projects(tmp_path)
    other = projects / "product-b" / "frontend"
    other.mkdir(parents=True)
    (other / "state.md").write_text("# frontend\n- Project root: `/elsewhere`\n", encoding="utf-8")
    front = tmp_path / "app" / "frontend"
    front.mkdir(parents=True)

    assert _folder(front, projects) == "frontend"


def test_repository_folder_idempotent(tmp_path: Path):
    """Re-computing for the same repository returns the same folder."""
    projects = _projects(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    first = _folder(proj, projects)
    _claim(projects, first, proj)
    assert _folder(proj, projects) == first


def test_rendered_template_has_no_placeholders_and_preserves_folder_ownership(tmp_path: Path):
    projects = _projects(tmp_path)
    project = tmp_path / "My Project"
    project.mkdir()
    template = Path(__file__).resolve().parent.parent / "knowledge/projects/_template/state.md"

    rendered = _render_new_state(template, Placement("product-a", project, "my-project"), project)
    state_dir = projects / "product-a" / "my-project"
    state_dir.mkdir()
    (state_dir / "state.md").write_text(rendered, encoding="utf-8")

    assert "<project" not in rendered and "<repository>" not in rendered
    # Quoted, so a name that redaction left looking like `[redacted-api-key]`
    # still reads back as a string rather than a YAML list.
    assert 'project: "product-a"' in rendered
    frontmatter = yaml.safe_load(rendered.split("---")[1])
    assert frontmatter["project"] == "product-a"
    assert frontmatter["repository"] == "my-project"
    assert "# product-a/my-project - State" in rendered
    assert f"- Project root: `{project}`" in rendered
    assert _folder(project, projects) == "my-project"


def test_repository_folder_hash_suffix_last_resort(tmp_path: Path):
    """If base, pop, git, and grandparent all collide, hash suffix kicks in."""
    projects = _projects(tmp_path)
    proj = tmp_path / "orphan"
    proj.mkdir()
    _claim(projects, "orphan", "/somewhere/else")

    # With no parent-of-parent matching, it should still resolve — either
    # via grandparent (tmp_path.name) or via hash. The output must NOT be
    # bare "orphan" (that's taken).
    folder = _folder(proj, projects)
    assert folder != "orphan"
    assert folder.startswith("orphan") or folder == "root"
