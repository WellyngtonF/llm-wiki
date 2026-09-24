"""Exercise the actual stdio launch, including envelope Git metadata."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows stdio subprocess handles")
def test_fresh_stdio_client_reads_page_without_metadata_timeout(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    (notes / "recall-check.md").write_text(
        "---\ntype: decision\n---\n# Recall check\nKeep the cache for 27 minutes.\n",
        encoding="utf-8",
    )
    server = Path(__file__).resolve().parents[1] / "scripts/mcp_server.py"

    async def check():
        parameters = StdioServerParameters(
            command=sys.executable, args=[str(server)],
            env=dict(os.environ, LLM_WIKI_ROOT=str(tmp_path),
                     LLM_WIKI_STATE_ROOT=str(tmp_path), PYTHONUTF8="1"),
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                result = await client.call_tool("read_page", {"slug": "recall-check"})
                payload = json.loads(result.content[0].text)
                assert not result.isError, payload
                assert "27 minutes" in payload["data"]["content"]

    asyncio.run(check())
