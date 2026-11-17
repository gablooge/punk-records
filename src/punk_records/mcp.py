"""Minimal, dependency-free MCP server (JSON-RPC 2.0 over stdio).

Register it with an agent, e.g.:  claude mcp add punk-records -- punk mcp

Dual-era (https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning):
- legacy clients open with `initialize` and get one of LEGACY_VERSIONS back;
- modern clients send `_meta` with `io.modelcontextprotocol/protocolVersion` on every request (and may
  call `server/discover` first); their results carry `resultType: "complete"`.

Offers tools, every memory file as a resource (`punk-records:///<satellite>/<path>`, with the satellite
id percent-encoded because it contains slashes), and a `punk-context` prompt. The `sync` tool reaches
the network, so it is only offered when the server runs with `punk mcp --allow-sync`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from . import __version__
from .store import PunkError, Store, sha

MODERN_VERSIONS = ["2026-07-28"]
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]  # newest first
PROTOCOL_VERSION = LEGACY_VERSIONS[0]
SCHEME = "punk-records"
PAGE_SIZE = 200
SERVER_INFO = {"name": "punk-records", "version": __version__}
INSTRUCTIONS = ("Punk Records holds the user's persistent AI memories across projects and machines. "
                "Call get_context at the start of a task; use append_memory to record durable lessons, "
                "and write_memory (with the sha256 from read_memory) to rewrite a memory file.")

SATELLITE = {"type": "string", "description": "satellite id, e.g. 'global' or 'github.com/me/repo'"}
MEMORY_PATH = {"type": "string", "description": "e.g. CLAUDE.md, AGENTS.md, .claude/napkin.md"}

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
        "description": "Read one memory file. Also returns its sha256, which write_memory needs.",
        "inputSchema": {
            "type": "object",
            "properties": {"satellite": SATELLITE, "path": MEMORY_PATH},
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
        "description": ("All memories relevant to a working directory (global + that project), as one document. "
                        "Defaults to the project the client was started in."),
        "inputSchema": {"type": "object", "properties": {"cwd": {"type": "string"}}},
    },
    {
        "name": "append_memory",
        "description": ("Append a lesson or note to a memory file and commit it. Use for durable learnings "
                        "(conventions, gotchas, decisions), never secrets."),
        "inputSchema": {
            "type": "object",
            "properties": {"satellite": SATELLITE, "path": MEMORY_PATH, "text": {"type": "string"}},
            "required": ["satellite", "path", "text"],
        },
    },
    {
        "name": "write_memory",
        "description": ("Replace the whole content of a memory file, e.g. to reorganise or correct it. For an "
                        "existing file pass base_sha256 from read_memory: the write is refused if the file "
                        "changed since, or if the user has unsaved edits on this machine. Never include secrets."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "satellite": SATELLITE,
                "path": MEMORY_PATH,
                "content": {"type": "string", "description": "the complete new file content"},
                "base_sha256": {"type": "string", "description": "sha256 reported by read_memory"},
            },
            "required": ["satellite", "path", "content"],
        },
    },
    {
        "name": "sync",
        "description": "Save local memories, pull/push the remote if one is configured, and restore.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

PROMPTS = [
    {
        "name": "punk-context",
        "title": "Load my punk records",
        "description": "Start with everything Punk Records knows for this directory (global + project memories).",
        "arguments": [{"name": "cwd", "description": "directory (default: where the client started)",
                       "required": False}],
    }
]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _default_cwd(args: dict) -> Path:
    return Path(args.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


# ------------------------------------------------------------------- resources
def resource_uri(sid: str, rel: str) -> str:
    return f"{SCHEME}:///{quote(sid, safe='')}/{quote(rel, safe='/')}"


def parse_uri(uri: str) -> tuple[str, str]:
    """punk-records:///<sid>/<rel> -> (sid, rel); rejects anything else, including `..` segments."""
    parts = urlsplit(uri)
    if parts.scheme != SCHEME or parts.netloc or not parts.path.startswith("/"):
        raise PunkError(f"not a punk-records resource: {uri}")
    raw_sid, _, raw_rel = parts.path[1:].partition("/")
    sid, rel = unquote(raw_sid), unquote(raw_rel)
    segments = PurePosixPath(rel).parts
    if not sid or not rel or rel.startswith("/") or any(s in ("..", ".") for s in [*segments, *sid.split("/")]):
        raise PunkError(f"invalid resource path: {uri}")
    return sid, rel


def _mime(rel: str) -> str:
    return "text/markdown" if rel.endswith((".md", ".mdc", ".markdown")) else "text/plain"


def list_resources(store: Store, cursor: str | None) -> dict:
    items = store.memories()
    try:
        start = int(cursor) if cursor else 0
    except ValueError:
        raise RpcError(-32602, f"invalid cursor: {cursor!r}") from None
    if start < 0:
        raise RpcError(-32602, f"invalid cursor: {cursor!r}")
    page = items[start:start + PAGE_SIZE]
    result = {"resources": [{"uri": resource_uri(s, r), "name": f"{s}/{r}", "mimeType": _mime(r),
                             "size": (store.records / s / r).stat().st_size} for s, r in page]}
    if start + PAGE_SIZE < len(items):
        result["nextCursor"] = str(start + PAGE_SIZE)
    return result


