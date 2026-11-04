# One shared MCP server for every agent

Every agent session used to start its own stdio MCP server, and each one loaded the same models on its own. Measured on 2026-11-04 on a 32 GB Windows machine: one warm server holds 5.2 GB of private memory (torch 0.5 GB, the e5-small encoder 1.3 GB, the int8 cross-encoder 3.2 GB). Six sessions open in one desktop client meant seven servers and about 36 GB of committed memory, and the machine stalled. So one process now serves every agent: `scripts/mcp_http.py` over Streamable HTTP on literal loopback, behind the bearer token and Origin and Host checks it already had. Agents register its URL instead of a command. The stdio server stays as it is: it is still the default, and an agent that keeps the stdio entry keeps working.

The shared server is stateless. It keeps no MCP session, so nothing one agent did can shape another agent's call, and a restart between two calls is invisible to a client. The tools never held per-session state: every call carries its own arguments, directory and deadline, and session capture runs in the lifecycle hooks, which are separate processes. The models, the retrieval cost records, the warm-up state and the language-server sessions are shared on purpose. That sharing is the point of the change.

## Consequences

- One process lives for the logon session. This is a persistent process, which the earlier contracts ruled out ("no persistent daemon"). The owner approved it for this one purpose. It is not a scheduler, a queue worker or a watcher. It answers MCP calls and nothing else.
- A Windows logon task, `LLMWiki-McpHttp` (`scripts/install-mcp-http-task.ps1`), starts it with the vault's `pythonw`, so no console window opens. The task is opt-in. Registering it changes no agent configuration.
- `--ensure` binds the port before it loads any model. The bind is the single-instance guard: a second start exits 0 without loading anything. On Windows the socket is `SO_EXCLUSIVEADDRUSE`, so no other socket can take the port.
- `POST /admin/shutdown`, behind the same guard, stops the server after the calls in flight. After a fast-forward that brought in new code, the nightly pass uses it to restart a running server (`mcp_endpoint.restart_shared_server`). A server that is not running stays stopped.
- The token stays in `run/mcp-http/token`. The clients read it from the user environment variable `LLMWIKI_MCP_TOKEN`, so no agent configuration file holds the secret.
- Every agent now shares one worker pool. A stdio server keeps four slots for its one agent. The shared server has eight, set by `LLMWIKI_MCP_WORKER_SLOTS` and bounded at 32. A call that finds every slot busy gets the existing `busy` answer and may retry.
- The LLM provider settings come from the server's environment, which is the user environment. They no longer come from each agent's `env` block, so every agent's `compile` call uses the same provider order.
- The installer treats the HTTP entry as equivalent to the stdio one, for Codex and for OpenCode. A reinstall therefore does not switch an agent back to a server of its own. `install.ps1 -SharedMcp` registers the task, sets the token variable and writes the HTTP entry. It writes the entry where an agent has none, and it switches OpenCode's entry in place. For an existing Claude Code or Codex entry it prints the replacement instead. `install.sh` has no equivalent yet.

## Considered Options

- A small stdio shim per session that forwards to one resident server: rejected. Each session would still start a process (~40 MB), and the shim would be a second protocol layer to keep correct. Every agent here already speaks Streamable HTTP natively.
- Stateful HTTP sessions: rejected. A restart would drop every agent's session, and the server has no state worth keeping in one.
- Keep stdio and load the models lazily: rejected. It moves the cost to the first question of each session and still pays it once per session.
