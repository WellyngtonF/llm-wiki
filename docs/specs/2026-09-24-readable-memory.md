# Readable memory: healthy compile, working links, real projects, better notes

Status: ready for implementation, one stage at a time, in the order 0 → 1 → 2 → 3 → 5.
Vocabulary: `CONTEXT.md`. Decisions: `docs/adr/0001` (the fork diverges from upstream),
`docs/adr/0002` (a project is a registered product made of repositories),
`docs/adr/0003` (Obsidian is the human reading surface).

This repository is public. Every example here is generic on purpose; nothing from the
owner's real memory belongs in this document or in the tests that implement it.

## Problem Statement

The owner reads the memory in Obsidian, with the vault rooted at `knowledge/`, and the
agents read it through MCP. Today neither reader is well served:

- The nightly maintenance fails on every run, and no compile has succeeded since a day's
  daily log grew past the compile's input budget. Pending knowledge waits unread.
- Notes carry information nobody needs later: test counts, pull-request numbers, task
  status, one-off machine setup. Near-duplicate notes accumulate. The compile model
  writes blind: it sees only the file names of existing notes, never their titles,
  summaries or bodies, so it cannot tell that a topic is already covered.
- About half the links in the vault do not open in Obsidian. Most are written backlink
  lines using a repository-rooted path that Obsidian cannot resolve. The links the model
  chooses are never checked against notes that exist, so some point nowhere and others
  join unrelated notes that happened to be compiled together. Many notes have no links.
- A note is mostly machine data: the claims ledger is about three quarters of the bytes,
  and the same fact appears half a dozen times per note.
- Projects are noise. Every directory an agent `cd`s into becomes a project, so most
  project folders are named after subfolders, worktrees or scratch directories. No work
  state holds anything useful, and no page shows the owner what the memory knows about
  one product.

## Solution

Five stages, each shippable and observable on its own:

- **Stage 0, health.** The nightly and the compile succeed again. Optional features that
  are absent are skipped, not failed. The compile budget follows the configured model,
  and one oversized piece of a daily log no longer blocks every other day. Health
  reporting stops counting harmless misses as lost captures.
- **Stage 1, links for Obsidian.** Every link is a bare `[[slug]]` to a note that
  exists. No backlink lines are written; Obsidian derives backlinks. Existing links are
  migrated once. A shipped CSS snippet collapses the claims ledger in Obsidian.
- **Stage 2, real projects.** A project exists only when the owner registers it, by
  asking an agent in chat. A repository is identified by its git root, so worktrees and
  subfolders stop creating projects. Each project gets a generated project page listing
  its notes by type and the work state of each of its repositories. The junk project
  folders are deleted, and existing notes get their project.
- **Stage 3, better notes at the source.** The compile sees a catalog of every note and
  the full text of the most similar ones, follows explicit rules about what never
  becomes a note, assigns the project from the session's repository, and tags modules.
- **Stage 5, consolidation.** The weekly consolidation rewrites notes that accumulated
  updates into one coherent page, keeps the old text in a collapsed history block, and
  no longer corrupts the claims ledger.

