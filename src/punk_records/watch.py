"""`punk watch`: save memories automatically when they change (standard library only, by polling).

Every `interval` seconds the watcher fingerprints the memory files of the watched satellites (path,
size, modification time; symlink targets). After a change it waits until nothing has changed for
`debounce` seconds, then saves once, so a burst of edits becomes one commit. With `sync_every`, it
also runs a full sync (save, pull, push, restore) on that schedule when a remote is set.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .discover import scan
from .store import Action, PunkError, Store


class Watcher:
    def __init__(self, store: Store, ids: list[str] | None = None, *, interval: float = 2.0,
                 debounce: float = 2.0, sync_every: float = 0.0, out: Callable[[str], None] = print):
        self.store, self.ids = store, ids or None
        self.interval, self.debounce, self.sync_every = interval, debounce, sync_every
        self.out = out
        self.seen: dict | None = None  # fingerprint at the last save
        self.current: dict | None = None
        self.changed_at: float | None = None  # when the latest unsaved change was noticed
        self.next_sync: float | None = None

    # ------------------------------------------------------------------ state
    def satellites(self) -> list[str]:
        paths = self.store.load_state()["paths"]
        return [sid for sid in (self.ids or sorted(paths)) if sid in paths]

    def fingerprint(self) -> dict:
        state, cfg, fp = self.store.load_state(), self.store.config(), {}
        for sid in self.satellites():
            root = Path(state["paths"][sid])
            if not root.is_dir():
                fp[sid] = None  # gone on this machine; save() reports it
                continue
            found = scan(root, self.store._kind(sid), cfg)
            entries = {}
            for rel in found.files:
                try:
                    st = (root / rel).stat()
                except OSError:
                    continue
                entries[rel] = (st.st_size, st.st_mtime_ns)
            for rel, target in found.links.items():
                entries[rel] = ("link", target)
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
        if self.sync_every:
            self.next_sync = now + self.sync_every

    def tick(self, now: float) -> None:
        if self.seen is None:
            self.start(now)
            return
        fp = self.fingerprint()
        if fp != self.current:
            self.current, self.changed_at = fp, now  # still changing: (re)start the quiet period
        if self.changed_at is not None and now - self.changed_at >= self.debounce:
            if self._save():
                self.seen, self.changed_at = self.fingerprint(), None
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
