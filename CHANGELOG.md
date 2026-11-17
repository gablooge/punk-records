# Changelog

## Unreleased

- `punk install-hooks` adds Claude Code hooks that restore memories when a session starts and save
  and sync them when it ends; `--project` for one project, `--uninstall` to remove them
- `punk dedupe [ID] [PATH]` lists duplicate lines left by union merges (the same line added in two
  places) and `--apply` keeps only the first copy; blank lines, fenced and indented code, headings
  and lines without letters are never touched
- `punk watch` saves memory files a moment after they change (polling, no dependencies), with
  `--interval`, `--debounce`, `--rescan` and an optional `--sync-every`

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
