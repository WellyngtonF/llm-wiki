# Antigravity is a supported host again

Cursor and Antigravity were retired on 2026-08-26 because the upstream owner used neither, and the Antigravity hook format the old integration wrote had never been checked against a running Antigravity. This fork's owner does use Antigravity, through T3 Code, which runs it as an ACP agent. So Antigravity comes back, built this time from what a real Antigravity does: on 2026-11-18 a probe `hooks.json` recorded every event an Antigravity ACP session fired, with its payload, and a second session confirmed that the context a `SessionStart` hook returns reaches the model. Cursor stays retired.

What the probe showed, and what the integration is built on:

- Antigravity reads hooks from `<gemini dir>/config/hooks.json` and MCP servers from `<gemini dir>/config/mcp_config.json`. The gemini directory is `~/.gemini` for the standalone products (`antigravity`, `antigravity-cli`, `antigravity-ide`, `antigravity-acp` inside it), and one directory per provider instance under `~/.t3/userdata/providers/antigravity/` when T3 Code starts it.
- A hook file holds named top-level objects. `SessionStart`, `PreInvocation`, `PostInvocation` and `Stop` hold a list of handlers; `PreToolUse` and `PostToolUse` hold groups with a `matcher` regex. A handler is `{"type": "command", "command", "timeout"}`. Antigravity runs it through `sh -c`, synchronously, with the event as camelCase JSON on stdin, and the payload names its event in `hookEventName`.
- The payload carries `conversationId`, `cwd`, `workspacePaths`, `transcriptPath`, `modelName` and `lastUserInput`. Inside T3 Code, `cwd` is T3's attachments folder, not the project; the project is one of the `workspacePaths`. `PostToolUse` adds `toolCall` (`id`, `name`, `args`) and `error`, empty on success. `Stop` adds `terminationReason`.
- `Stop` fires after every turn. There is no session-end event.
- A `SessionStart` hook that prints `{"injectSteps": [{"systemMessage": {"systemMessage": <text>}}]}` adds that text to the conversation.
- The transcript is JSON lines under `<gemini dir>/<product>/brain/<conversation>/`, one step per line, with `step_index`, `type` (`USER_INPUT`, `PLANNER_RESPONSE`, `GENERIC`, …), `content` and `tool_calls`.

The integration:

- One entry point, `scripts/antigravity_hook.py`, for `SessionStart`, `PostToolUse` and `Stop`. It projects the payload onto the shared hook events and hands them to `integration_adapter`, like the other hosts. A failure prints `{}` and exits 0.
- The work happened in the first of the command's own `Cwd`, `cwd` and `workspacePaths` that is a repository, so a session in T3 Code is placed in its project.
- `Stop` follows the rule Codex turn ends use (`codex_turn_end`), under its own state key: a turn end is a capture when the conversation was not captured in the last 30 minutes; otherwise it is kept as the tail, and the next Antigravity hook captures a tail that has been quiet for 30 minutes.
- `PostToolUse` matches `run_command` and the file-writing tools. A command or a written file leaves a breadcrumb; a tool that returned an error is a significant failure.
- The installer materializes `integrations/antigravity/hooks.json` with the absolute `uv` and the vault root, because Antigravity's environment has neither `LLM_WIKI_ROOT` nor, when started from a GUI, a login shell's `PATH`. The install control plane owns the exact `llm-wiki` object in each installation's `hooks.json`: resource `antigravity-user-hooks` for `~/.gemini` (the id the retired integration used, so an old fragment is taken back by the live resource) and `antigravity-t3-hooks-<instance>` for each T3 Code instance. Everything else in the file stays the user's.
- `scripts/antigravity_install.py` adds the `llm-wiki` MCP entry to each installation's `mcp_config.json` when it has none. Like the other hosts' MCP entries, it is not owned: it never replaces another entry, and an uninstall names it instead of removing it.
- `session_evidence` renders the Antigravity transcript as conversation, and `host_transcripts` allows each installation's `brain` directories.
- Doctor checks every installation for both the hooks and the MCP entry.

## Consequences

- A T3 Code instance created after the install has no hooks until the installer runs again; doctor reports it as degraded.
- Hooks run synchronously and block the agent loop, so each has a bound: 15 s for `SessionStart`, 5 s for `PostToolUse`, 30 s for `Stop`.
- Only the ACP product inside T3 Code on macOS was observed. The standalone products are expected to read the same file, and Windows is wired the same way but unverified.
- The IDE documents older tool names (`write_to_file`, `replace_file_content`, `multi_replace_file_content`, with `TargetFile`); they are matched alongside today's `write_file` and `edit_file`.

## Considered Options

- Keep Antigravity MCP-only, configured by hand: rejected. The sessions were never captured, and a session got no context at start.
- Capture on `PostInvocation` instead of `Stop`: rejected. It fires after every model call, many times per turn.
- Treat every `Stop` as a session end: rejected. Each turn would be a separate capture of the whole conversation so far.
