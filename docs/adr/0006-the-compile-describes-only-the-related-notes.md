# The compile describes only the notes related to its batch

Every compile prompt carried a catalog line for each live note: slug, title, one-sentence summary, type, project and tags (readable-memory spec, issue #19). That line costs about 300 tokens under the compile's byte-count estimate, so the catalog grows with the vault, and a vault of 171 notes filled a 64,000-token compile window before any daily log was added: every compile was refused. Raising the window only moves the refusal to a later note count. So the catalog now has two tiers. Every live note is still listed, so the model can see that a slug exists and must never be re-created, but only by its slug. Title, summary, type, project and tags are given only for the notes related to the batch: the ones the vault's semantic search ranks closest to the batch's daily entries, at most `CATALOG_DESCRIBED_MAX` of them, added most related first while the prompt still fits. A vault with no more live notes than that limit is described in full, as before. This supersedes the "catalog of every live note: slug, title, one-sentence summary, type, project, tags" line of `docs/specs/2026-09-24-readable-memory.md`.

## Consequences

- A slug-only line costs about a tenth of a described line, so the compile window is filled ten times later. The cost is still linear in the number of notes; the list of slugs is what keeps a covered topic from being re-created, and it stays whole.
- Without vectors on a vault larger than the limit, no note is described. The model still sees every slug and the critique still checks duplicates against it; the compile says once that similar notes are unavailable, as it already did.
- The reviewer reads the same tiers as the writer. When one operation is too long to review beside them, the reviewer sheds the similar notes first and the descriptions after.
- The described notes are part of the compile cache key, like the similar notes: another selection is another prompt.
- The compile still counts one token per UTF-8 byte. A real tokenizer would give about three to four times more room; that is a separate decision.

## Considered Options

- Raise `MEMORY_COMPILE_CONTEXT_TOKENS`: rejected as the fix, because the refusal returns as the vault grows. It stays the operator's setting for the window the compile model supports.
- Drop the catalog and show only the related notes: rejected, because a note the search does not rank would be re-created under a new slug.
- Describe notes in slug order until the window is full: rejected, because the order says nothing about which notes the batch is about, and the prompt would always be as large as the window.
