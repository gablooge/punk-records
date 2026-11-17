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
              "  punk git remote add origin <url>\nthen `punk sync`.")
    return 0


def cmd_track(store: Store, a) -> int:
    paths = a.paths or ["."]
    if a.id and len(paths) > 1:
        raise PunkError("--id works with a single path")
    for p in paths:
        sid = store.track(Path(p), sid=a.id)
        n = len(discover(Path(p).resolve(), store._kind(sid), store.config()))
        print(f"tracking {sid}  <-  {Path(p).resolve()}  ({n} memory file{'' if n == 1 else 's'})")
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


def cmd_dedupe(store: Store, a) -> int:
    from . import dedupe
    found = list(dedupe.find(store, [a.satellite] if a.satellite else None, a.path))
    if not found:
        print("no duplicate lines")
        return 0
    for sid, rel, _, _, removed in found:
        for n, text in removed:
            print(f"{sid}/{rel}:{n}: {text}")
    lines, files = sum(len(x[4]) for x in found), len(found)
    summary = f"{lines} duplicate line{'' if lines == 1 else 's'} in {files} file{'' if files == 1 else 's'}"
    if not a.apply:
        print(f"\n{summary}; the first copy of each line is kept. Run `punk dedupe --apply` to remove them.")
        return 1
    print(f"\nremoved {summary} in the store")
    return _report(dedupe.apply(store, found), "local copies updated")


def cmd_watch(store: Store, a) -> int:
    from .watch import Watcher
    store.require()
    if a.ids:
        store._local_ids(store.load_state(), a.ids)  # unknown ids are an error, not silently ignored
    w = Watcher(store, a.ids or None, interval=a.interval, debounce=a.debounce, sync_every=a.sync_every)
    n = len(w.satellites())
    extra = f", syncing every {a.sync_every:g}s" if a.sync_every and store.remote() else ""
    print(f"watching {n} satellite{'' if n == 1 else 's'} every {a.interval:g}s{extra} (Ctrl+C to stop)")
    try:
        w.run()
    except KeyboardInterrupt:
        print("stopped")
    return 0


def _seconds(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _interval(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be more than 0")
    return value


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


def cmd_install_hooks(store: Store, a) -> int:
    from . import hooks
    if not a.uninstall:
        store.require()
    project = Path(a.project).expanduser().resolve() if a.project else None
    path, status = hooks.install(store, project, uninstall=a.uninstall, dry_run=a.dry_run)
    verb = {"installed": "installed", "updated": "updated", "removed": "removed",
            "unchanged": "nothing to remove" if a.uninstall else "already installed"}[status]
    print(f"{'would be ' if a.dry_run and status != 'unchanged' else ''}{verb}: Claude Code hooks in {path}")
    if a.uninstall or status == "unchanged":
        return 0
    print("  session start: restore this project's and your global memories (pull first if online)\n"
          "  session end:   save them, then pull and push if a remote is set")
    if not hooks.runnable():
        print(f"  warning: {sys.executable} cannot import punk_records without PYTHONPATH, so the hooks would "
              f"fail; install punk with `pipx install punk-records` (or `pip install .`) and run this again",
              file=sys.stderr)
    if project and hooks.gitignored(project, ".claude/settings.local.json") is False:
        print(f"  note: add .claude/settings.local.json to {project / '.gitignore'}; it holds paths for "
              f"this machine only")
    return 0


def cmd_hook(store: Store, a) -> int:
    from . import hooks
    code, out, err = hooks.run(store, a.event, sys.stdin.read())
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


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

    sp = add("dedupe", cmd_dedupe, "remove duplicate lines left by merges (shows them first; exit 1 if any)")
    sp.add_argument("satellite", nargs="?", help="satellite id (default: all)")
    sp.add_argument("path", nargs="?", help="one memory file, e.g. CLAUDE.md")
    sp.add_argument("--apply", action="store_true", help="remove them, commit, and update local copies")

    sp = add("watch", cmd_watch, "save automatically when memory files change (Ctrl+C to stop)")
    sp.add_argument("ids", nargs="*", help="satellites to watch (default: all tracked here)")
    sp.add_argument("--interval", type=_interval, default=2.0, metavar="SECONDS", help="how often to look (2)")
    sp.add_argument("--debounce", type=_seconds, default=2.0, metavar="SECONDS",
                    help="save once files have been quiet this long (2)")
    sp.add_argument("--sync-every", type=_seconds, default=0.0, metavar="SECONDS",
                    help="also pull and push on this schedule when a remote is set (off)")

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

    sp = add("install-hooks", cmd_install_hooks,
             "restore memories when a Claude Code session starts, save them when it ends")
    sp.add_argument("--project", nargs="?", const=".", metavar="DIR",
                    help="only for this project (.claude/settings.local.json) instead of all projects")
    sp.add_argument("--uninstall", action="store_true", help="remove punk's hooks, keep everything else")
    sp.add_argument("-n", "--dry-run", action="store_true")

    sp = add("hook", cmd_hook, "run a Claude Code hook (used by install-hooks; reads the hook's JSON on stdin)")
    sp.add_argument("event", choices=["session-start", "session-end"])
    return p


def _utf8_when_piped() -> None:
    """Pipes and files get UTF-8 (`punk context | llm`), not the locale codepage (cp1252 on Windows).

    Consoles are left alone: Python already writes Unicode to them correctly.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure and not stream.isatty() and (stream.encoding or "").lower().replace("-", "") != "utf8":
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_when_piped()
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
