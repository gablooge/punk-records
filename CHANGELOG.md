# Changelog

## Unreleased

- `punk status --json` and `punk list --json` (satellites, local paths, memory files, team
  subscriptions) for editor integrations; the JSON carries `"version": 1`
- Claude Code auto memory (`~/.claude/projects/<project>/memory/*.md`) is saved with its project as
  `@claude-memory/...` and restored into the right folder on every machine, whatever the checkout
  path; worktrees share the main checkout's. Opt out with `"exclude": ["@claude-memory/*"]`
- `punk show @team/<satellite> <path>` prints a memory from a subscribed team store

## 0.3.1 - 2026-11-17

- Secret scanning no longer blocks documentation placeholders in `password=...`-style lines
  (`YOUR_API_KEY`, `EXAMPLE_TOKEN`, `changeme`, `xxxx...`, `SECRET_FROM_VAULT` names); specific key
  formats are unchanged
- `punk git --help` (and `-h`) now reach git instead of printing punk's own help
- `punk track` warns when the folder is your home folder or holds more than 50,000 files
- Tested on Python 3.15 (Linux, macOS, Windows); README has a Sponsor badge

## 0.3.0 - 2026-11-17

Shared team conventions, and a richer MCP server for agents.

- Team brain: `punk subscribe NAME URL` reads another Punk Records store read-only; its memories
  show up in `punk context`, `punk search` and the MCP context/search tools (labelled `@NAME`), are
  refreshed by `punk sync` and the session-start hook, and are never written back
- **Changed:** the MCP `sync` tool is off by default because it reaches the network; register the
  server as `punk mcp --allow-sync` to offer it
- MCP `write_memory` replaces a memory file with conflict checks: it needs the sha256 that
  `read_memory` now reports, and refuses if the file changed since or has unsaved local edits
- Every memory file is an MCP resource (`punk-records:///<satellite>/<path>`), and a `punk-context`
  prompt starts a conversation with your memories
- The MCP server speaks the 2026-07-28 revision (`server/discover`, per-request versions) as well as
  `initialize`-based clients, and answers `initialize` with a version it supports instead of echoing
  the client's
- `get_context` and the prompt default to the project the client started in (`CLAUDE_PROJECT_DIR`)

## 0.2.0 - 2026-11-17

Sync without thinking about it, and on hosts you do not trust.

- `punk install-hooks` adds Claude Code hooks that restore memories when a session starts and save
  and sync them when it ends; `--project` for one project, `--uninstall` to remove them
- `punk dedupe [ID] [PATH]` lists duplicate lines left by union merges (the same line added in two
  places) and `--apply` keeps only the first copy; blank lines, fenced and indented code, headings
  and lines without letters are never touched
- `punk encrypt init <url>` syncs through a remote that only ever holds one age-encrypted bundle;
  stores stay plain text locally, merges happen locally, `punk init --encrypted <url>` joins from
  another machine, `punk encrypt add-recipient` adds another machine's own key
- `punk watch` saves memory files a moment after they change (polling, no dependencies), with
  `--interval`, `--debounce`, `--rescan` and an optional `--sync-every`
- `punk track` says "1 memory file" instead of "1 memory files"
- README: using Punk Records with Claude Code, Cursor, Codex and AI tools without MCP, with prompt
  examples

## 0.1.0 - 2026-11-17

First release.

### Store and sync
- Git-backed memory store for `CLAUDE.md`, `AGENTS.md`, napkins, Cursor/Windsurf/Cline/Roo/Kiro/Copilot
  rules and `~/.claude`, `~/.codex`, `~/.gemini` globals
- Three-way sync per file that reports conflicts instead of overwriting; union merge for concurrent
  appends
- Online sync over any git remote; air-gapped sync with `punk export` / `punk import` bundles
- Projects are identified by their git remote, so the same repo at different paths on different
  machines shares one set of memories
- `punk diff [ID] [PATH]` shows a unified diff between local memories and the store, and names
  line-ending-only differences; conflict reports print the exact `punk diff` command

### Symlinks
- Symlinked memories (e.g. `AGENTS.md -> CLAUDE.md`) are recorded as links and recreated on
  restore; `punk remember` and the MCP tools write through to the linked file
- Global memories symlinked into a dotfiles repo (e.g. `~/.claude/CLAUDE.md ->
  ~/dotfiles/claude/CLAUDE.md`) are followed: saved by content and restored through the link
- Links that leave the project are reported, never followed

### Agents
- MCP server (`punk mcp`) with context, search, read, list, append and sync tools
- `punk context` prints everything relevant to a directory for any LLM; `punk remember` appends a
  note from the shell

### Safety
- Secret scanning (block, or store with `--redact`) before anything enters the store
- Restore and append only write memory paths inside the project (or `$HOME` for global memories);
  a tampered store or bundle cannot plant hooks or scripts

### Platforms
- Linux, macOS and Windows, Python 3.9 to 3.13 (CI tests 3.9 and 3.13), no dependencies besides git
- Memories stay byte for byte identical across machines: the store never converts line endings,
  even with `core.autocrlf=true`, and appends keep a file's own line endings
- UTF-8 throughout, including the MCP stream and piped CLI output (`punk context | llm`) on Windows

### Known limitations
- If two machines change the same line, the merged file keeps both versions
- Creating symlinks on Windows needs Developer Mode or an elevated shell
