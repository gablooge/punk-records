"""The Punk Records store: a git repository holding every satellite's memories.

Layout of the store (everything except `.git/` is versioned and synced):

    records/<satellite-id>/<path>   the memory files themselves
    satellites/<satellite-id>.json  what each satellite is (name, git remote, kind)
    punk.json                       shared config (extra include/exclude globs)
    .git/punk-state.json            machine-local: where satellites live on THIS
                                    machine, plus the hashes seen at last sync

Sync is three-way per file: we remember the local hash and the store hash at the
last sync, so we can tell "I edited it" from "another machine edited it" from
"both did" (a conflict, which is never resolved silently).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import secrets
from .discover import followed_target, is_memory_path, scan

GLOBAL_ID = "global"

GITATTRIBUTES = """\
# Store every file byte for byte (no CRLF conversion), so hashes match on every OS.
* -text
# Memories are mostly append-style notes. When two machines edit the same file,
# keep both sides' lines instead of stopping on a merge conflict.
records/** merge=union
"""

# Git for Windows defaults to core.autocrlf=true; the store must never convert line endings.
GIT_STORE_CONFIG = ("core.autocrlf", "false")

STORE_README = """\
# Punk Records

This repository is a memory store managed by `punk` (Punk Records).
It holds AI agent memories (CLAUDE.md, AGENTS.md, napkins, rules, ...) for many projects and machines.

- `records/<satellite>/...` - memory files, mirrored from each tracked project
- `satellites/<satellite>.json` - metadata for each tracked project

Keep this repository private: memories describe your code and how you work.
"""


class PunkError(Exception):
    pass


@dataclass
class Action:
    kind: str  # add | update | delete | conflict | blocked | stale | missing | skipped
    sat: str
    rel: str = ""
    note: str = ""

    def __str__(self) -> str:
        mark = {"add": "+", "update": "~", "delete": "-", "conflict": "!", "blocked": "x",
                "stale": "?", "missing": "?", "skipped": "?"}.get(self.kind, " ")
        where = f"{self.sat}/{self.rel}" if self.rel else self.sat
        return f"  {mark} {self.kind:<8} {where}" + (f"  ({self.note})" if self.note else "")


@dataclass
class Diff:
    sat: str
    rel: str
    text: str  # unified diff (store -> local), or a note for symlinks


def _diff_lines(data: bytes | None) -> list[str]:
    if data is None:
        return []
    lines = data.decode("utf-8", "replace").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines


def _eol(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    return "LF" if not crlf else "CRLF" if crlf == data.count(b"\n") else "mixed"


def _force_rmtree(path: Path) -> None:
    """Remove a tree that may hold read-only files: git makes its objects read-only, and Windows will
    not delete a read-only file (shutil.rmtree with ignore_errors would silently leave it behind)."""
    def clear_and_retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_and_retry)
    else:
        shutil.rmtree(path, onerror=clear_and_retry)


def _write_text(path: Path, text: str) -> None:
    """UTF-8, LF line endings on every OS (Path.write_text has no `newline` before 3.10)."""
    path.write_bytes(text.encode("utf-8"))


def _read_json(path: Path):
    return json.loads(path.read_bytes().decode("utf-8"))


def _git_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, encoding="utf-8", errors="replace")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_home() -> Path:
    return Path(os.environ.get("PUNK_HOME") or Path.home() / ".punk-records").expanduser()


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower()
    return "_" if s in ("", ".", "..") else s


def normalize_remote(url: str) -> str | None:
    """git@github.com:Me/Repo.git and https://github.com/me/repo -> github.com/me/repo"""
    u = url.strip()
    if u.endswith(".git"):
        u = u[:-4]
    m = re.match(r"^[\w.-]+@([^:/]+):(.+)$", u)
    if m:
        host, path = m.groups()
    else:
        parsed = urlparse(u)
        host, path = parsed.hostname, parsed.path
        if not host:
            return None
    segs = [_slug(x) for x in path.split("/") if x]
    return "/".join([_slug(host), *segs]) if segs else None


def derive_id(path: Path) -> str:
    """Stable id for a project, the same on every machine when it has a git remote."""
    r = _git_run(["-C", str(path), "config", "--get", "remote.origin.url"])
    remote = normalize_remote(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    return remote or f"local/{_slug(Path(path).resolve().name)}"


def validate_id(sid: str) -> str:
    p = PurePosixPath(sid)
    if (not sid or p.is_absolute() or any(s in ("", ".", "..") for s in sid.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", sid) or sid.startswith(".")):
        raise PunkError(f"invalid satellite id: {sid!r}")
    return sid


def _files_under(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and not p.is_symlink())


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    d = start
    while d != stop and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def _safe_target(root: Path, rel: str) -> Path:
    target = (root / rel)
    resolved_root = root.resolve()
    if resolved_root not in target.resolve().parents:
        raise PunkError(f"refusing to write outside {root}: {rel}")
    return target


def _inside(root: Path, rel: str) -> bool:
    """True if `rel`'s parent directory resolves inside `root` (the entry itself is not followed)."""
    resolved_root = root.resolve()
    parent = (root / rel).parent.resolve()
    return parent == resolved_root or resolved_root in parent.parents


OUTSIDE = "<outside>"


def _local_link(root: Path, rel: str) -> str | None:
    """Where the symlink at root/rel points (root-relative), OUTSIDE, or None if it is no symlink."""
    full = root / rel
    if not full.is_symlink():
        return None
    try:
        return full.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        return OUTSIDE


def _set_link(links: dict, rel: str, target: str | None) -> None:
    if target is None:
        links.pop(rel, None)
    else:
        links[rel] = target


def _make_link(root: Path, rel: str, target: str) -> None:
    """Create root/rel as a relative symlink to root/target, replacing whatever is there."""
    dst = root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(dst):
        dst.unlink()
    os.symlink(os.path.relpath(root / target, dst.parent), dst)


class Store:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else default_home()

    # ------------------------------------------------------------------ basics
    @property
    def records(self) -> Path:
        return self.root / "records"

    @property
    def state_path(self) -> Path:
        return self.root / ".git" / "punk-state.json"

    def exists(self) -> bool:
        return (self.root / ".git").is_dir()

    def require(self) -> None:
        if not self.exists():
            raise PunkError(f"no store at {self.root} - run `punk init` first")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = _git_run(["-C", str(self.root), *args])
        if check and r.returncode != 0:
            raise PunkError(f"git {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
        return r

    def load_state(self) -> dict:
        try:
            state = _read_json(self.state_path)
        except FileNotFoundError:
            state = {}
        state.setdefault("machine", socket.gethostname())
        state.setdefault("paths", {})
        state.setdefault("synced", {})
        state.setdefault("links", {})  # {sid: {link rel: target rel}} as of the last sync
        return state

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        _write_text(tmp, json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)

    def config(self) -> dict:
        try:
            return _read_json(self.root / "punk.json")
        except FileNotFoundError:
            return {}

    def remote(self) -> str | None:
        remotes = self.git("remote").stdout.split()
        return "origin" if "origin" in remotes else (remotes[0] if remotes else None)

    def branch(self) -> str:
        return self.git("symbolic-ref", "--short", "HEAD").stdout.strip()

    def commit(self, message: str) -> bool:
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.git("commit", "-q", "-m", message)
        return True

    # -------------------------------------------------------------- lifecycle
    def init(self, remote: str | None = None, machine: str | None = None,
             track_global: bool = True, encrypted: str | None = None) -> None:
        if self.exists():
            raise PunkError(f"a store already exists at {self.root}")
        if remote and encrypted:
            raise PunkError("use either a plain remote to clone or --encrypted, not both")
        created = not self.root.exists()
        if remote:
            self.root.parent.mkdir(parents=True, exist_ok=True)
            r = _git_run(["clone", "-q", "-c", "=".join(GIT_STORE_CONFIG), remote, str(self.root)])
            if r.returncode != 0:
                raise PunkError(f"git clone failed:\n{r.stderr.strip()}")
            if self.git("rev-parse", "--verify", "HEAD", check=False).returncode != 0:
                self.git("symbolic-ref", "HEAD", "refs/heads/main")  # empty remote
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q")
            self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", *GIT_STORE_CONFIG)
        state = self.load_state()
        if machine:
            state["machine"] = machine
        self.save_state(state)
        if not self.git("config", "user.email", check=False).stdout.strip():
            self.git("config", "user.name", f"punk@{state['machine']}")
            self.git("config", "user.email", f"punk@{_slug(state['machine'])}.local")
        for name, content in ((".gitattributes", GITATTRIBUTES), ("README.md", STORE_README),
                              ("punk.json", json.dumps({"include": [], "exclude": []}, indent=2) + "\n")):
            if not (self.root / name).exists():
                _write_text(self.root / name, content)
        self.commit(f"init punk records on {state['machine']}")
        if encrypted:
            from .crypt import Crypt
            try:
                Crypt(self).setup(encrypted)
                Crypt(self).exchange(push=False)  # bring in what other machines already pushed
            except BaseException:
                if created:
                    try:
                        _force_rmtree(self.root)  # so `punk init --encrypted` can simply be run again
                    except OSError:
                        pass  # never hide the original error
                raise
        if track_global:
            self.track(Path.home(), sid=GLOBAL_ID, kind="global")

    # ------------------------------------------------------------- satellites
    def satellites(self) -> dict[str, dict]:
        base = self.root / "satellites"
        out = {}
        for f in sorted(base.rglob("*.json")) if base.is_dir() else []:
            sid = f.relative_to(base).as_posix()[: -len(".json")]
            try:
                out[sid] = _read_json(f)
            except ValueError:
                out[sid] = {}
        return out

    def track(self, path: Path, sid: str | None = None, kind: str = "project") -> str:
        self.require()
        path = Path(path).expanduser().resolve()
        if not path.is_dir():
            raise PunkError(f"not a directory: {path}")
        sid = validate_id(sid or derive_id(path))
        meta_path = self.root / "satellites" / f"{sid}.json"
        if not meta_path.exists():
            remote = _git_run(["-C", str(path), "config", "--get", "remote.origin.url"]).stdout.strip()
            meta = {"name": path.name if kind != "global" else "global", "kind": kind}
            if remote and kind != "global":
                meta["remote"] = remote
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            _write_text(meta_path, json.dumps(meta, indent=2) + "\n")
            self.commit(f"track {sid}")
        state = self.load_state()
        state["paths"][sid] = str(path)
        self.save_state(state)
        return sid

    def untrack(self, sid: str, purge: bool = False) -> None:
        state = self.load_state()
        if sid not in state["paths"] and sid not in self.satellites():
            raise PunkError(f"unknown satellite: {sid}")
        state["paths"].pop(sid, None)
        state["synced"].pop(sid, None)
        state["links"].pop(sid, None)
        self.save_state(state)
        if purge:
            validate_id(sid)
            shutil.rmtree(self.records / sid, ignore_errors=True)
            (self.root / "satellites" / f"{sid}.json").unlink(missing_ok=True)
            self.commit(f"purge {sid}")

    def satellite_for(self, path: Path) -> str | None:
        """Which tracked satellite contains `path` on this machine (deepest wins)."""
        path = Path(path).resolve()
        best, best_len = None, -1
        for sid, p in self.load_state()["paths"].items():
            if sid == GLOBAL_ID:
                continue
            p = Path(p)
            if (path == p or p in path.parents) and len(p.parts) > best_len:
                best, best_len = sid, len(p.parts)
        if best:
            return best
        sid = derive_id(path)
        return sid if sid in self.satellites() else None

    def _local_ids(self, state: dict, ids: list[str] | None) -> list[str]:
        known = state["paths"]
        if not ids:
            return sorted(known)
        for i in ids:
            if i not in known:
                raise PunkError(f"{i!r} is not tracked on this machine (see `punk list`)")
        return ids

    def _kind(self, sid: str) -> str:
        return "global" if sid == GLOBAL_ID else self.satellites().get(sid, {}).get("kind", "project")

    def _meta_path(self, sid: str) -> Path:
        return self.root / "satellites" / f"{validate_id(sid)}.json"

    def links(self, sid: str) -> dict[str, str]:
        """Symlinked memories recorded for a satellite: {link relpath: target relpath}."""
        try:
            links = _read_json(self._meta_path(sid)).get("links", {})
        except (FileNotFoundError, ValueError, AttributeError):
            return {}
        if not isinstance(links, dict):
            return {}
        return {k: v for k, v in links.items() if isinstance(k, str) and isinstance(v, str)}

    def _follow_links(self, sid: str, rel: str) -> str:
        """The memory file a recorded symlink chain ends at (`rel` itself if it is no link)."""
        links, seen = self.links(sid), set()
        while rel in links:
            if rel in seen:
                raise PunkError(f"symlink loop at {sid}/{rel}")
            seen.add(rel)
            rel = links[rel]
        return rel

    def _write_links(self, sid: str, links: dict[str, str]) -> None:
        path = self._meta_path(sid)
        try:
            meta = _read_json(path)
        except (FileNotFoundError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if links:
            meta["links"] = dict(sorted(links.items()))
        else:
            meta.pop("links", None)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, json.dumps(meta, indent=2) + "\n")

    # ------------------------------------------------------------------- save
    def save(self, ids: list[str] | None = None, *, message: str | None = None, redact: bool = False,
             allow_secrets: bool = False, force: bool = False, dry_run: bool = False) -> list[Action]:
        """Copy local memory files into the store and commit."""
        self.require()
        state, cfg, actions = self.load_state(), self.config(), []
        for sid in self._local_ids(state, ids):
            root = Path(state["paths"][sid])
            if not root.is_dir():
                actions.append(Action("missing", sid, note=f"{root} not found on this machine"))
                continue
            kind = self._kind(sid)
            scanned = scan(root, kind, cfg)
            found, local_links = scanned.files, scanned.links
            actions.extend(Action("skipped", sid, rel, why) for rel, why in scanned.warnings)
            synced = state["synced"].setdefault(sid, {})
            dest = self.records / sid
            for rel in found:
                data = (root / rel).read_bytes()
                loc = sha(data)
                st = synced.get(rel)
                target = dest / rel
                rec = sha(target.read_bytes()) if target.is_file() else None
                if st and loc == st["local"]:
                    continue  # untouched here since last sync
                if rec == loc:
                    synced[rel] = {"local": loc, "record": rec}
                    continue
                if rec is not None and (st is None or rec != st["record"]) and not force:
                    note = ("differs from the store copy; `punk restore --force` or `punk save --force`"
                            if st is None else "changed here and in the store")
                    actions.append(Action("conflict", sid, rel, note))
                    continue
                out, note = data, ""
                text = data.decode("utf-8", "replace")
                hits = secrets.scan(text)
                if hits and not allow_secrets:
                    lines = ", ".join(f"{h.kind}@L{h.line}" for h in hits)
                    if not redact:
                        actions.append(Action("blocked", sid, rel, f"possible secret: {lines}"))
                        continue
                    out, note = secrets.redact(text, hits).encode(), f"redacted {lines}"
                actions.append(Action("update" if rec else "add", sid, rel, note))
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(out)
                    synced[rel] = {"local": loc, "record": sha(out)}
            found_set = set(found)
            for rel in sorted(set(synced) - found_set):
                target = dest / rel
                if not target.is_file():
                    if not dry_run:
                        del synced[rel]
                    continue
                if sha(target.read_bytes()) == synced[rel]["record"] or force:
                    actions.append(Action("delete", sid, rel))
                    if not dry_run:
                        target.unlink()
                        _prune_empty_dirs(target.parent, self.records)
                        del synced[rel]
                else:
                    actions.append(Action("conflict", sid, rel, "deleted here but changed in the store"))
            actions.extend(self._save_links(sid, local_links, state, force=force, dry_run=dry_run))
        if not dry_run:
            self.save_state(state)
            n = sum(a.kind in ("add", "update", "delete") for a in actions)
            self.commit(message or f"save {n} memories from {state['machine']}")
        return actions

    def _save_links(self, sid: str, local: dict[str, str], state: dict, *, force: bool,
                    dry_run: bool) -> list[Action]:
        """Three-way merge of this machine's symlinked memories into satellites/<sid>.json."""
        actions = []
        base = state["links"].setdefault(sid, {})
        stored = self.links(sid)
        new = dict(stored)
        for rel in sorted(set(local) | set(stored) | set(base)):
            loc, rec, old = local.get(rel), stored.get(rel), base.get(rel)
            if loc == rec:
                if not dry_run:
                    _set_link(base, rel, loc)
                continue
            if loc == old:
                continue  # unchanged here; `punk restore` applies the store's version
            if rec != old and not force:
                actions.append(Action("conflict", sid, rel, "symlink changed here and in the store"))
                continue
            if loc is None:
                actions.append(Action("delete", sid, rel, "symlink"))
                new.pop(rel, None)
            else:
                actions.append(Action("update" if rec else "add", sid, rel, f"symlink -> {loc}"))
                new[rel] = loc
            if not dry_run:
                _set_link(base, rel, loc)
        if new != stored and not dry_run:
            self._write_links(sid, new)
        return actions

    def _restore_links(self, sid: str, root: Path, kind: str, cfg: dict, state: dict,
                       recfiles: list[str], *, force: bool, prune: bool, dry_run: bool) -> list[Action]:
        """Recreate the store's symlinked memories locally, never clobbering local changes."""
        actions = []
        base = state["links"].setdefault(sid, {})
        synced = state["synced"].setdefault(sid, {})
        stored = self.links(sid)
        for rel in sorted(set(stored) | set(base)):
            rec, old = stored.get(rel), base.get(rel)
            if rec is not None and not (is_memory_path(rel, kind, cfg) and is_memory_path(rec, kind, cfg)):
                actions.append(Action("blocked", sid, rel, "symlink outside memory paths; refusing to create it"))
                continue
            if not _inside(root, rel) or (rec is not None and not _inside(root, rec)):
                actions.append(Action("blocked", sid, rel, "symlink would leave the satellite; refusing"))
                continue
            if rec is not None and (rec == rel or (root / rec).is_dir()):
                actions.append(Action("blocked", sid, rel, f"symlink to {rec} must point to a file"))
                continue
            dst = root / rel
            loc = _local_link(root, rel)
            if loc == rec:
                if not dry_run:
                    _set_link(base, rel, rec)
                continue
            if rec == old:
                continue  # store unchanged; local changes wait for `punk save`
            exists = os.path.lexists(dst)
            if rec is None:  # removed from the store
                if loc != old:
                    if not dry_run:
                        base.pop(rel, None)
                    continue
                if rel in recfiles or prune:  # replaced by a regular file upstream, or pruning
                    actions.append(Action("delete", sid, rel, "symlink"))
                    if not dry_run:
                        dst.unlink()
                        base.pop(rel, None)
                else:
                    actions.append(Action("stale", sid, rel, "symlink removed from the store; --prune deletes it here"))
                continue
            plain = exists and loc is None and dst.is_file()
            replaceable = plain and rel in synced and rel not in recfiles and \
                sha(dst.read_bytes()) == synced[rel]["local"]
            safe = loc == old and (not exists or loc is not None or replaceable)
            if not safe and not force:
                note = ("regular file here, symlink in the store" if plain
                        else "symlink changed here and in the store")
                actions.append(Action("conflict", sid, rel, note))
                continue
            note = f"symlink -> {rec}"
            backup = plain and not replaceable
            if backup:
                note += f"; local copy kept as {Path(rel).name}.punk-bak"
            actions.append(Action("update" if exists else "add", sid, rel, note))
            if dry_run:
                continue
            try:
                if backup:
                    shutil.copy2(dst, dst.with_name(dst.name + ".punk-bak"))
                _make_link(root, rel, rec)
            except OSError as e:
                actions[-1] = Action("blocked", sid, rel, f"could not create symlink: {e}")
                continue
            base[rel] = rec
            synced.pop(rel, None)
        return actions

    # ---------------------------------------------------------------- restore
    def restore(self, ids: list[str] | None = None, *, force: bool = False, prune: bool = False,
                dry_run: bool = False) -> list[Action]:
        """Write store memories back into local projects, never clobbering local edits."""
        self.require()
        state, cfg, actions = self.load_state(), self.config(), []
        for sid in self._local_ids(state, ids):
            root = Path(state["paths"][sid])
            if not root.is_dir():
                actions.append(Action("missing", sid, note=f"{root} not found on this machine"))
                continue
            kind = self._kind(sid)
            synced = state["synced"].setdefault(sid, {})
            recfiles = _files_under(self.records / sid)
            actions.extend(self._restore_links(sid, root, kind, cfg, state, recfiles,
                                               force=force, prune=prune, dry_run=dry_run))
            stored_links = self.links(sid)
            for rel in recfiles:
                if not is_memory_path(rel, kind, cfg):
                    actions.append(Action("blocked", sid, rel, "not a memory path; refusing to write it"))
                    continue
                rdata = (self.records / sid / rel).read_bytes()
                rec = sha(rdata)
                dst = root / rel
                # never write through a link, except a global dotfile link (see followed_target)
                if dst.is_symlink() and not (kind == "global" and followed_target(root, dst, cfg)):
                    st = synced.get(rel)
                    if st and rec == st["record"]:
                        continue  # store unchanged; the new local symlink waits for `punk save`
                    if not force:
                        actions.append(Action("conflict", sid, rel, "symlink here, regular file in the store"))
                        continue
                    actions.append(Action("update", sid, rel, "replaced local symlink"))
                    if not dry_run:
                        if not _inside(root, rel):
                            raise PunkError(f"refusing to write outside {root}: {rel}")
                        dst.unlink()
                        dst.write_bytes(rdata)
                        synced[rel] = {"local": rec, "record": rec}
                        state["links"][sid].pop(rel, None)
                    continue
                dst = _safe_target(root, rel)
                loc = sha(dst.read_bytes()) if dst.is_file() else None
                st = synced.get(rel)
                if loc == rec:
                    if not dry_run:
                        synced[rel] = {"local": rec, "record": rec}
                    continue
                if st and rec == st["record"]:
                    continue  # store unchanged; local edits are waiting for `punk save`
                safe = (loc is None and st is None) or (st is not None and loc == st["local"])
                if not safe and not force:
                    note = "deleted here, changed in the store" if loc is None else "edited here and in the store"
                    actions.append(Action("conflict", sid, rel, note))
                    continue
                note = ""
                if not safe and loc is not None:
                    note = f"local copy kept as {Path(rel).name}.punk-bak"
                actions.append(Action("update" if loc else "add", sid, rel, note))
                if not dry_run:
                    if note:
                        shutil.copy2(dst, dst.with_name(dst.name + ".punk-bak"))
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(rdata)
                    synced[rel] = {"local": rec, "record": rec}
            for rel in sorted(set(synced) - set(recfiles) - set(stored_links)):
                dst = root / rel
                if dst.is_symlink() and not (kind == "global" and followed_target(root, dst, cfg)):
                    continue  # became a symlink here; `punk save` records it
                if not dst.is_file():
                    if not dry_run:
                        del synced[rel]
                elif sha(dst.read_bytes()) == synced[rel]["local"]:
                    if prune:
                        actions.append(Action("delete", sid, rel))
                        if not dry_run:
                            dst.unlink()
                            del synced[rel]
                    else:
                        actions.append(Action("stale", sid, rel, "removed from the store; --prune deletes it here"))
        if not dry_run:
            self.save_state(state)
        return actions

    # ------------------------------------------------------------------- sync
    def pull(self, source: str, ref: str) -> None:
        r = self.git("pull", "-q", "--rebase", "--no-autostash", source, ref, check=False)
        if r.returncode != 0:
            self.git("rebase", "--abort", check=False)
            raise PunkError(f"could not merge {source} {ref}:\n{(r.stderr or r.stdout).strip()}\n"
                            f"Resolve by hand in {self.root} (it is a normal git repo).")

    def exchange(self, push: bool = True) -> bool:
        """Pull from the git remote (if it has our branch) and push to it. False if there is no remote."""
        from .crypt import Crypt
        crypt = Crypt(self)
        if crypt.enabled:
            return crypt.exchange(push)
        remote = self.remote()
        if not remote:
            return False
        branch = self.branch()
        if self.git("ls-remote", "--heads", remote, branch).stdout.strip():
            self.pull(remote, branch)
        if push:
            self.git("push", "-q", "-u", remote, branch)
        return True

    def sync(self, ids: list[str] | None = None, **save_kw) -> list[Action]:
        """save -> pull -> push -> restore. Works offline: it just saves and restores."""
        actions = self.save(ids, **save_kw)
        self.exchange()
        from .team import Team
        for name, error in Team(self).update():  # read-only subscriptions; never block your own sync
            actions.append(Action("skipped", f"@{name}", note=f"could not update: {error}"))
        return actions + self.restore(ids)

    def export_bundle(self, path: Path) -> Path:
        """Write the whole store into one file for sneakernet / air-gapped transfer."""
        self.require()
        path = Path(path).expanduser().resolve()
        self.git("bundle", "create", "-q", str(path), self.branch())
        return path

    def import_bundle(self, path: Path, **save_kw) -> list[Action]:
        self.require()
        path = Path(path).expanduser().resolve()
        self.git("bundle", "verify", "-q", str(path))
        heads = [ln.split()[1] for ln in self.git("bundle", "list-heads", str(path)).stdout.splitlines()
                 if len(ln.split()) == 2 and ln.split()[1].startswith("refs/heads/")]
        if not heads:
            raise PunkError(f"{path} contains no branches")
        mine = f"refs/heads/{self.branch()}"
        actions = self.save(**save_kw)
        self.pull(str(path), mine if mine in heads else heads[0])
        return actions + self.restore()

    # ------------------------------------------------------------------- diff
    def diff(self, ids: list[str] | None = None, rel: str | None = None) -> list[Diff]:
        """What differs between this machine's memories and the store, as unified diffs.

        `-` lines are the store copy, `+` lines the local file. Only memory paths are compared,
        and local files are read only if discovery would save them.
        """
        self.require()
        state, cfg, out = self.load_state(), self.config(), []
        for sid in self._local_ids(state, ids):
            root, kind = Path(state["paths"][sid]), self._kind(sid)
            if rel is not None and not is_memory_path(rel, kind, cfg):
                raise PunkError(f"{rel} is not a memory path (e.g. CLAUDE.md, AGENTS.md, napkin.md)")
            if not root.is_dir():
                out.append(Diff(sid, "", f"{root} not found on this machine\n"))
                continue
            scanned = scan(root, kind, cfg)
            local = set(scanned.files)
            stored = {r for r in _files_under(self.records / sid) if is_memory_path(r, kind, cfg)}
            links = self.links(sid)
            for r in sorted(local | stored | set(scanned.links) | set(links)):
                if rel is not None and r != rel:
                    continue
                here, there = scanned.links.get(r), links.get(r)
                if here != there:
                    out.append(Diff(sid, r, f"symlink {sid}/{r}\n"
                                            f"  store: {'-> ' + there if there else '(none)'}\n"
                                            f"  local: {'-> ' + here if here else '(none)'}\n"))
                a = (self.records / sid / r).read_bytes() if r in stored else None
                b = (root / r).read_bytes() if r in local else None
                if a == b:
                    continue
                if a is not None and b is not None and \
                        a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"):
                    out.append(Diff(sid, r, f"{sid}/{r}: only line endings differ "
                                            f"(store {_eol(a)}, local {_eol(b)})\n"))
                    continue
                text = "".join(difflib.unified_diff(
                    _diff_lines(a), _diff_lines(b),
                    f"store/{sid}/{r}" if a is not None else "/dev/null",
                    f"local/{sid}/{r}" if b is not None else "/dev/null"))
                out.append(Diff(sid, r, text or f"{sid}/{r}: bytes differ (encoding or line endings)\n"))
        return out

    # ------------------------------------------------------------ consumption
    def memories(self, sid: str | None = None) -> list[tuple[str, str]]:
        sats = [sid] if sid else sorted(self.satellites())
        return [(s, rel) for s in sats for rel in _files_under(self.records / validate_id(s))]

    def read(self, sid: str, rel: str) -> str:
        validate_id(sid)
        base = self.records / sid
        target = _safe_target(base, self._follow_links(sid, rel))
        if not target.is_file():
            raise PunkError(f"no memory {sid}/{rel}")
        return target.read_text("utf-8", "replace")

    def search(self, query: str, regex: bool = False, sid: str | None = None):
        """Matching lines in your memories, then in subscribed team stores (labelled `@name/<sid>`)."""
        rx = re.compile(query if regex else re.escape(query), re.IGNORECASE)
        for s, rel in self.memories(sid):
            for n, line in enumerate(self.read(s, rel).splitlines(), 1):
                if rx.search(line):
                    yield s, rel, n, line
        from .team import Team
        team = Team(self)
        for label, tsid, rel in team.search_files():
            if sid and tsid != sid:
                continue
            try:
                text = team.read(label.split("/", 1)[0][1:], tsid, rel)
            except PunkError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    yield label, rel, n, line

    def context(self, path: Path) -> str:
        """Everything an agent should know when working in `path`, as one document.

        Order: subscribed team rules, your global memories, team notes for this project, your project
        memories. Your own notes come after the team's, so they read as the final word.
        """
        from .team import Team, project_id
        team, parts = Team(self), []

        def mine(sid: str) -> None:
            if sid in self.satellites():
                for _, rel in self.memories(sid):
                    parts.append(f"<!-- punk-records: {sid}/{rel} -->\n{self.read(sid, rel).rstrip()}\n")

        def theirs(sid: str) -> None:
            for label, where, text in team.sections([sid]):
                parts.append(f"<!-- punk-records: {label}/{where} (read-only, from {label[1:]}) -->\n{text.rstrip()}\n")

        theirs(GLOBAL_ID)
        mine(GLOBAL_ID)
        project = project_id(self, path) if team.names() else self.satellite_for(path)
        if project:
            theirs(project)
            mine(project)
        return "\n".join(parts)

    def _write_target(self, sid: str, rel: str) -> tuple[str, Path, str | None]:
        """Resolve where a write to sid/rel lands: (rel, store path, local root or None).

        Follows symlinked memories (AGENTS.md -> CLAUDE.md): first a local link on this machine, if
        it stays inside the project, then links recorded in the store. Only memory paths qualify.
        """
        self.require()
        validate_id(sid)
        if sid not in self.satellites():
            raise PunkError(f"unknown satellite: {sid}")
        kind, cfg = self._kind(sid), self.config()
        if not is_memory_path(rel, kind, cfg):
            raise PunkError(f"{rel} is not a memory path (e.g. CLAUDE.md, AGENTS.md, napkin.md)")
        local_root = self.load_state()["paths"].get(sid)
        local = _local_link(Path(local_root), rel) if local_root else None
        if local and local != OUTSIDE and is_memory_path(local, kind, cfg):
            rel = local  # linked here but not saved yet
        rel = self._follow_links(sid, rel)
        if not is_memory_path(rel, kind, cfg):
            raise PunkError(f"{rel} is not a memory path (e.g. CLAUDE.md, AGENTS.md, napkin.md)")
        return rel, _safe_target(self.records / sid, rel), local_root

    def write(self, sid: str, rel: str, content: str, base_sha256: str | None = None) -> tuple[str, str]:
        """Replace a memory file, refusing if it changed since the caller read it.

        `base_sha256` is the hash of the store copy the caller based its edit on (read_memory reports
        it); required when the file exists. Refuses if this machine's copy has unsaved edits, so they
        are never overwritten. Keeps a CRLF file CRLF. Returns (path written, new sha256).
        """
        rel, target, local_root = self._write_target(sid, rel)
        if secrets.scan(content):
            raise PunkError("refusing to store text that looks like a secret")
        current = target.read_bytes() if target.is_file() else None
        if current is not None and not base_sha256:
            raise PunkError(f"{sid}/{rel} exists: read it with read_memory first and pass its sha256 "
                            f"as base_sha256")
        if current is None and base_sha256:
            raise PunkError(f"{sid}/{rel} was removed since you read it; read it again")
        if current is not None and sha(current) != base_sha256:
            raise PunkError(f"{sid}/{rel} changed since you read it; read it again and redo the edit")
        if local_root and any(a.rel == rel for a in self.save([sid], dry_run=True)):
            raise PunkError(f"{sid}/{rel} has unsaved edits on this machine; run `punk save` (or append "
                            f"instead) so they are not overwritten")
        data = content.replace("\r\n", "\n")
        if current is not None and b"\r\n" in current:
            data = data.replace("\n", "\r\n")  # keep the file's own line endings
        out = data.encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out)
        self.commit(f"write {sid}/{rel}")
        if local_root:
            self.restore([sid])
        return rel, sha(out)

    def append(self, sid: str, rel: str, text: str) -> str:
        """Add a note to a memory file in the store and propagate it locally.

        Symlinked memories are followed (AGENTS.md -> CLAUDE.md appends to CLAUDE.md): first a
        local link on this machine, if it stays inside the project, then links recorded in the
        store. Returns the path that was written.
        """
        rel, target, local_root = self._write_target(sid, rel)
        if secrets.scan(text):
            raise PunkError("refusing to store text that looks like a secret")
        existing = target.read_bytes() if target.is_file() else b""
        existing.decode("utf-8")  # refuse to append to a file that is not UTF-8 text
        eol = b"\r\n" if b"\r\n" in existing else b"\n"  # keep the file's own line endings
        if existing and not existing.endswith(b"\n"):
            existing += eol
        lines = text.rstrip().replace("\r\n", "\n").split("\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(existing + eol.join(x.encode("utf-8") for x in lines) + eol)
        self.commit(f"remember in {sid}/{rel}")
        if local_root:
            self.restore([sid])
        return rel
