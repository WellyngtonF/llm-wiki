# One shared MCP server for every agent

By default every agent session starts its own stdio server, and each one loads the
models again, about 5 GB of private memory per session on Windows. The shared
server loads them once and serves every agent over loopback HTTP. The decision is
ADR 0008. The steps below are opt-in, and the stdio entry keeps working until you
replace it.

On Windows, `install.ps1 -SharedMcp` does steps 1 and 2. It also does step 3 for an
agent that has no `llm-wiki` entry yet, and for OpenCode. For an existing Claude
Code or Codex entry it prints the replacement.

## 1. Start the server at logon (Windows)

```powershell
& "$env:LLM_WIKI_ROOT\scripts\install-mcp-http-task.ps1" -VaultRoot $env:LLM_WIKI_ROOT
```

This registers and starts the `LLMWiki-McpHttp` task. The server binds
`127.0.0.1:8765` and takes about 30 seconds to warm before it answers. Calls that
arrive during the warm-up wait their turn instead of being refused. Its output goes
to `logs/mcp-http.log`. Check the task with `-Status`, remove it with `-Uninstall`.

By hand, without the task:

```powershell
uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/mcp_http.py --ensure
```

`--ensure` exits 0 if a server already holds the port. `--restart` stops a running
server cleanly and starts it again. The nightly pass runs that restart itself after
an update brings in new code.

## 2. Put the token in the user environment

```powershell
$token = uv run --locked --no-sync --directory $env:LLM_WIKI_ROOT python scripts/mcp_http.py --print-token
[Environment]::SetEnvironmentVariable("LLMWIKI_MCP_TOKEN", $token.Trim(), "User")
```

Restart the agent apps afterwards, including the desktop client that starts them,
so they see the new variable. The token file is `run/mcp-http/token`, and it
survives restarts.

## 3. Point each agent at the URL

`scripts/mcp_http.py --client-config` prints these entries. Each one replaces the
`llm-wiki` entry that runs `scripts/mcp_server.py`.

Claude Code, `~/.claude.json` under `mcpServers`:

```json
"llm-wiki": {
  "type": "http",
  "url": "http://127.0.0.1:8765/mcp",
  "headers": { "Authorization": "Bearer ${LLMWIKI_MCP_TOKEN}" }
}
```

Codex, `~/.codex/config.toml`. Delete `command`, `args` and the
`[mcp_servers.llm-wiki.env]` table:

```toml
[mcp_servers.llm-wiki]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "LLMWIKI_MCP_TOKEN"
```

OpenCode, `opencode.json(c)` under `mcp`:

```json
"llm-wiki": {
  "type": "remote",
  "url": "http://127.0.0.1:8765/mcp",
  "headers": { "Authorization": "Bearer {env:LLMWIKI_MCP_TOKEN}" },
  "enabled": true
}
```

The provider settings (`MEMORY_*`) that used to sit in each agent's `env` block now
come from the user environment, because the shared server's `compile` reads its own
environment. A reinstall keeps these entries: the installer treats them as
equivalent to the stdio entry.

## What is shared, and what is not

Each call carries its own arguments, directory and deadline. The server keeps no
MCP session, so no agent's call sees another agent's state, and a restart between
two calls is invisible to the agents. The models, the language-server sessions and
the worker slots are shared. There are eight slots, set by
`LLMWIKI_MCP_WORKER_SLOTS`. When every slot is busy, a call gets the `busy` answer
and can be retried.
