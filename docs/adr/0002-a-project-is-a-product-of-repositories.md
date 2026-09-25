# A project is a registered product made of repositories

A project is the product the owner thinks in, and it may span several repositories. Projects exist only when the owner registers them, by asking an agent in chat; a repository joins a project the same way. Much agent work is not project work (web research, one-off file chores), so an unregistered directory creates no project and no work state. A repository is identified by its main checkout, so worktrees and subfolders belong to it and never become projects of their own. Areas inside a repository are modules, expressed as tags on notes.

## Considered Options

- A project per working directory (the previous behaviour): rejected, because every `cd` created a new project and most project folders were noise.
- Every git repository becomes a project automatically: rejected, because not every repository the agent touches is something the owner is building.
- A project per repository only: rejected, because the owner reads notes by product, and one product can span a backend, a frontend and shared services.
