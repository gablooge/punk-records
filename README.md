# Punk Records

[![PyPI](https://img.shields.io/pypi/v/punk-records)](https://pypi.org/project/punk-records/)
[![Python](https://img.shields.io/pypi/pyversions/punk-records)](https://pypi.org/project/punk-records/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/gablooge/punk-records/blob/main/LICENSE)

**One brain for all your AI agents.** Persist every `CLAUDE.md`, `AGENTS.md`, napkin, and rules file
from every project and machine in a single versioned store. Sync it over any git remote, or carry it
across an air gap on a USB stick. Agents can read and write it live over MCP.

> On Egghead, Dr. Vegapunk keeps one shared brain, the Punk Records. His satellites each go off
> and learn things, and whatever they learn flows back into that shared brain. This project does the
> same for your agents: each project and machine is a satellite, and `~/.punk-records` is the brain.

## Why

Agent memories are scattered across dozens of repos and dotfiles. Many of them are deliberately
uncommitted (`CLAUDE.local.md`, napkins, `~/.claude/CLAUDE.md`). They get lost when you wipe a
laptop, they diverge between your work and home machines, and they can't follow you into an
offline network.

## What it picks up

| Scope | Files |
|---|---|
| Any project | `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `AGENT.md`, `GEMINI.md`, `napkin.md`, `CONVENTIONS.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`, `.goosehints` (anywhere in the tree) |
| | `.claude/*.md` (napkins, commands, agents), `.claude/skills/*`, `.cursor/rules/*`, `.windsurf/rules/*`, `.clinerules/*`, `.roo/rules/*`, `.kiro/steering/*`, `.github/copilot-instructions.md`, `.github/instructions/*.md` |
| Global (`$HOME`) | `~/.claude/CLAUDE.md`, `~/.claude/{commands,agents,skills}/*`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md` |

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

### Let agents use it live (MCP)

```bash
claude mcp add punk-records -- punk mcp
```

Any MCP client works the same way (`"command": "punk", "args": ["mcp"]`). Tools offered:
`get_context`, `search_memories`, `read_memory`, `list_memories`, `list_satellites`,
`append_memory` (writes to the store, commits, and updates the local file), and `sync`.

From the shell: `punk remember "- integration tests need docker running"` appends to the current
project's `CLAUDE.md` (use `-f .claude/napkin.md` to target another file).

## How sync works

- **The store is a git repo.** Every save is a commit, so `punk git log -p` shows how your agents'
  knowledge changed over time, and nothing is ever truly lost.
- **Three-way per file.** Punk records the local and store hash of each file at its last sync, which
  lets it tell "edited here" apart from "edited on another machine". When both sides changed, it
  reports a **conflict** and leaves both copies alone. You resolve it with `save --force` (local wins)
  or `restore --force` (store wins, and your local copy is kept as `.punk-bak`).
- **Concurrent appends merge.** Memory files use git's `union` merge driver, so when two machines
  add lines to the same `CLAUDE.md`, the result contains both sets of lines.
- **Deletes propagate on save.** Locally removing a file that was synced removes it from the store.
  Deletions coming *from* the store are only reported, unless you pass `restore --prune`.
- **Symlinked memories are kept as links.** If `AGENTS.md -> CLAUDE.md`, the content is stored once
  and the link is recorded in `satellites/<id>.json`, then recreated on other machines. Only links
  that resolve to a memory file inside the same project are recorded. Links that point outside the
  project are never followed. `punk scan` and `punk save` warn about them instead.

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
sync              save, pull/push remote, restore
export | import   air-gapped bundle files
search | show | context | remember
git ...           run git inside the store
mcp               MCP server on stdio
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

MIT licensed.
