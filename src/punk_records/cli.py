"""`punk` command line."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .discover import discover, scan
from .store import GLOBAL_ID, PunkError, Store, derive_id


def _report(actions, quiet_ok: str = "nothing to do") -> int:
    for a in actions:
        print(a)
    if not actions:
        print(quiet_ok)
    bad = [a for a in actions if a.kind in ("conflict", "blocked")]
    if bad:
        print(f"\n{len(bad)} file(s) need attention. Conflicts: see what differs with `punk diff`, then "
              f"use --force on save (local wins in the store) or restore (local gets the store copy, "
              f"with a .punk-bak backup). Secrets: edit them out, add `punk:allow` on the line, or use "
              f"--redact.", file=sys.stderr)
        for c in (a for a in bad if a.kind == "conflict" and a.rel):
            print(f"  punk diff {c.sat} {c.rel}", file=sys.stderr)
        return 1
    return 0


def cmd_init(store: Store, a) -> int:
    store.init(remote=a.remote, machine=a.machine, track_global=not a.no_global)
    print(f"Punk Records ready at {store.root}")
    if a.remote:
        print("Cloned from remote. Track your projects with `punk track <dir>`, then `punk sync`.")
    else:
        print("Next: `punk track <project-dir>` and `punk save`. Add a private git remote with\n"
              f"  punk git remote add origin <url>\nthen `punk sync`.")
    return 0


def cmd_track(store: Store, a) -> int:
    paths = a.paths or ["."]
    if a.id and len(paths) > 1:
        raise PunkError("--id works with a single path")
    for p in paths:
        sid = store.track(Path(p), sid=a.id)
        n = len(discover(Path(p).resolve(), store._kind(sid), store.config()))
        print(f"tracking {sid}  <-  {Path(p).resolve()}  ({n} memory files)")
    return 0


def cmd_untrack(store: Store, a) -> int:
    store.untrack(a.id, purge=a.purge)
    print(f"untracked {a.id}" + (" and removed its records" if a.purge else ""))
    return 0


def cmd_list(store: Store, a) -> int:
    store.require()
    paths = store.load_state()["paths"]
    sats = store.satellites()
    for sid in sorted(set(sats) | set(paths)):
        n = len(store.memories(sid)) if sid in sats else 0
        where = paths.get(sid, "(not on this machine)")
        print(f"{sid:<45} {n:>3} file{'' if n == 1 else 's'}  {where}")
    return 0


def cmd_scan(store: Store, a) -> int:
    root = Path(a.path).expanduser().resolve()
    kind = "global" if a.global_ else "project"
    cfg = store.config() if store.exists() else {}
    found = scan(Path.home() if a.global_ else root, kind, cfg)
    print(f"# {GLOBAL_ID if a.global_ else derive_id(root)}")
    for f in found.files:
        via = found.followed.get(f)
        print(f"  {f}" + (f"  (follows symlink to {via})" if via else ""))
    for rel, target in found.links.items():
        print(f"  {rel} -> {target}  (symlink)")
    if not found.files and not found.links:
        print("  (no memory files found)")
    for rel, why in found.warnings:
        print(f"punk: warning: {rel}: {why}", file=sys.stderr)
    return 0


def _save_kw(a) -> dict:
    return dict(redact=a.redact, allow_secrets=a.allow_secrets)


def cmd_save(store: Store, a) -> int:
    return _report(store.save(a.ids or None, message=a.message, force=a.force, dry_run=a.dry_run,
                              **_save_kw(a)), "all memories already saved")


def cmd_restore(store: Store, a) -> int:
    return _report(store.restore(a.ids or None, force=a.force, prune=a.prune, dry_run=a.dry_run),
                   "all local memories up to date")


def cmd_status(store: Store, a) -> int:
    store.require()
    print("Local changes `punk save` would store:")
    s = store.save(dry_run=True)
    _report(s, "  none")
    print("\nStore changes `punk restore` would write here:")
    r = store.restore(dry_run=True)
    _report(r, "  none")
    remote = store.remote()
    print(f"\nremote: {remote or '(none - offline / air-gapped mode)'}")
    return 0


def cmd_sync(store: Store, a) -> int:
    return _report(store.sync(**_save_kw(a)), "in sync")


def cmd_diff(store: Store, a) -> int:
    diffs = store.diff([a.satellite] if a.satellite else None, a.path)
    for d in diffs:
        sys.stdout.write(d.text)
    if not diffs:
        print("no differences")
    return 1 if diffs else 0


def cmd_export(store: Store, a) -> int:
    path = store.export_bundle(Path(a.file))
    print(f"wrote {path} ({path.stat().st_size:,} bytes). Carry it over and run `punk import {path.name}`.")
    return 0


def cmd_import(store: Store, a) -> int:
    return _report(store.import_bundle(Path(a.file), **_save_kw(a)), "imported; already up to date")


def cmd_search(store: Store, a) -> int:
    store.require()
    hits = 0
    for sid, rel, n, line in store.search(a.query, regex=a.regex, sid=a.satellite):
        print(f"{sid}/{rel}:{n}: {line.strip()}")
        hits += 1
    return 0 if hits else 1


def cmd_show(store: Store, a) -> int:
    store.require()
    if a.path:
        sys.stdout.write(store.read(a.satellite, a.path))
        return 0
    for sid, rel in store.memories(a.satellite):
        print(f"{sid}/{rel}")
    return 0


def cmd_context(store: Store, a) -> int:
    store.require()
    sys.stdout.write(store.context(Path(a.path)))
    return 0


def cmd_remember(store: Store, a) -> int:
    sid = a.satellite or store.satellite_for(Path.cwd()) or GLOBAL_ID
    text = " ".join(a.text) if a.text else sys.stdin.read()
    written = store.append(sid, a.file, text)
    print(f"remembered in {sid}/{written}")
    return 0


def cmd_git(store: Store, a) -> int:
    store.require()
    return subprocess.call(["git", "-C", str(store.root), *a.args])


def cmd_mcp(store: Store, a) -> int:
    from .mcp import serve
    store.require()
    serve(store)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="punk", description="Punk Records - one brain for all your AI agents' "
                                "memories (CLAUDE.md, AGENTS.md, napkins, rules...), synced online or air-gapped.")
    p.add_argument("--home", help="store location (default: $PUNK_HOME or ~/.punk-records)")
    p.add_argument("--version", action="version", version=f"punk {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help, aliases=()):
        sp = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        sp.set_defaults(fn=fn)
        return sp

    def secret_opts(sp):
        sp.add_argument("--redact", action="store_true", help="store files with secrets redacted")
        sp.add_argument("--allow-secrets", action="store_true", help="skip secret scanning (not recommended)")

    sp = add("init", cmd_init, "create a store, or clone one from a git remote")
    sp.add_argument("remote", nargs="?", help="git URL or path of an existing store to clone")
    sp.add_argument("--machine", help="name for this machine (default: hostname)")
    sp.add_argument("--no-global", action="store_true", help="don't track ~/.claude, ~/.codex, ...")

    sp = add("track", cmd_track, "start persisting a project's memories")
    sp.add_argument("paths", nargs="*", help="project directories (default: .)")
    sp.add_argument("--id", help="override the satellite id (default: from the git remote)")

    sp = add("untrack", cmd_untrack, "stop syncing a project on this machine")
    sp.add_argument("id")
    sp.add_argument("--purge", action="store_true", help="also delete its records from the store")

    add("list", cmd_list, "list satellites (projects) and where they live here", aliases=["ls"])

    sp = add("scan", cmd_scan, "show which memory files would be picked up")
    sp.add_argument("path", nargs="?", default=".")
    sp.add_argument("--global", dest="global_", action="store_true", help="scan user-level memories")

    sp = add("save", cmd_save, "copy local memories into the store and commit")
    sp.add_argument("ids", nargs="*")
    sp.add_argument("-m", "--message")
    sp.add_argument("--force", action="store_true", help="on conflict, the local copy wins")
    sp.add_argument("-n", "--dry-run", action="store_true")
    secret_opts(sp)

    sp = add("restore", cmd_restore, "write store memories into local projects")
    sp.add_argument("ids", nargs="*")
    sp.add_argument("--force", action="store_true", help="on conflict, the store wins (local kept as .punk-bak)")
    sp.add_argument("--prune", action="store_true", help="delete local files removed from the store")
    sp.add_argument("-n", "--dry-run", action="store_true")

    add("status", cmd_status, "what save/restore would do")

    sp = add("diff", cmd_diff, "show how local memories differ from the store (exit 1 if they do)")
    sp.add_argument("satellite", nargs="?", help="satellite id (default: all tracked here)")
    sp.add_argument("path", nargs="?", help="one memory file, e.g. CLAUDE.md")

    sp = add("sync", cmd_sync, "save, pull + push the git remote (if any), restore")
    secret_opts(sp)

    sp = add("export", cmd_export, "write the store to a single bundle file (air-gapped transfer)")
    sp.add_argument("file")

    sp = add("import", cmd_import, "merge a bundle file made by `punk export`")
    sp.add_argument("file")
    secret_opts(sp)

    sp = add("search", cmd_search, "search all memories")
    sp.add_argument("query")
    sp.add_argument("-e", "--regex", action="store_true")
    sp.add_argument("-s", "--satellite")

    sp = add("show", cmd_show, "list a satellite's memories, or print one")
    sp.add_argument("satellite", nargs="?")
    sp.add_argument("path", nargs="?")

    sp = add("context", cmd_context, "print all memories relevant to a directory (pipe it to any LLM)")
    sp.add_argument("path", nargs="?", default=".")

    sp = add("remember", cmd_remember, "append a note to a memory file")
    sp.add_argument("text", nargs="*", help="note text (default: read stdin)")
    sp.add_argument("-s", "--satellite", help="default: project of the current dir, else global")
    sp.add_argument("-f", "--file", default="CLAUDE.md", help="memory file (default: CLAUDE.md)")

    sp = add("git", cmd_git, "run any git command inside the store")
    sp.add_argument("args", nargs=argparse.REMAINDER)

    add("mcp", cmd_mcp, "run the MCP server on stdio (for Claude Code, Cursor, ...)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    if not getattr(a, "fn", None):
        parser.print_help()
        return 0
    store = Store(a.home or os.environ.get("PUNK_HOME"))
    try:
        return a.fn(store, a)
    except PunkError as e:
        print(f"punk: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
