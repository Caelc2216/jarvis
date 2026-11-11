"""Jarvis's tools as an MCP server, so Claude Code and Codex can use them.

Claude Code and Codex start this automatically; you don't run it yourself.
It exposes exactly the tools in tools.py and nothing else.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer  # noqa: E402  (mcp 2.x)

import functools  # noqa: E402

from tools import TOOLS, log_activity  # noqa: E402

mcp = MCPServer("jarvis", log_level="ERROR")


def _logged(fn):
    @functools.wraps(fn)  # keeps the signature, so the tool's inputs stay the same
    def wrapper(*args, **kwargs):
        log_activity(fn.__name__, kwargs)  # shows up in the running Jarvis HUD
        return fn(*args, **kwargs)
    return wrapper


for fn, description, _schema in TOOLS:
    mcp.add_tool(_logged(fn), name=fn.__name__, description=description)

if __name__ == "__main__":
    mcp.run()  # stdio
