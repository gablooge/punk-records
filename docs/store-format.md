# Store format

What a Punk Records store looks like on disk and on its git remote, for tools that read a store
without running `punk` (for example a dashboard reading it through the GitHub API). `punk` itself is
the only writer. This page describes format version 1; `tests/test_punk.py` (`TestStoreFormat`)
fails if the code and this page drift apart.

## The repository

A store is a git repository, by default at `~/.punk-records` (`PUNK_HOME` or `--home` move it).
`punk init` creates it on the branch `main`. A store cloned with `punk init <url>` keeps the
remote's default branch, which is `main` for every store `punk` created.

Everything outside `.git/` is versioned and synced:

```
.gitattributes                      line endings and merges, see below
README.md                           a short note on what the repository is
punk.json                           shared settings: {"include": [...], "exclude": [...]}
satellites/<satellite id>.json      one file per tracked project or global scope
records/<satellite id>/<path>       the memory files themselves
```

`.git/punk-state.json` is machine-local (where each satellite lives on this machine, and the hashes
seen at the last sync). It is never committed or pushed, so a reader of the remote never sees it.

## Satellite ids

A satellite is one tracked project, or the global scope (`$HOME`, id `global`). A project's id comes
from its git remote, normalised: `git@github.com:Acme/Web.git` and `https://github.com/acme/web`
both become `github.com/acme/web`. A project with no remote is `local/<folder name>` (lowercased,
with characters other than letters, digits, `.`, `_` and `-` turned into `-`), and
`punk track --id` sets any id.

An id matches `[A-Za-z0-9._/-]+`, is not absolute, does not start with `.`, and has no empty, `.`
or `..` segment. Its `/` separators are directories, so `github.com/acme/web` is stored under
`records/github.com/acme/web/` and described by `satellites/github.com/acme/web.json`.

## `satellites/<id>.json`

```json
{
  "name": "web",
  "kind": "project",
  "remote": "git@github.com:acme/web.git",
  "links": {"AGENTS.md": "CLAUDE.md"}
}
```

| Key | Present | Meaning |
|---|---|---|
| `name` | always | the folder name when first tracked; `global` for the global scope |
| `kind` | always | `project` or `global` |
| `remote` | projects with a git remote | the project's `remote.origin.url` as it was when first tracked |
| `links` | when the project has symlinked memories | `{link path: target path}`, both relative to the project; the link itself is not stored under `records/` |

Readers should ignore keys they do not know.

## `records/<id>/<path>`

Each memory file, byte for byte, at its path relative to the project (or to `$HOME` for `global`):
`records/github.com/acme/web/CLAUDE.md`, `records/github.com/acme/web/.claude/agents/reviewer.md`,
`records/global/.claude/CLAUDE.md`. Claude Code auto memory is stored under the project as
`@claude-memory/<file>.md`.

`.gitattributes` keeps every file exactly as written (`* -text`) and merges edits to `records/**`
from two machines by keeping both sides' lines (`merge=union`).

## History

Every change is a commit on the branch. The messages `punk` writes start with what happened:
`init punk records on <machine>`, `track <id>`, `save ...`, `remember in <id>/<path>`,
`write <id>/<path>`, `dedupe ...`, `purge <id>`. The commit author is the machine's git identity,
or `punk@<machine>` when it has none. The history of one satellite is the history of
`records/<id>/` (and its `satellites/<id>.json`).

## Encrypted remotes

With `punk encrypt`, the local store is unchanged but its remote never holds this layout: the
remote branch `main` has a single file, `punk-records.bundle.age`, an age-encrypted git bundle of
the store, in a commit with no parent. A reader that finds that file and no `records/` is looking at
an encrypted store and cannot read it without the machine's age key.
