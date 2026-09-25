# The fork diverges from upstream

This fork is its own product, not a mirror of the upstream it came from. Upstream changes too fast to merge continuously, and the owner plans to rewrite the fork in Rust, so upstream fixes and features are ported selectively. A periodic review compares upstream from a recorded last-reviewed commit. Product decisions are recorded here, in `docs/adr/`, with the vocabulary in `CONTEXT.md`, both public and free of private knowledge, so they can serve as the specification of the rewrite.

## Considered Options

- Keep merging upstream `main`: rejected, because the compile, search and transaction modules change weekly upstream and every local improvement would conflict.
- Record decisions as private pages under `knowledge/notes/`: rejected for product decisions, because the rewrite needs them in the repository.
