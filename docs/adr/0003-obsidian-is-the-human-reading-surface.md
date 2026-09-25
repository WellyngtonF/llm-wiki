# Obsidian is the human reading surface

The owner reads the memory in Obsidian, with the vault rooted at `knowledge/`. So the product writes for it: bare `[[slug]]` links, no written backlinks (Obsidian derives them), and it may ship viewer files such as a CSS snippet and generated project pages. Upstream treats Obsidian as an optional viewer and forbids bundling Obsidian files. This fork deliberately departs from that, with one limit kept: nothing the agents need may depend on Obsidian being installed or open.
