# knowledge/projects/

Work state for the projects you registered. A project exists only when you register
it, by asking an agent ("I'm starting project X here, add it to the memory"); a
directory that belongs to no registered repository gets nothing here. Structure:

```
knowledge/projects/
  project-map.md                   ← your projects and the repositories each is made of
  _template/state.md               ← skeleton for a repository's first state page
  <project>/                       ← one folder per registered project
    index.md                       ← generated project page (reserved)
    <repository>/journal.md        ← append-only record of what agents did there
    <repository>/state.md          ← generated from the journal; do not edit
```

## Repository folder rule
A repository is its main checkout: a subfolder or a worktree resolves to it. Its
folder inside the project is, in priority order (implemented in
`scripts/session_start_project_state.py::repository_folder`):

1. **Base**: the main checkout's folder name, lowercase, hyphens.
2. **On collision**: append parent-of-parent (e.g. `backend` + `your-app` → `backend-your-app`).
3. **On further collision**: `owner-repo` parsed from `.git/config` origin remote.
4. **On further collision**: append grandparent folder name.
5. **Last resort**: 6-char path-hash suffix — guaranteed unique.

Collisions are resolved among the repositories of the same project. Ownership is
determined by strict match of `- Project root:` in the existing `state.md`.

## Registering, moving and removing
Ask an agent, which calls the `manage_project` tool, or edit `project-map.md` by
hand. Attaching a repository to another project moves its folder, renaming a project
moves the project's folder, and detaching a repository or removing a project deletes
its work state. Each change is one transaction you can undo for two days; notes in
`knowledge/notes/` are never touched.

## What belongs here vs elsewhere
- **Here (`knowledge/projects/<project>/`):** what agents were last doing in each of the project's repositories.
- **In `knowledge/notes/`:** durable knowledge, with the project in its frontmatter.
- **In `knowledge/daily/`:** raw session captures, tagged `<project>/<repository>` for registered work.

## Conventions
- `state.md` stays ≤ 1 screen. Split into sibling pages when it grows.
- `Source:` line records the project root path (and git remote if any).
- `## Editorial note` footer marks the page as vault metadata.

