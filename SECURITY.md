# Security policy

Punk Records copies files between your projects, your machines and a git remote, so a mistake can
leak private notes or let someone write files on your computer. Reports are taken seriously.

## Reporting a vulnerability

Please report privately through GitHub:
**[Report a vulnerability](https://github.com/gablooge/punk-records/security/advisories/new)**
(the Security tab of this repository, then "Report a vulnerability").

Do not open a public issue for a security problem. Include the version (`punk --version`), your
OS, and the smallest steps that show the problem. You should get a first reply within a week.

## Supported versions

Only the latest release gets security fixes. Releases on PyPI are never replaced; a fix ships as a
new version.

## What is in scope

These guarantees must always hold. Breaking any of them is a vulnerability:

1. **Writes stay inside memory paths.** `restore`, `append` (`punk remember`, MCP
   `append_memory`) and `sync` only write files that match the memory patterns, never a path with
   a `.git` or `..` segment, never outside the project (or `$HOME` for global memories), and never
   through a symlink other than a followed dotfile link as documented in the README. A tampered
   store, bundle or `punk.json` must not be able to plant git hooks, scripts or other files.
2. **Recreated symlinks stay inside.** Links restored from the store only point from one memory
   file to another inside the same project.
3. **Secrets are scanned before storage.** `save` and `append` check text for API keys, private
   keys and similar before anything enters the store, unless the user passes `--allow-secrets`.
4. **Machine-local state stays local.** Paths and sync hashes live only in
   `.git/punk-state.json` inside the store and are never committed or pushed.
5. **Conflicts are never resolved destructively.** When a file changed in two places, both
   copies are kept until the user chooses, and `--force` keeps a `.punk-bak` backup.
6. **`punk diff` and reads stay inside memory paths.** A tampered store must not make punk print
   arbitrary local files.

## Out of scope

- Who can read your git remote. Keep it private; for third-party hosts, consider encrypting the
  store with [git-crypt](https://github.com/AGWA/git-crypt).
- Secret formats the scanner does not know. Detection is best effort; please open a normal issue
  with a (fake) example so a pattern can be added.
- Anything that requires write access to your machine or your store's `.git` directory already.
