---
description: Set up Punk Records for this plugin. Use when the user asks to set up or install punk records, or when the punk-records MCP server or the punk hooks fail because `punk` is missing, too old, or has no store.
---

The punk-records plugin runs the `punk` command; it does not install it. Set it up step by step and
show the user each command's output:

1. Run `punk --version`. It must print 0.4.0 or newer.
   - Not found: install it with `pipx install punk-records` (or `uv tool install punk-records`).
   - Older: `pipx upgrade punk-records`.
2. If there is no store yet (`punk status` says "run `punk init` first"), ask whether the user
   already keeps their memories in a git repository. If so, run `punk init <that repo URL>`;
   otherwise run `punk init`, and offer to add a private git repo later for syncing between
   machines: `punk git remote add origin <url>`, then `punk sync`.
3. Track the current project with `punk track .`, then run `punk save` and `punk status`.
4. If the user ran `punk install-hooks` before, run `punk install-hooks --uninstall`: this plugin
   already provides the same session hooks, and keeping both would restore and save twice.
5. Tell the user to run `/reload-plugins` (or start a new session) so the MCP server connects.

Never pass `--allow-secrets`, `--force` or `--allow-sync` unless the user asks for it.
