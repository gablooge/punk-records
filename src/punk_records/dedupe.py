"""Remove duplicate lines that union merges leave behind.

When two machines add the same line in different places, git's union merge keeps both copies. This
keeps the first copy of each line and drops the later ones, except lines that legitimately repeat:
blank lines, fenced code (and the fences, matched as CommonMark does), lines indented 4+ spaces or a
tab (indented code, or deeply nested lists), Markdown headings, and lines without letters or digits
(`---`, table separators, closing tags). Lines match exactly, indentation included; only trailing
whitespace is ignored. Line endings and the rest of the bytes are kept as they are.
"""

from __future__ import annotations

import re

from .store import PunkError, Store, _safe_target

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_WORDY = re.compile(r"\w")


def dedupe_lines(data: bytes) -> tuple[bytes, list[tuple[int, str]]]:
    """Return (new bytes, [(line number, removed text)]). Raises UnicodeDecodeError for non-UTF-8."""
    lines = data.decode("utf-8").splitlines(keepends=True)
    seen: set[str] = set()
    kept: list[str] = []
    removed: list[tuple[int, str]] = []
    fence = ""  # the fence that opened the current code block, e.g. "```" or "~~~~"
    for n, raw in enumerate(lines, 1):
        text = raw.rstrip()
        m = _FENCE.match(text)
        if fence:
            # CommonMark: only the same character, at least as long, with nothing after it, closes
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = ""
            kept.append(raw)
            continue
        if m:
            fence = m.group(1)
            kept.append(raw)
            continue
        indented_code = raw.startswith(("    ", "\t"))  # or a deeply nested list: leave both alone
        if indented_code or not text or _HEADING.match(text) or not _WORDY.search(text):
            kept.append(raw)
            continue
        if text in seen:
            removed.append((n, text))
            continue
        seen.add(text)
        kept.append(raw)
    if not removed:
        return data, []
    return "".join(kept).encode("utf-8"), removed


def find(store: Store, ids: list[str] | None = None, rel: str | None = None):
    """Yield (satellite, path, data, new data, removed) for each memory file with duplicates."""
    from .discover import is_memory_path
    store.require()
    known = store.satellites()
    for sid in ids or sorted(known):
        if sid not in known:
            raise PunkError(f"unknown satellite: {sid} (see `punk list`)")
        kind, cfg = store._kind(sid), store.config()
        files = [r for _, r in store.memories(sid) if is_memory_path(r, kind, cfg)]
        if rel is not None:
            if not is_memory_path(rel, kind, cfg):
                raise PunkError(f"{rel} is not a memory path (e.g. CLAUDE.md, AGENTS.md, napkin.md)")
            if rel not in files:
                raise PunkError(f"no memory {sid}/{rel} in the store")
            files = [rel]
        for r in files:
            data = _safe_target(store.records / sid, r).read_bytes()
            try:
                new, removed = dedupe_lines(data)
            except UnicodeDecodeError:
                continue  # not UTF-8 text: leave it alone
            if removed:
                yield sid, r, data, new, removed


def apply(store: Store, found: list) -> list:
    """Write the deduplicated copies, commit, and restore the affected satellites on this machine."""
    with store.lock():
        for sid, r, _, new, _ in found:
            _safe_target(store.records / sid, r).write_bytes(new)
        lines = sum(len(x[4]) for x in found)
        store.commit(f"dedupe {lines} line{'' if lines == 1 else 's'} in {len(found)} "
                     f"file{'' if len(found) == 1 else 's'}")
        local = store.load_state()["paths"]
        sids = sorted({sid for sid, *_ in found if sid in local})
        return store.restore(sids) if sids else []
