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

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import secrets
from .discover import discover, is_memory_path

GLOBAL_ID = "global"

GITATTRIBUTES = """\
# Memories are mostly append-style notes. When two machines edit the same file,
# keep both sides' lines instead of stopping on a merge conflict.
records/** merge=union
"""

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
    kind: str  # add | update | delete | conflict | blocked | stale | missing
    sat: str
    rel: str = ""
    note: str = ""

    def __str__(self) -> str:
        mark = {"add": "+", "update": "~", "delete": "-", "conflict": "!", "blocked": "x",
                "stale": "?", "missing": "?"}.get(self.kind, " ")
        where = f"{self.sat}/{self.rel}" if self.rel else self.sat
        return f"  {mark} {self.kind:<8} {where}" + (f"  ({self.note})" if self.note else "")


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
    r = subprocess.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                       capture_output=True, text=True)
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
        r = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
        if check and r.returncode != 0:
            raise PunkError(f"git {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
        return r

    def load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            state = {}
        state.setdefault("machine", socket.gethostname())
        state.setdefault("paths", {})
        state.setdefault("synced", {})
        return state

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)

    def config(self) -> dict:
        try:
            return json.loads((self.root / "punk.json").read_text())
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
             track_global: bool = True) -> None:
        if self.exists():
            raise PunkError(f"a store already exists at {self.root}")
        if remote:
            self.root.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "-q", remote, str(self.root)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise PunkError(f"git clone failed:\n{r.stderr.strip()}")
            if self.git("rev-parse", "--verify", "HEAD", check=False).returncode != 0:
                self.git("symbolic-ref", "HEAD", "refs/heads/main")  # empty remote
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q")
            self.git("symbolic-ref", "HEAD", "refs/heads/main")
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
                (self.root / name).write_text(content)
        self.commit(f"init punk records on {state['machine']}")
        if track_global:
            self.track(Path.home(), sid=GLOBAL_ID, kind="global")

    # ------------------------------------------------------------- satellites
    def satellites(self) -> dict[str, dict]:
        base = self.root / "satellites"
        out = {}
        for f in sorted(base.rglob("*.json")) if base.is_dir() else []:
            sid = f.relative_to(base).as_posix()[: -len(".json")]
            try:
                out[sid] = json.loads(f.read_text())
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
            remote = subprocess.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                                    capture_output=True, text=True).stdout.strip()
            meta = {"name": path.name if kind != "global" else "global", "kind": kind}
            if remote and kind != "global":
                meta["remote"] = remote
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(meta, indent=2) + "\n")
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
            found = discover(root, kind, cfg)
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
        if not dry_run:
            self.save_state(state)
            n = sum(a.kind in ("add", "update", "delete") for a in actions)
            self.commit(message or f"save {n} memories from {state['machine']}")
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
            for rel in recfiles:
                if not is_memory_path(rel, kind, cfg):
                    actions.append(Action("blocked", sid, rel, "not a memory path; refusing to write it"))
                    continue
                rdata = (self.records / sid / rel).read_bytes()
                rec = sha(rdata)
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
            for rel in sorted(set(synced) - set(recfiles)):
                dst = root / rel
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

    def sync(self, **save_kw) -> list[Action]:
        """save -> pull -> push -> restore. Works offline: it just saves and restores."""
        actions = self.save(**save_kw)
        remote = self.remote()
        if remote:
            branch = self.branch()
            if self.git("ls-remote", "--heads", remote, branch).stdout.strip():
                self.pull(remote, branch)
            self.git("push", "-q", "-u", remote, branch)
        return actions + self.restore()

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

    # ------------------------------------------------------------ consumption
    def memories(self, sid: str | None = None) -> list[tuple[str, str]]:
        sats = [sid] if sid else sorted(self.satellites())
        return [(s, rel) for s in sats for rel in _files_under(self.records / validate_id(s))]

    def read(self, sid: str, rel: str) -> str:
        validate_id(sid)
        base = self.records / sid
        target = _safe_target(base, rel)
        if not target.is_file():
            raise PunkError(f"no memory {sid}/{rel}")
        return target.read_text("utf-8", "replace")

    def search(self, query: str, regex: bool = False, sid: str | None = None):
        rx = re.compile(query if regex else re.escape(query), re.IGNORECASE)
        for s, rel in self.memories(sid):
            for n, line in enumerate(self.read(s, rel).splitlines(), 1):
                if rx.search(line):
                    yield s, rel, n, line

    def context(self, path: Path) -> str:
        """Everything an agent should know when working in `path`, as one document."""
        parts = []
        sids = [GLOBAL_ID] if GLOBAL_ID in self.satellites() else []
        project = self.satellite_for(path)
        if project:
            sids.append(project)
        for sid in sids:
            for _, rel in self.memories(sid):
                parts.append(f"<!-- punk-records: {sid}/{rel} -->\n{self.read(sid, rel).rstrip()}\n")
        return "\n".join(parts)

    def append(self, sid: str, rel: str, text: str) -> None:
        """Add a note to a memory file in the store and propagate it locally."""
        self.require()
        validate_id(sid)
        if sid not in self.satellites():
            raise PunkError(f"unknown satellite: {sid}")
        if not is_memory_path(rel, self._kind(sid), self.config()):
            raise PunkError(f"{rel} is not a memory path (e.g. CLAUDE.md, AGENTS.md, napkin.md)")
        if secrets.scan(text):
            raise PunkError("refusing to store text that looks like a secret")
        target = _safe_target(self.records / sid, rel)
        existing = target.read_text("utf-8") if target.is_file() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(existing + text.rstrip() + "\n", "utf-8")
        self.commit(f"remember in {sid}/{rel}")
        if sid in self.load_state()["paths"]:
            self.restore([sid])
