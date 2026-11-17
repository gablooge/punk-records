"""`punk watch`: save memories automatically when they change (standard library only, by polling).

Every `interval` seconds the watcher fingerprints the memory files it knows about (size and
modification time; the target of symlinks between memory files). That is a few `stat` calls; the full
directory walk that finds *new* memory files runs only every `rescan` seconds. After a change it waits
until nothing has changed for `debounce` seconds, then saves once, so a burst of edits becomes one
commit. With `sync_every`, it
also runs a full sync (save, pull, push, restore) on that schedule when a remote is set.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from .discover import local_path, scan
from .store import Action, PunkError, Store


class Watcher:
    def __init__(self, store: Store, ids: list[str] | None = None, *, interval: float = 2.0,
                 debounce: float = 2.0, sync_every: float = 0.0, rescan: float = 30.0,
                 out: Callable[[str], None] = print):
        self.store, self.ids = store, ids or None
        self.interval, self.debounce, self.sync_every, self.rescan = interval, debounce, sync_every, rescan
        self.known: dict | None = None  # sid -> (root or None, regular/followed files, recorded links)
        self.last_scan = 0.0
        self.out = out
        self.seen: dict | None = None  # fingerprint at the last save
        self.current: dict | None = None
        self.changed_at: float | None = None  # when the latest unsaved change was noticed
        self.next_sync: float | None = None

    # ------------------------------------------------------------------ state
    def satellites(self) -> list[str]:
        paths = self.store.load_state()["paths"]
        return [sid for sid in (self.ids or sorted(paths)) if sid in paths]

    def _rescan(self) -> None:
        """Walk the watched projects to learn which memory files exist (new files, new links)."""
        state, cfg = self.store.load_state(), self.store.config()
        self.known = {}
        for sid in self.satellites():
            root = Path(state["paths"][sid])
            if not root.is_dir():
                self.known[sid] = (None, [], [])  # gone on this machine; save() reports it
                continue
            found = scan(root, self.store._kind(sid), cfg)
            self.known[sid] = (root, list(found.files), list(found.links))

    def fingerprint(self, full: bool = True) -> dict:
        if full or self.known is None:
            self._rescan()
        fp = {}
        for sid, (root, files, links) in self.known.items():
            if root is None:
                fp[sid] = None
                continue
            entries = {}
            for rel in files:  # stat follows symlinks, so edits through followed dotfile links count
                try:
                    st = local_path(root, rel).stat()
                except OSError:
                    continue  # deleted: missing from the fingerprint is a change
                entries[rel] = (st.st_size, st.st_mtime_ns)
            for rel in links:
                try:
                    entries[rel] = ("link", os.readlink(root / rel))
                except OSError:
                    continue
            fp[sid] = entries
        return fp

    # ----------------------------------------------------------------- output
    def say(self, text: str) -> None:
        self.out(f"[{time.strftime('%H:%M:%S')}] {text}")

    def report(self, what: str, actions: list[Action]) -> None:
        changed = [a for a in actions if a.kind != "skipped"]
        if changed:
            self.say(what)
            for a in changed:
                self.out(str(a))

    # ------------------------------------------------------------------- work
    def _save(self) -> bool:
        try:
            self.report("saved", self.store.save(self.ids))
            return True
        except PunkError as e:
            self.say(f"save failed, will retry: {str(e).strip().splitlines()[0]}")
            return False

    def _sync(self) -> None:
        try:
            self.report("synced", self.store.sync(self.ids))
        except PunkError as e:
            self.say(f"sync failed, will retry: {str(e).strip().splitlines()[0]}")

    def start(self, now: float | None = None) -> None:
        """Save anything edited while nobody was watching, and take the first fingerprint."""
        now = time.monotonic() if now is None else now
        self._save()
        self.seen = self.current = self.fingerprint()
        self.last_scan = now
        if self.sync_every:
            self.next_sync = now + self.sync_every

    def tick(self, now: float) -> None:
        if self.seen is None:
            self.start(now)
            return
        full = now - self.last_scan >= self.rescan
        if full:
            self.last_scan = now
        fp = self.fingerprint(full)
        if fp != self.current:
            self.current, self.changed_at = fp, now  # still changing: (re)start the quiet period
        if self.changed_at is not None and now - self.changed_at >= self.debounce:
            if self._save():
                self.seen, self.changed_at = self.fingerprint(full=False), None
                self.current = self.seen
            else:
                self.changed_at = now  # retry after another quiet period
        if self.next_sync is not None and now >= self.next_sync:
            self._sync()
            self.seen = self.current = self.fingerprint()
            self.next_sync = now + self.sync_every

    def run(self) -> None:
        """Poll until interrupted (Ctrl+C raises KeyboardInterrupt to the caller)."""
        self.start()
        while True:
            time.sleep(self.interval)
            self.tick(time.monotonic())
