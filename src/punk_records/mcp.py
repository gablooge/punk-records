"""Minimal, dependency-free MCP server (JSON-RPC 2.0 over stdio).

Register it with an agent, e.g.:  claude mcp add punk-records -- punk mcp
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import __version__
from .store import PunkError, Store

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "list_satellites",
        "description": "List every project/machine whose memories are in Punk Records.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_memories",
        "description": "List memory files (CLAUDE.md, AGENTS.md, napkins, rules...), optionally for one satellite.",
        "inputSchema": {"type": "object", "properties": {"satellite": {"type": "string"}}},
    },
    {
        "name": "read_memory",
        "description": "Read one memory file.",
        "inputSchema": {
            "type": "object",
            "properties": {"satellite": {"type": "string"}, "path": {"type": "string"}},
            "required": ["satellite", "path"],
        },
    },
    {
        "name": "search_memories",
        "description": "Case-insensitive search across all memories of all projects.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "satellite": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_context",
        "description": "All memories relevant to a working directory (global + that project), as one document.",
        "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string"}}},
    },
    {
        "name": "append_memory",
        "description": ("Append a lesson or note to a memory file and commit it. Use for durable learnings "
                        "(conventions, gotchas, decisions), never secrets."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "satellite": {"type": "string", "description": "satellite id, e.g. 'global' or 'github.com/me/repo'"},
                "path": {"type": "string", "description": "e.g. CLAUDE.md, AGENTS.md, .claude/napkin.md"},
                "text": {"type": "string"},
            },
            "required": ["satellite", "path", "text"],
        },
    },
    {
        "name": "sync",
        "description": "Save local memories, pull/push the remote if one is configured, and restore.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(store: Store, name: str, args: dict) -> str:
    if name == "list_satellites":
        paths = store.load_state()["paths"]
        return json.dumps({sid: {**meta, "local_path": paths.get(sid)}
                           for sid, meta in store.satellites().items()}, indent=2)
    if name == "list_memories":
        return "\n".join(f"{s}/{r}" for s, r in store.memories(args.get("satellite"))) or "(none)"
    if name == "read_memory":
        return store.read(args["satellite"], args["path"])
    if name == "search_memories":
        hits = list(store.search(args["query"], sid=args.get("satellite")))
        return "\n".join(f"{s}/{r}:{n}: {line}" for s, r, n, line in hits[:200]) or "(no matches)"
    if name == "get_context":
        return store.context(Path(args.get("cwd") or os.getcwd())) or "(no memories for this directory)"
    if name == "append_memory":
        store.append(args["satellite"], args["path"], args["text"])
        return f"remembered in {args['satellite']}/{args['path']}"
    if name == "sync":
        actions = store.sync()
        return "\n".join(map(str, actions)) or "already in sync"
    raise PunkError(f"unknown tool: {name}")


def handle(store: Store, msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    try:
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "punk-records", "version": __version__},
                "instructions": ("Punk Records holds the user's persistent AI memories across projects "
                                 "and machines. Call get_context at the start of a task; use "
                                 "append_memory to record durable lessons."),
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                text, err = call_tool(store, params.get("name", ""), params.get("arguments") or {}), False
            except (PunkError, KeyError, OSError) as e:
                text, err = f"error: {e}", True
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # never kill the server on one bad request
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def serve(store: Store) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(store, msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
