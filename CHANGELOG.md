# Changelog

## 0.1.0 - 2026-11-16

First release.

- Git-backed memory store for CLAUDE.md, AGENTS.md, napkins, Cursor/Windsurf/Cline/Copilot rules and `~/.claude` globals
- Three-way sync that reports conflicts instead of overwriting; union merge for concurrent appends
- Online sync over any git remote; air-gapped sync with `punk export` / `punk import` bundles
- Secret scanning (block or `--redact`) before anything enters the store
- MCP server (`punk mcp`) with context, search, read and append tools
- Symlinked memories (e.g. `AGENTS.md -> CLAUDE.md`) are recorded as links and recreated on restore; links leaving the project are reported, never followed
- `punk diff [ID] [PATH]` shows a unified diff between local memories and the store; conflict reports name the exact `punk diff` command
- Global memories symlinked into a dotfiles repo (e.g. `~/.claude/CLAUDE.md -> ~/dotfiles/claude/CLAUDE.md`) are followed: saved by content and restored through the link
