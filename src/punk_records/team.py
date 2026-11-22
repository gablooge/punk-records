"""Team brain: read-only memories subscribed from someone else's Punk Records store.

`punk subscribe acme <git-url>` keeps a bare clone in `<store>/.git/punk-subs/acme`. It is machine-local
(never synced: a synced list would let a tampered store make every machine fetch from a URL of its
choosing) and read straight from git objects: nothing is checked out, so symlinks or hooks in the team's
repo never touch the disk. Symlink entries are ignored and files over MAX_BYTES are skipped.

Subscribed memories appear in `punk context`, `punk search` and the MCP context/search tools, labelled
`@name`. They are never restored into projects, never saved into your store and never pushed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .discover import MAX_BYTES
from .store import GLOBAL_ID, PunkError, Store, _force_rmtree, locked_store

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
FORBIDDEN_URL = re.compile(r"^\s*(ext|fd)::", re.IGNORECASE)  # git transports that run commands


class Team:
    def __init__(self, store: Store):
        self.store = store
        self.dir = store.root / ".git" / "punk-subs"

    def _git(self, name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-c", "protocol.ext.allow=never", "--git-dir", str(self.dir / name), *args],
                           capture_output=True)
        if check and r.returncode != 0:
            err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
            raise PunkError(f"git {' '.join(args)} (subscription {name}) failed:\n{err}")
        return r

    # ------------------------------------------------------------- management
    def names(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.name for p in self.dir.iterdir() if (p / "HEAD").is_file() and NAME.match(p.name))

    def url(self, name: str) -> str:
        return self._git(name, "config", "--get", "remote.origin.url", check=False).stdout.decode().strip()

    @locked_store
    def subscribe(self, name: str, url: str) -> None:
        self.store.require()
        if not NAME.match(name):
            raise PunkError(f"invalid name {name!r}: use lowercase letters, digits and dashes")
        if FORBIDDEN_URL.match(url):
            raise PunkError(f"refusing {url!r}: ext:: and fd:: transports run commands")
        if name in self.names():
            raise PunkError(f"already subscribed to {name} ({self.url(name)}); unsubscribe first")
        self.dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "-c", "protocol.ext.allow=never", "clone", "-q", "--bare", url,
                            str(self.dir / name)], capture_output=True)
        if r.returncode != 0:
            _force_rmtree(self.dir / name)
            raise PunkError(f"git clone of {url} failed:\n{r.stderr.decode('utf-8', 'replace').strip()}")
        if not self._git(name, "ls-tree", "--name-only", "HEAD", "records", check=False).stdout.strip():
            _force_rmtree(self.dir / name)
            raise PunkError(f"{url} is not a Punk Records store (no records/ on its default branch)")

    @locked_store
    def unsubscribe(self, name: str) -> None:
        if name not in self.names():
            raise PunkError(f"not subscribed to {name!r} (see `punk subscriptions`)")
        _force_rmtree(self.dir / name)

    @locked_store
    def update(self) -> list[tuple[str, str]]:
        """Fetch every subscription. Returns [(name, error)] for the ones that failed (kept as they were)."""
        failed = []
        for name in self.names():
            r = self._git(name, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/heads/*", check=False)
            if r.returncode != 0:
                lines = r.stderr.decode("utf-8", "replace").strip().splitlines() or ["git fetch failed"]
                failed.append((name, lines[0]))
        return failed

    def updated(self, name: str) -> str:
        return self._git(name, "log", "-1", "--format=%cr", "HEAD", check=False).stdout.decode().strip()

    # ---------------------------------------------------------------- reading
    def files(self, name: str, sid: str) -> list[tuple[str, str]]:
        """[(satellite, path)] of regular, small enough files under records/<sid>/ (symlinks ignored)."""
        prefix = f"records/{sid}/"
        out = self._git(name, "ls-tree", "-r", "-l", "-z", "HEAD", "--", prefix.rstrip("/"), check=False).stdout
        found = []
        for entry in out.decode("utf-8", "replace").split("\0"):
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) != 4 or parts[0] not in ("100644", "100755") or parts[1] != "blob":
                continue  # symlinks (120000), submodules, trees
            if not parts[3].isdigit() or int(parts[3]) > MAX_BYTES or not path.startswith(prefix):
                continue
            found.append((sid, path[len(prefix):]))
        return found

    def read(self, name: str, sid: str, rel: str) -> str:
        r = self._git(name, "cat-file", "blob", f"HEAD:records/{sid}/{rel}", check=False)
        if r.returncode != 0:
            raise PunkError(f"no memory @{name}/{sid}/{rel}")
        data = r.stdout
        if b"\0" in data[:8192]:
            raise PunkError(f"@{name}/{sid}/{rel} is not text")
        return data.decode("utf-8", "replace")

    def satellites(self, name: str) -> list[str]:
        """Satellite ids listed in the team store's satellites/*.json (as every punk store has), plus global."""
        out = self._git(name, "ls-tree", "-r", "--name-only", "HEAD", "satellites", check=False).stdout.decode()
        sids = {p[len("satellites/"):-len(".json")] for p in out.splitlines() if p.endswith(".json")}
        if self.files(name, GLOBAL_ID):
            sids.add(GLOBAL_ID)
        return sorted(sids)

    def sections(self, sids: list[str]) -> list[tuple[str, str, str]]:
        """[(label, sid, text)] for these satellites in every subscription, for `context`."""
        out = []
        for name in self.names():
            for sid in sids:
                for _, rel in self.files(name, sid):
                    try:
                        out.append((f"@{name}", f"{sid}/{rel}", self.read(name, sid, rel)))
                    except PunkError:
                        continue
        return out

    def search_files(self) -> list[tuple[str, str, str]]:
        """[(label, sid, rel)] for every file of every subscription, per satellite listed by the store."""
        out = []
        for name in self.names():
            for sid in self.satellites(name):
                for _, rel in self.files(name, sid):
                    out.append((f"@{name}/{sid}", sid, rel))
        return out


def project_id(store: Store, path: Path) -> str | None:
    """The satellite id for `path`, even if this machine does not track it (team notes may exist)."""
    from .store import derive_id
    return store.satellite_for(path) or (derive_id(path) if Path(path).is_dir() else None)


__all__ = ["Team", "project_id", "GLOBAL_ID"]
