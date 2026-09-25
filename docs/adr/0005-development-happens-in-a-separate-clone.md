# Development happens in a separate clone

The installed vault is both the product's checkout and the running memory, and a directory inside the vault is never a project: the memory's own model calls run there, so its sessions are not captured as work. Developing the product in the vault would therefore leave that work unrecorded, and every edit would risk the nightly fast-forward, which declines when an update touches a locally modified file. So the product is developed in a second, ordinary clone of the fork, outside the vault, registered as a project like any other repository. The vault checkout only follows the fork's `main`: nobody commits or edits code in it, and the nightly fast-forward, or a manual `git pull --ff-only`, deploys what was pushed. No code changes; the environment contract stays as it is.

## Consequences

- `LLM_WIKI_ROOT` keeps naming the vault. A script run by hand from the development clone acts on the installed vault; the test suite is hermetic and does not.
- The installer is never run from the development clone, because it would point the hooks, scheduled tasks and `LLM_WIKI_ROOT` at the clone.
- A change to hooks, scheduled tasks or dependencies reaches the vault only after the installer is run again in the vault.

## Considered Options

- Register the vault itself as a project and mark the memory's own calls so capture can tell them apart: rejected, because it needs a new signal on every internal call and a code change, while a separate clone needs neither.
