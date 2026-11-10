# LLM Wiki

A local, file-based memory shared by coding agents. Sessions are captured, compiled into durable notes, and read by agents and by the owner in Obsidian.

## Language

### Knowledge

**Note**:
A durable Markdown page under `knowledge/notes/` holding one reusable piece of knowledge.
_Avoid_: page, wiki page, memory

**Daily log**:
The append-only file of one day's captured session summaries, which the compile turns into notes.
_Avoid_: daily, journal

**Session record**:
The redacted verbatim copy of one captured session, kept whatever the session's tier.
_Avoid_: transcript, raw session

**Compile**:
The pass that reads pending daily logs and creates or updates notes.
_Avoid_: ingest, consolidation

**Claims ledger**:
The machine-readable block at the end of a note that lists the note's extracted claims.
_Avoid_: claims JSON, claim block

### Organisation

**Project**:
A product the owner has registered, which may span several repositories; work outside a registered repository belongs to no project.
_Avoid_: repo, workspace, folder

**Repository**:
One git repository, identified by its main checkout; its worktrees and subfolders belong to it.
_Avoid_: project, checkout, worktree

**Project map**:
The owner's private list of projects and the repositories each one is made of.
_Avoid_: alias map, project config

**Project page**:
The generated page a person reads for one project: its notes grouped by type and the work state of each of its repositories.
_Avoid_: hub, MOC, project index

**Work state**:
What agents were last doing in one repository, derived from lifecycle events, never narrated by a model.
_Avoid_: project state, handoff

**Module**:
A named area inside a repository, expressed as a tag on notes, never as a project.
_Avoid_: subproject, component

**Link**:
A bare `[[slug]]` wikilink from one note to another existing note.
_Avoid_: path link, related page

**Backlink**:
The reverse of a link, derived by the reader (Obsidian), not written into the note.
_Avoid_: links-to-this-page line