Stage 4 (cleaning the existing notes' content) is deferred; see Out of Scope.

## Stage 0 — Health

### User stories

1. As the owner, I want the nightly maintenance to report success when every step that applies succeeded, so that a red health line means something is really broken.
2. As the owner, I want a step whose optional library is not installed to be reported as skipped, so that installations without optional features stay healthy.
3. As the owner, I want semantic search installed on my vault, so that a question asked in one language finds a note written in another.
4. As the owner, I want the compile's context window to be a setting I can match to my model, so that the compile uses the room my model actually has.
5. As the owner, I want the size of each daily-log piece to account for the fixed prompt text that surrounds it, so that pieces never exceed the budget by construction.
6. As the owner, I want a piece that still does not fit to be set aside with a clear diagnostic while every other pending day compiles, so that one large day never stops the whole memory.
7. As the owner, I want a set-aside piece to stay pending and be retried, so that no captured knowledge is dropped.
8. As the owner, I want dropped tool breadcrumbs reported as dropped breadcrumbs and not as lost captures, so that the health summary does not overstate damage.
9. As the owner, I want the code-navigation server to be reinstalled, and if it still cannot be verified, reported as informational, so that an optional feature does not look like a failure.
10. As the owner, I want the evidence generation that was registered but never activated to be diagnosed and fixed, so that retrieval uses a current index.
11. As the owner, I want to see that Codex sessions are captured again after I re-approved its hooks, so that I know no host is silently missing.
12. As an agent, I want the operating contract to point at `CONTEXT.md` and `docs/adr/`, so that I use the right vocabulary and respect recorded decisions.

### Implementation decisions

- The model-installation step returns a distinct "not applicable" outcome when its library is absent. The nightly counts that outcome as skipped, not failed. Its exit status reflects only real failures.
- The owner's vault installs the semantic extra and its pinned models once. This is an operator action and is documented as one.
- The compile context window becomes a setting with the current value as its safe default. The per-piece byte limit is derived from that window each run: the window, minus the answer reserve and slack, minus the measured size of the fixed prompt (system text, schema, instructions, note catalog). The limit is recomputed every run, because the catalog grows with the vault.
- A daily-log piece that still exceeds the budget is recorded as oversized, skipped for this run, and left pending. The run continues with every other piece and day. Its diagnostic is labelled as deferred, never as lost.
- Breadcrumb appends that give up on the writer gate are classified separately from lost captures. Doctor does not degrade on them.
- The code-navigation server is reinstalled through its supported installer. If its identity still cannot be verified, doctor reports it as informational.
- The never-activated evidence generation is diagnosed first. Its fix is decided from the finding. Root cause: unknown at the time of writing.
- Failure counters are cleared after the fixes land. They are cleared only once they are known to be false.
- The agent contract files (`AGENTS.md` and `CLAUDE.md`, which must stay byte-identical) point to `CONTEXT.md` and `docs/adr/`. Product decisions are recorded there from now on, per ADR 0001.

### Testing decisions

- Nightly seam: in a temporary vault with the optional library absent, the nightly reports the step as skipped and exits successfully.
- Compile seam: with the fake provider, a daily log larger than one piece compiles when the window setting allows it. With a small window, the oversized piece is deferred, the other days compile, and the deferred day stays pending.
- Health seam: a simulated breadcrumb timeout does not appear as a lost capture in doctor's output.

## Stage 1 — Links for Obsidian

### User stories

13. As the owner, I want every link in a note to open the target note in Obsidian, so that I can navigate the memory by clicking.
14. As the owner, I want links written as bare `[[slug]]`, so that they resolve the same way in Obsidian and in the product.
15. As the owner, I want the product to stop writing "links to this page" lines, so that notes carry no duplicate of what Obsidian's backlinks pane already shows.
16. As the owner, I want the links the model proposes to be checked against notes that exist, so that no new link points nowhere.
17. As the owner, I want a dropped link to be recorded in the vault log, so that I can see what the model tried to link.
18. As the owner, I want the links the model proposes on an update to be added too, validated the same way, so that notes gain links as knowledge grows.
19. As the owner, I want a one-time migration that removes existing backlink lines and rewrites path-style links to bare links, so that the notes I already have work in Obsidian.
20. As the owner, I want that migration to show me what it will change before it changes anything, and to be undoable, so that I stay in control.
21. As the owner, I want links that still resolve to nothing after the migration listed for me, not guessed at, so that no link is silently rewritten to the wrong note.
22. As the owner, I want a shipped Obsidian CSS snippet that collapses the claims ledger, so that a note reads as prose.
23. As the owner, I want the installer to place that snippet in my vault's Obsidian configuration when one exists, so that I only need to enable it once.
24. As an agent, I want the memory to work exactly the same when Obsidian is not installed, so that nothing I need depends on a viewer.
25. As the owner, I want lint to stop demanding written backlinks, so that the new rule does not produce endless warnings.

### Implementation decisions

- The canonical link is a bare `[[slug]]` naming a note. Links to non-note files are resolved relative to the vault root (`knowledge/`), as Obsidian resolves them. Lint resolves links the same way.
- The nightly backlink-repair step is removed. The lint rule that requires reciprocal backlinks is retired.
- The index rebuild emits bare links. The test guard that keeps the tracked index free of unpublished notes must recognise bare links, or it stops protecting anything.
- The compile validates proposed links, on both create and update, against the set of known slugs: live notes plus notes created in the same batch. Unknown links are dropped and recorded in the vault log.
- The migration is a single operator command. It supports dry run and apply, and applies through recoverable transactions, so the undo window covers it. It removes written backlink lines, strips the vault-root prefix from path-style links, and reports every link that still does not resolve.
- The product ships an Obsidian CSS snippet that collapses the claims ledger section. The installer copies it into the vault's Obsidian snippets folder only when an Obsidian configuration folder exists. The owner enables it in Obsidian once.
- Per ADR 0003, the prohibition on bundled Obsidian files narrows. Viewer files are allowed. Anything that makes Obsidian required remains forbidden, and the test states the narrower rule.
- The claims ledger stays inside the note. Moving it out is recorded as a decision for the rewrite, not done here.

### Testing decisions

- Compile seam: a draft proposing links to an existing note, to a same-batch note and to a missing note produces a note that holds only the first two links. The vault log records the dropped link.
- Nightly seam: after a nightly pass, no note contains a written backlink line.
- Migration seam: a temporary vault containing backlink lines, path-style links and one broken link migrates to bare links. The broken link is reported, and dry run changes nothing.
- Lint seam: bare links and vault-relative links resolve, and a note without reciprocal backlinks raises no warning.

## Stage 2 — Real projects

### User stories

26. As the owner, I want to say to any agent "I'm starting project X here, add it to the memory", so that registering a project happens in the conversation I'm already in.
27. As the owner, I want to say "this repository belongs to project X", so that a product spanning a backend, a frontend and shared services reads as one project.
28. As the owner, I want to rename a project, detach a repository, and remove a project the same way, so that the map follows my work as it changes.
29. As the owner, I want the same registration to work from Claude Code, Codex and OpenCode, so that it does not matter which agent I'm using.
30. As the owner, I want the project map to be a private file I can also edit in Obsidian, so that I can fix it by hand when that is quicker.
31. As the owner, I want work in an unregistered directory (web research, file chores) to create no project, so that the project list holds only what I'm building.
32. As the owner, I want sessions outside any project still captured and compiled, so that useful lessons from them are not lost.
33. As the owner, I want notes without a project listed on a General page, so that they are as easy to find as project notes.
34. As the owner, I want a worktree or a subfolder of a repository treated as that repository, so that one repository never splits into many.
35. As the owner, I want one folder per project in Obsidian, holding the project page and each repository's work state, so that everything about a product is in one place.
36. As the owner, I want the project page to list the project's notes grouped by type, each with its one-sentence summary, so that I can read what the memory knows about a product at a glance.
37. As the owner, I want the project page to show only current decisions, so that superseded ones do not mislead me.
38. As the owner, I want the project page to show each repository's work state, so that I see where agents left off.
39. As the owner, I want the project page regenerated whenever notes or the map change, so that it is never stale.
40. As the owner, I want blockers recorded when a tool fails, so that the work state shows what went wrong.
41. As the owner, I want turns that change nothing to add no checkpoint, so that the work-state history is not padded with empty entries.
42. As the owner, I want the branch recorded, so that I know which line of work a checkpoint belongs to.
43. As the owner, I want a proposed initial project map built from the real projects the memory already knows, for my approval, so that I don't start from nothing.
44. As the owner, I want every junk project folder deleted in one go after I've seen the list, so that the projects area is clean.
45. As the owner, I want existing notes assigned to projects from a list I approve, so that project pages are complete from day one.
46. As the owner, I want no reminder when I work in an unregistered repository, so that sessions stay quiet.
47. As an agent, I want session-start context and the context tool to find work state in the new layout, so that handoff keeps working.
48. As an agent, I want notes to carry their project in frontmatter, so that I can filter search by project.

### Implementation decisions

- **Repository identity.** Resolve from the working directory upward to the git root. When `.git` is a pointer file (a worktree), follow it to the main checkout. The repository key is the main checkout's path. Its slug derives from the main checkout's directory name, with the existing collision rules. The resolution stops below the vault and the home directory.
- **Project map.** One private Markdown file under `knowledge/projects/`, denied by the existing ignore rules. Each project has a heading followed by a list of repository paths. The parser tolerates hand edits, and doctor reports invalid entries.
- **Registration.** A new MCP tool, the thirteenth, on the existing server, with the uniform response envelope. Its actions: create a project (optionally with the current repository), attach a repository (current or given) to a project, detach a repository, rename a project, remove a project. It writes the map and moves folders through the transaction API. Every place that states the tool count is updated.
- **Unregistered work.** A directory that resolves to no registered repository gets no journal, no work state and no project folder. Capture and compile are unchanged, and its notes have no project.
- **Layout.** `knowledge/projects/<project>/index.md` is the generated project page. `knowledge/projects/<project>/<repository>/` holds that repository's append-only journal and generated work state. `knowledge/projects/general/index.md` lists notes without a project, and `general` is a reserved project name. `docs/STRUCTURE.md` and the structure tests change to match.
- **Project page.** Generated, never hand-edited. It lists the project's live notes grouped by type, each as a bare link plus its one-sentence summary. Superseded and archived notes are excluded. It also lists module tags and each repository's current work state. It is regenerated in the same transaction as the index rebuild (after a compile), after every registration change, and nightly.
- **Work state stays derived, never narrated** (a model-written summary is out of scope). Three fixes:
  - failure events open blockers;
  - a checkpoint with an empty delta is not appended;
  - the branch is read from the repository.
- **Readers.** Session-start handoff and the context tool read work state from the new layout.
- **Note frontmatter** gains `project:` (the project name) and `tags:` (module tags). Retrieval already honours `project`.
- **One-off migration**, as an operator command with dry run and apply:
  1. Propose the initial map from the existing project folders that correspond to real repositories. The owner approves it.
  2. Move kept journals under their project.
  3. Delete every other project folder. This is irreversible; the owner accepted that. The list is shown at dry run and again before apply.
  4. Write `project:` onto existing notes from a list the owner approves.

### Testing decisions

- Lifecycle-event seam: events from a subfolder and from a worktree of a registered repository land in that repository's journal. Events from an unregistered directory create nothing. A failure event opens a blocker, and an empty turn appends no checkpoint.
- MCP seam: calling the registration tool as an agent would creates the project, attaches the repository, renames, detaches and removes. The map and the folders reflect each step, and the envelope reports each result.
- Compile seam: a compiled note from a registered repository's session carries that project, and the project page lists it. A note from unregistered work appears on the General page.
- Migration seam: a temporary vault with real and junk project folders migrates to the approved map. Junk folders are gone, and dry run changes nothing.

## Stage 3 — Better notes at the source

### User stories

49. As the owner, I want the compile to see every existing note's title, summary, project and tags, so that it does not create a note for a topic already covered.
50. As the owner, I want the compile to read the full text of the notes most similar to what it is about to write, so that updates land on the right note and add only what is new.
51. As the owner, I want the reviewer to see the same catalog and similar notes, so that it can reject duplicates.
52. As the owner, I want explicit rules for what never becomes a note, given to both the writer and the reviewer, so that test counts, pull-request numbers, commit hashes, CI links, task status, point-in-time deployment or environment state, one-off machine setup, and generic documentation knowledge stay out.
53. As the owner, I want the test "still true and useful in three months" applied to every note, so that the memory holds durable knowledge only.
54. As the owner, I want a proposed note body that contains frontmatter, a title or its own evidence or claims sections rejected, so that no note is pasted inside another.
55. As the owner, I want the note's project taken from the session's repository and not from the model's guess, so that project assignment is reliable.
56. As the owner, I want the model to tag modules from the tags the project already uses, and allowed to create a new one when none fits, so that tags stay few and meaningful.
57. As the owner, I want every newly created tag recorded in the vault log, so that I notice if tags start to multiply.
58. As the owner, I want notes to stay in English with stable slugs, so that links never break because a note was renamed.
59. As the owner, I want the compile to fall back to the catalog alone when semantic search is unavailable, so that it still works on installations without it.

### Implementation decisions

- **Compile input.**
  - The prompt always includes a compact catalog of every live note: slug, title, one-sentence summary, type, project, tags. It is counted in the budget from Stage 0.
  - For each daily-log piece, the prompt also includes the full bodies of the most similar notes, selected by semantic retrieval over the piece and bounded by the budget.
  - Without vectors, the catalog alone is used.
- **Trusted instructions.** The durability rules and the never-a-note list are part of the instructions, not of the untrusted source block. Both the draft and the critique receive them. The critique also receives the catalog and the similar notes, and may return a verdict naming the existing note that already covers an operation. That operation then becomes an update to that note, or is dropped.
- **Body validation.** A proposed body is rejected if it contains a frontmatter block, a top-level title, or its own evidence or claims sections.
- **Project assignment.** The project is resolved from the daily entry's repository through the project map, never chosen by the model.
- **Module tags.** The prompt carries the project's existing module tags. The model may propose a new tag, and each new tag is recorded in the vault log.
- **Language and slugs.** Notes stay in English. Slugs are chosen once and never changed by later compiles. The catalog makes existing slugs visible, so the model reuses them.

### Testing decisions

- Compile seam, fake provider:
  - a draft that repeats an existing note becomes an update to it, or is dropped;
  - a draft whose body embeds frontmatter is rejected;
  - a note from a registered repository gets that project;
  - a new tag is recorded in the vault log;
  - with vectors absent, the compile succeeds with the catalog alone.
- The rules and the catalog are asserted by what reaches the fake provider's prompt, not by internal helpers.

## Stage 5 — Consolidation

### User stories

60. As the owner, I want a note that has accumulated updates rewritten weekly into one coherent page, so that I read the current knowledge and not a stack of addenda.
61. As the owner, I want the previous text kept in a collapsed history block in the same note, so that nothing is lost and the note still reads cleanly.
62. As the owner, I want every consolidated note to keep exactly one valid claims ledger, so that consolidation never corrupts the note.
63. As the owner, I want decisions never rewritten, only superseded by a newer decision, so that the record of what was decided stays intact.

### Implementation decisions

- Consolidation stays weekly and triggers on two or more update sections. Decisions and retired notes stay excluded.
- The text sent to the model excludes the claims ledger. The history block holds the previous prose only. The consolidated note ends with the single, merged claims ledger.
- The history block is a collapsed details block, which Obsidian shows closed.

### Testing decisions

- Weekly seam, fake provider: a note with two updates is consolidated into one page that contains its one-sentence summary, a collapsed history block and exactly one parseable claims ledger. A decision note with updates is left untouched.

## Testing Decisions (all stages)

- A good test drives the product from outside, the way an event, an agent or the scheduler does, and asserts what a reader would see: files in the vault, the vault log, doctor's report, the tool's response envelope. It never asserts internal helpers, so the structure behind these seams can change freely, including in the Rust rewrite.
- Four seams, all existing:
  1. the compile over a temporary vault with the fake provider;
  2. lifecycle events entering through the hook adapter;
  3. the MCP server called as an agent calls it;
  4. the nightly and weekly passes over a temporary vault.
- Prior art: the compile transaction tests, the automatic-writer integration tests, the MCP server tests, and the backlink and reflection tests (which this work rewrites).
- Tests that pin the replaced behaviour are changed deliberately, each with the stage and the ADR that justifies it:
  - the flat project-slug rules;
  - written backlinks;
  - the bundled-Obsidian prohibition;
  - the tool count;
  - the "current task: none" projections.

## Out of Scope

- Stage 4: cleaning the content of existing notes, meaning merging duplicates, deleting ephemeral notes, marking stale ones superseded, and clearing the quarantined claim candidates. The link migration and the `project:` backfill are mechanical and are in scope.
- The periodic comparison with upstream (ADR 0001). It is an owner routine, not a product change.
- A model-written summary of work state (goal, phase, next actions).
- Moving the claims ledger out of the note.
- Notes in subfolders of `knowledge/notes/`.
- Notes written in any language other than English.
- A reminder about unregistered repositories.
- Publishing any note, and the Rust rewrite itself.

## Further Notes

- Divergence from upstream is accepted (ADR 0001), so merge conflicts with upstream are not a constraint on these stages.
- Stage 2 changes the vault structure. Its decisions are recorded in ADR 0002 and ADR 0003, and `docs/STRUCTURE.md` changes in the same work.
- Stage 3 depends on Stage 0's budget setting and benefits from Stage 0's semantic search. Stage 3's project assignment depends on Stage 2's map.
- Each stage is observed in the owner's vault before the next begins.
