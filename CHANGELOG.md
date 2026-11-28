# Changelog

## Unreleased

- Xcode: memories, commands, agents and skills of the agents run inside Xcode (Claude Agent,
  Codex, Gemini, under `~/Library/Developer/Xcode/CodingAssistant/`) are part of the global
  satellite; Xcode's MCP list and the agents' settings and credentials are not. README: "Xcode"
  (memories, the `mcp-servers.json` entry, syncing, driving Xcode from the terminal)

## 0.6.2 - 2026-11-26

A store started on its own now joins its remote by itself, with nothing to rebuild by hand.

- A store started on its own (`punk init` before the remote was known) joins the remote on the next
  sync instead of never merging: it continues the remote's history, adds the files only it had, and
  reports a memory that differs as a conflict. The old history is kept under
  `refs/punk/before-join/`. A remote that is not a punk store is never joined, where before its
  history could be silently rebased onto (#51)
- `punk init <url>` with a bare remote whose HEAD names another branch (git's default `master`)
  now continues the remote's `main` instead of starting a store with its own history
- README: eight copy-and-paste prompts for setting punk up through an agent (first time, private
  backup, new laptop, Claude Code hooks, encryption, team store, unattended agents, end of session),
  as code blocks with GitHub's copy button; a test checks they name only real commands
- README: the "Things to ask your agent" phrases also as one copyable block under the table, kept
  identical to it by a test

## 0.6.1 - 2026-11-23

Exit codes agent runners can trust: `punk session` exits 2 only when a person must act, and says why.

- `punk session` exits 2 ("a person must act") when the store cannot merge its remote, instead of 0
  with only "could not merge origin main:". The message names the cause: two stores started
  separately (no shared history, with the fix), or the files both sides changed (#51)
- An encrypted store without `age` on PATH: `session start` restores from the local store and
  `session end` saves locally, both say `age` is missing and exit 2; `punk sync` stops with that
  error instead of a traceback (#49, #50)
- `punk session` exits 1, not 2, for a usage error (a bad `--agent` id or `PUNK_AGENT`, an unknown
  step or option), so 2 always means a person must act. Other commands keep exit 2
- README: the Claude Code plugin install also in two steps, `/plugin marketplace add
  gablooge/punk-records` then `/plugin install punk-records@punk-records`, which works in every
  Claude Code version; the one-step `--marketplace` form needs 2.1.275 or newer

## 0.6.0 - 2026-11-23

For agent fleets: several punk processes can share a store, each change says which agent made it,
and `punk session` gives any runner what the Claude Code hooks do.

- README: "Agent fleets", how unattended agents use `punk session`, `--agent` and the store lock together
- `--agent ID` (or `PUNK_AGENT`) records which agent made a change: every commit it makes ends with a
  `Punk-Agent: ID` git trailer
- `punk session start|end [--project PATH]`: what the Claude Code hooks do, for any agent runner (cron,
  CI, a scheduler), with exit codes it can act on: 0 done, 2 a person must resolve a conflict, 1 failed
- `docs/store-format.md`: the store's layout (branch, `records/`, `satellites/*.json`, history, encrypted
  remotes) for tools that read a store without punk, kept true by tests
- Commands that change the store take a lock, so several punk processes on one machine (cron agents,
  hooks, the MCP server) no longer fail with git's `index.lock` error or lose a change. They wait
  up to 60 s (`PUNK_LOCK_TIMEOUT`; hooks 20 s), then stop with a clear error

## 0.5.0 - 2026-11-22

Easier to find and set up: the MCP Registry, a Claude Code plugin, and a `punk-records` command.

- Listed in the MCP Registry as `io.github.gablooge/punk-records` (`server.json`; published by the
  release workflow after PyPI, over GitHub OIDC)
- `punk-records` is also installed as a command (same as `punk`), so `uvx punk-records mcp` works
- Claude Code plugin: `/plugin install punk-records --marketplace gablooge/punk-records` adds the
  MCP server and the session hooks in one step, plus a `setup` skill
- README: the Punk Records VS Code extension (Marketplace and Open VSX)

## 0.4.0 - 2026-11-21

JSON output for editor integrations, Claude Code auto memory, and safer moves.

- A moved project no longer loses its memories: when the old folder is no longer a clone of the
  satellite's git remote (an empty folder left behind, or another repo), `save` and `restore` skip
  it as `missing` and point to `punk track <new path>`, instead of deleting every memory from the store
- `punk status --json` and `punk list --json` (satellites, local paths, memory files, team
  subscriptions) for editor integrations; the JSON carries `"version": 1`
- Claude Code auto memory (`~/.claude/projects/<project>/memory/*.md`) is saved with its project as
  `@claude-memory/...` and restored into the right folder on every machine, whatever the checkout
  path; worktrees share the main checkout's. Opt out with `"exclude": ["@claude-memory/*"]`
- `punk show @team/<satellite> <path>` prints a memory from a subscribed team store
- `punk show <satellite> <path>` writes the stored bytes when piped: no more `\r\n` for every `\n`
  on Windows, and files saved with `\r\n` keep them (also in MCP `read_memory`)
- README: using punk from VS Code (the Claude Code extension, and Copilot agent mode over MCP)

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
