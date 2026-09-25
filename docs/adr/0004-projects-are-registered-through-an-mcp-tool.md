# Projects are registered through an MCP tool

The owner registers projects in chat ("I'm starting project X here"), so an agent has to act on that sentence. Lifecycle hooks only fire on events and cannot understand it, and a skill that edits the project map by hand would bypass the transaction API and differ per agent. So registration is a thirteenth tool, `manage_project`, on the existing MCP server: create, attach, detach, rename, remove and list, each one transaction over the map, the work-state folders, the notes' `project:` and the project pages. This departs from the earlier contract that no MCP tool is added; the tool adds no server, daemon or runtime root.

## Considered Options

- A hook that detects the phrase in the prompt: rejected as fragile.
- A skill that edits the map file directly: rejected, because writes would escape the transaction API and each agent would do it differently.