def read_resource(store: Store, uri: str, modern: bool) -> dict:
    not_found = -32602 if modern else -32002  # 2025-11-25 and earlier used -32002
    try:
        sid, rel = parse_uri(uri)
        text = store.read(sid, rel)
    except PunkError as e:
        raise RpcError(not_found, f"Resource not found: {e}", {"uri": uri}) from None
    return {"contents": [{"uri": uri, "mimeType": _mime(rel), "text": text}]}


# --------------------------------------------------------------------- prompts
def get_prompt(store: Store, name: str, args: dict) -> dict:
    if name != "punk-context":
        raise RpcError(-32602, f"unknown prompt: {name}")
    cwd = _default_cwd(args)
    context = store.context(cwd) or "(no memories for this directory yet)"
    text = (f"Here are my notes from Punk Records for {cwd} (global preferences first, then this project's "
            f"memories). Follow them, and tell me if anything you do would contradict them.\n\n{context}")
    return {"description": f"Punk Records context for {cwd}",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


# ----------------------------------------------------------------------- tools
def tools(allow_sync: bool) -> list[dict]:
    return [t for t in TOOLS if allow_sync or t["name"] != "sync"]


def call_tool(store: Store, name: str, args: dict, allow_sync: bool = False) -> list[str]:
    if name == "list_satellites":
        paths = store.load_state()["paths"]
        return [json.dumps({sid: {**meta, "local_path": paths.get(sid)}
                            for sid, meta in store.satellites().items()}, indent=2)]
    if name == "list_memories":
        return ["\n".join(f"{s}/{r}" for s, r in store.memories(args.get("satellite"))) or "(none)"]
    if name == "read_memory":
        text = store.read(args["satellite"], args["path"])
        digest = sha(store._write_target(args["satellite"], args["path"])[1].read_bytes())
        return [text, f"(punk-records: sha256={digest}; pass it as base_sha256 to write_memory)"]
    if name == "search_memories":
        hits = list(store.search(args["query"], sid=args.get("satellite")))
        return ["\n".join(f"{s}/{r}:{n}: {line}" for s, r, n, line in hits[:200]) or "(no matches)"]
    if name == "get_context":
        return [store.context(_default_cwd(args)) or "(no memories for this directory)"]
    if name == "append_memory":
        written = store.append(args["satellite"], args["path"], args["text"])
        return [f"remembered in {args['satellite']}/{written}"]
    if name == "write_memory":
        written, digest = store.write(args["satellite"], args["path"], args["content"], args.get("base_sha256"))
        return [f"wrote {args['satellite']}/{written} (sha256={digest})"]
    if name == "sync":
        if not allow_sync:
            raise PunkError("sync is disabled because it reaches the network; start the server with "
                            "`punk mcp --allow-sync` to enable it, or run `punk sync` yourself")
        actions = store.sync()
        return ["\n".join(map(str, actions)) or "already in sync"]
    raise PunkError(f"unknown tool: {name}")


# -------------------------------------------------------------------- dispatch
def _capabilities() -> dict:
    return {"tools": {}, "resources": {}, "prompts": {}}


def _check_modern(params: dict) -> bool:
    """True for a modern (per-request _meta) request; raises RpcError for a bad one."""
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    if version is None:
        return False
    if version not in MODERN_VERSIONS:
        raise RpcError(-32022, "Unsupported protocol version", {"supported": MODERN_VERSIONS, "requested": version})
    if "io.modelcontextprotocol/clientCapabilities" not in meta:
        raise RpcError(-32602, "missing _meta io.modelcontextprotocol/clientCapabilities")
    return True


def dispatch(store: Store, method: str, params: dict, allow_sync: bool) -> dict:
    modern = _check_modern(params)
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": requested if requested in LEGACY_VERSIONS else PROTOCOL_VERSION,
            "capabilities": _capabilities(),
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "server/discover":
        return {"supportedVersions": MODERN_VERSIONS, "capabilities": _capabilities(), "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools(allow_sync)}
    if method == "tools/call":
        try:
            texts, err = call_tool(store, params.get("name", ""), params.get("arguments") or {}, allow_sync), False
        except (PunkError, KeyError, OSError) as e:
            texts, err = [f"error: {e}"], True
        return {"content": [{"type": "text", "text": t} for t in texts], "isError": err}
    if method == "resources/list":
        return list_resources(store, params.get("cursor"))
    if method == "resources/read":
        return read_resource(store, str(params.get("uri", "")), modern)
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": PROMPTS}
    if method == "prompts/get":
        return get_prompt(store, str(params.get("name", "")), params.get("arguments") or {})
    raise RpcError(-32601, f"unknown method {method}")


def handle(store: Store, msg: dict, allow_sync: bool = False) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    try:
        result = dispatch(store, method, params, allow_sync)
        if _check_modern(params):
            result = {"resultType": "complete", **result,
                      "_meta": {**result.get("_meta", {}), "io.modelcontextprotocol/serverInfo": SERVER_INFO}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except RpcError as e:
        error = {"code": e.code, "message": e.message}
        if e.data is not None:
            error["data"] = e.data
        return {"jsonrpc": "2.0", "id": mid, "error": error}
    except Exception as e:  # never kill the server on one bad request
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def serve(store: Store, stdin=None, stdout=None, allow_sync: bool = False) -> None:
    """Newline-delimited JSON-RPC over raw bytes: always UTF-8 and "\\n", whatever the OS locale."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(store, msg, allow_sync) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply).encode("utf-8") + b"\n")
            stdout.flush()
