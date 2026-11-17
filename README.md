# Punk Records

[![CI](https://github.com/gablooge/punk-records/actions/workflows/ci.yml/badge.svg)](https://github.com/gablooge/punk-records/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/punk-records)](https://pypi.org/project/punk-records/)
[![Python](https://img.shields.io/pypi/pyversions/punk-records)](https://pypi.org/project/punk-records/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/gablooge/punk-records/blob/main/LICENSE)

**One brain for all your AI agents.** Persist every `CLAUDE.md`, `AGENTS.md`, napkin, and rules file
from every project and machine in a single versioned store. Sync it over any git remote, or carry it
across an air gap on a USB stick. Agents can read and write it live over MCP.

![punk init, track, save, remember and context in a terminal](https://raw.githubusercontent.com/gablooge/punk-records/main/docs/demo.gif)

> On [Egghead](https://onepiece.fandom.com/wiki/Egghead), Dr.
> [Vegapunk](https://onepiece.fandom.com/wiki/Vegapunk) keeps one shared brain, the
> [Punk Records](https://onepiece.fandom.com/wiki/Punk_Records). His satellites each go off
> and learn things, and whatever they learn flows back into that shared brain. This project does the
> same for your agents: each project and machine is a satellite, and `~/.punk-records` is the brain.
>
> <sub>The name is a nod to *One Piece* (the linked wiki pages contain spoilers). This is a fan-named
> project, not affiliated with Eiichiro Oda, Shueisha or Toei Animation.</sub>

## Why

Agent memories are scattered across dozens of repos and dotfiles. Many of them are deliberately
uncommitted (`CLAUDE.local.md`, napkins, `~/.claude/CLAUDE.md`). They get lost when you wipe a
laptop, they diverge between your work and home machines, and they can't follow you into an
offline network.

## How it compares

- **Committing `CLAUDE.md` to each repo.** Keep doing it for conventions your team shares. It does not
  cover the memories you keep out of git on purpose (`CLAUDE.local.md`, napkins, `~/.claude`), notes
  you would rather not push to a team repo, or a machine that cannot reach the repo host. Punk
  Records stores those too, alongside the committed ones.
- **Dotfiles managers** (chezmoi, yadm, GNU Stow). They are built for files at fixed paths in your home
  directory. Most agent memories live inside project checkouts, at different paths on each machine.
  Punk Records finds them by itself, matches checkouts by their git remote, merges edits made on two
  machines, scans for secrets before storing, and serves everything to agents over MCP. The two work
  together: a global memory that is a symlink into your dotfiles repo is followed (see below).

## What it picks up

| Scope | Files |
|---|---|
| Any project | `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `AGENT.md`, `GEMINI.md`, `napkin.md`, `CONVENTIONS.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`, `.goosehints` (anywhere in the tree) |
| | `.claude/*.md` (napkins, commands, agents), `.claude/skills/*`, `.cursor/rules/*`, `.windsurf/rules/*`, `.clinerules/*`, `.roo/rules/*`, `.kiro/steering/*`, `.github/copilot-instructions.md`, `.github/instructions/*.md` |
| Global (`$HOME`) | `~/.claude/CLAUDE.md`, `~/.claude/napkin.md`, `~/.claude/{commands,agents,skills}/*`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`, `~/.config/goose/.goosehints` |

Run `punk scan` to see what would be captured. To add your own patterns, put globs in `punk.json`
inside the store (`{"include": ["docs/ai/*.md"], "exclude": []}`).

## Install

Requires Python 3.9+ and git. No other dependencies.

```bash
pipx install punk-records      # or: pip install punk-records
```

## Quickstart

```bash
punk init                       # creates ~/.punk-records and tracks your global memories
punk track ~/code/app ~/code/api
punk save                       # snapshot every memory file into the store
punk context ~/code/app         # everything an agent should know there, as one document
```

### Online sync (any git host)

Create a **private** repo (GitHub, GitLab, Gitea, a bare repo on your NAS, anything git can reach):

```bash
punk git remote add origin git@github.com:you/my-punk-records.git
punk sync                       # save -> pull -> push -> restore
```

On another machine:

```bash
punk init git@github.com:you/my-punk-records.git
punk track ~/src/app            # matched to the same records through the project's git remote
punk sync
```

Projects get their identity from their git remote (`github.com/you/app`), so the same repo checked out
at different paths on different machines shares one set of memories. Projects without a remote get
`local/<dirname>`; you can override it with `punk track --id`.

### Air-gapped sync

```bash
punk export /media/usb/records.bundle     # on the connected side
punk import /media/usb/records.bundle     # on the air-gapped side (and back again)
```

A bundle is a standard `git bundle` file. It carries full history and can be verified on arrival, and
imports merge the same way online pulls do.

## Use it with your AI tools

### Let your agent set it up

Paste one of these into Claude Code, Cursor, Codex or any agent that can run shell commands:

> Install https://pypi.org/project/punk-records with pipx, run `punk init`, track this repo with
> `punk track .`, save, and register its MCP server for Claude Code for all my projects
> (`claude mcp add --scope user punk-records -- punk mcp`). Then run `punk scan` and tell me which
> memory files it found.

> I'm setting up a new laptop. Use https://pypi.org/project/punk-records to bring back my agent
> memories: `punk init git@github.com:me/my-punk-records.git`, then track every repo under ~/code
> and run `punk sync`.

> Use punk-records (https://pypi.org/project/punk-records) to keep this project's CLAUDE.md and
> AGENTS.md in sync across my machines automatically: run `punk install-hooks`.

### Claude Code

```bash
claude mcp add --scope user punk-records -- punk mcp   # all projects; without --scope: this project only
punk install-hooks                                     # restore at session start, save + sync at the end
```

There is no server to keep running: Claude Code starts `punk mcp` when a session begins and talks to
it over stdin/stdout. Run `punk init` once first. If `punk` is not found, use the full path from
`which punk`.

### Cursor, Codex and other MCP clients

Cursor: add to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{ "mcpServers": { "punk-records": { "command": "punk", "args": ["mcp"] } } }
```

Codex (CLI, IDE extension and ChatGPT desktop share this):

```bash
codex mcp add punk-records -- punk mcp     # or [mcp_servers.punk-records] in ~/.codex/config.toml
```

Any other MCP client works the same way: a stdio server with `"command": "punk"` and
`"args": ["mcp"]`.

### Things to ask your agent

| You say | The agent uses |
|---|---|
| "Before you start, check punk records for what I already know about this repo." | `get_context` |
| "Search punk records across all my projects: how did I set up GitHub Actions caching?" | `search_memories` |
| "Which projects do I have memories for?" | `list_satellites`, `list_memories` |
| "Show me the napkin for github.com/me/api." | `read_memory` |
| "Remember in punk records: integration tests need Docker running." | `append_memory` |
| "Before we finish, save the gotchas you hit today to this project's CLAUDE.md in punk records." | `append_memory` |
| "Sync punk records." | `sync` |

`append_memory` writes to the store, commits, and updates the local file. Secrets are refused, and it
only writes memory files (CLAUDE.md, AGENTS.md, napkins, rules).

### AI tools without MCP

ChatGPT, Claude.ai, Gemini on the web or a local model can still use your memories as plain text:

```bash
punk context . | pbcopy          # macOS; on Linux: | xclip -selection clipboard
punk context ~/code/api > notes.md
```

Then start the conversation with something like:

> Here are my notes for this project (global preferences first, then project rules). Follow them,
> and tell me if anything you do contradicts them.

### Sync automatically with Claude Code

```bash
punk install-hooks              # all projects (~/.claude/settings.json)
punk install-hooks --project    # only this project (.claude/settings.local.json)
```

This adds two [Claude Code hooks](https://code.claude.com/docs/en/hooks): when a session starts, punk
restores your global memories and the current project's (pulling first when a remote is set); when
it ends, punk saves them and syncs. Nothing is printed into the conversation unless something needs
your attention, such as a conflict or a blocked secret. Your other settings and hooks are left alone,
the previous file is kept as `.punk-bak`, and `punk install-hooks --uninstall` removes only punk's
entries. Only tracked projects are synced.

### Save as you go

```bash
punk watch                      # save memory files a moment after they change (Ctrl+C to stop)
punk watch --sync-every 300     # ...and pull/push every 5 minutes when a remote is set
```

`punk watch` checks the memory files it knows every 2 seconds (`--interval`) and saves once they have
been quiet for 2 seconds (`--debounce`), so a burst of edits becomes one commit. New memory files are
found by a full rescan every 30 seconds (`--rescan`). No file-system events, no extra dependencies.

From the shell: `punk remember "- integration tests need docker running"` appends to the current
project's `CLAUDE.md` (use `-f .claude/napkin.md` to target another file).

## How sync works

- **The store is a git repo.** Every save is a commit, so `punk git log -p` shows how your agents'
  knowledge changed over time, and nothing is ever truly lost.
- **Three-way per file.** Punk records the local and store hash of each file at its last sync, which
  lets it tell "edited here" apart from "edited on another machine". When both sides changed, it
  reports a **conflict** and leaves both copies alone. `punk diff <satellite> <path>` shows what
  differs (`-` store, `+` local). You resolve it with `save --force` (local wins) or
  `restore --force` (store wins, and your local copy is kept as `.punk-bak`).
- **Concurrent appends merge.** Memory files use git's `union` merge driver, so when two machines
  add lines to the same `CLAUDE.md`, the result contains both sets of lines. The catch: if both
  machines change the *same* line, the merged file keeps both versions, one after the other. Nothing
  is lost, but read the file after such a sync and delete the line you no longer want;
  `punk git log -p` shows which machine wrote which. When two machines add the *same* line in
  different places, both copies stay; `punk dedupe` lists them and `punk dedupe --apply` keeps only
  the first (blank lines, code blocks, indented lines and headings are never touched).
- **Deletes propagate on save.** Locally removing a file that was synced removes it from the store.
  Deletions coming *from* the store are only reported, unless you pass `restore --prune`.
- **Symlinked memories are kept as links.** If `AGENTS.md -> CLAUDE.md`, the content is stored once
  and the link is recorded in `satellites/<id>.json`, then recreated on other machines. Only links
  that resolve to a memory file inside the same project are recorded. Links that point outside the
  project are never followed. `punk scan` and `punk save` warn about them instead.
- **Dotfiles work.** For global memories, a link such as `~/.claude/CLAUDE.md ->
  ~/dotfiles/claude/CLAUDE.md` is followed: the content is stored, and restores write through the
  link into your dotfiles repo. Only non-executable Markdown/text files (or files with the same name
  as the link) inside `$HOME` are followed, never anything under `.git`, `.ssh`, `.gnupg`, `.aws`
  and similar.

## Safety

- **Secret scanning.** Before a file enters the store it is checked for API keys (Anthropic, OpenAI,
  AWS, GitHub, Slack, Google, Stripe), private keys, and `password=...`-style assignments. A matching
  file is blocked by default. You can pass `--redact` to store it with secrets replaced, or put
  `punk:allow` on a line to mark a false positive.
- **Restricted writes.** `restore` only writes paths that match the memory patterns. It never writes
  into `.git/`, outside the project, through `..`, or through an existing symlink. Recreated symlinks
  must point from one memory path to another inside the project, so a tampered store or bundle can't
  plant hooks or scripts in your repos.
- **Keep the remote private.** Memories describe your code and habits. If you need encryption at
  rest on a third-party host, [git-crypt](https://github.com/AGWA/git-crypt) works transparently
  on the store repo.

## Commands

```
init [URL]        create a store, or clone an existing one
track [DIR...]    start persisting a project      untrack ID [--purge]
list | scan       what is tracked / what would be captured
save | restore    store <-> local projects (--dry-run, --force)
status            what save/restore would do
diff [ID] [PATH]  unified diff of local memories vs the store
dedupe [ID] [PATH] duplicate lines left by merges (--apply removes them)
sync              save, pull/push remote, restore
export | import   air-gapped bundle files
search | show | context | remember
git ...           run git inside the store
mcp               MCP server on stdio
install-hooks     sync automatically when Claude Code sessions start and end
watch             save automatically when memory files change
```

## Roadmap

- Built-in encryption (age) for untrusted remotes
- `punk watch`: save automatically when memory files change
- Semantic search and deduplication across projects
- Shared "team brain" satellites that you can subscribe to read-only
- Small web UI for browsing memory history

## Development

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](https://github.com/gablooge/punk-records/blob/main/CONTRIBUTING.md) for the rules (no runtime dependencies, Python 3.9 syntax,
tests for every change). Report security problems privately as described in
[SECURITY.md](https://github.com/gablooge/punk-records/blob/main/SECURITY.md).

MIT licensed.
