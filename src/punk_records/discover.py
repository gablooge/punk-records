"""Find AI memory files (CLAUDE.md, AGENTS.md, napkins, cursor rules, ...)."""

from __future__ import annotations

import fnmatch
import functools
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import NamedTuple

# File names that are memories wherever they appear inside a project.
BASENAMES = {
    "CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    "AGENT.md",
    "GEMINI.md",
    "napkin.md",
    "NAPKIN.md",
    ".napkin.md",
    "CONVENTIONS.md",  # aider
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".goosehints",
}

# Paths (relative to the project root) that hold memories. `*` also matches `/`.
PROJECT_GLOBS = [
    ".github/copilot-instructions.md",
    ".github/instructions/*.md",
    ".cursor/rules/*",
    ".windsurf/rules/*",
    ".clinerules/*",
    ".roo/rules/*",
    ".kiro/steering/*",
    ".claude/*.md",  # napkins, notes, commands, agents, skills
    ".claude/skills/*",
]

# User-level memories, relative to $HOME. Only these locations are read.
GLOBAL_GLOBS = [
    ".claude/CLAUDE.md",
    ".claude/napkin.md",
    ".claude/commands/*",
    ".claude/agents/*",
    ".claude/skills/*",
    ".codex/AGENTS.md",
    ".gemini/GEMINI.md",
    ".config/goose/.goosehints",
]

# Directories never descended into.
PRUNE = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "vendor", ".gradle", ".idea", ".terraform",
}

# Path segments that may never be written to, whatever the config says.
FORBIDDEN_SEGMENTS = {".git", ".hg", ".svn", ".."}

MAX_BYTES = 1_000_000

# Claude Code's auto memory for a project lives outside it, in
# <CLAUDE_CONFIG_DIR or ~/.claude>/projects/<repo path, non-alphanumerics as "-">/memory/, keyed by
# the repo's path on this machine. Stored as <satellite>/@claude-memory/<file>, so another machine
# (or another checkout path) restores it into its own folder.
AUTO_MEMORY = "@claude-memory"

# Global (dotfiles-style) symlinks are never followed into these directories.
SENSITIVE_SEGMENTS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
                      "gcloud", ".netrc", ".git-credentials"}
# ...and only to files that look like memories: these suffixes, or the link's own file name.
FOLLOW_SUFFIXES = {".md", ".mdc", ".markdown", ".txt"}


@functools.lru_cache(maxsize=256)
def _repo_root(root: str) -> str:
    """The main checkout a folder belongs to (worktrees share it), or the folder itself."""
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True)
    except OSError:
        return root
    common = r.stdout.strip()
    if r.returncode != 0 or not common:
        return root
    common_path = Path(common)
    return str(common_path.parent if common_path.name == ".git" else Path(root))


def auto_memory_dir(root: Path) -> Path:
    """Where Claude Code keeps auto memory for the project at `root` on this machine."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    repo = _repo_root(str(Path(root).resolve()))
    key = "".join(c if c.isascii() and c.isalnum() else "-" for c in repo)
    return base / "projects" / key / "memory"


def local_path(root: Path, rel: str) -> Path:
    """The file on this machine for a satellite-relative memory path (auto memory lives elsewhere)."""
    head, _, rest = rel.partition("/")
    if head == AUTO_MEMORY and rest:
        return auto_memory_dir(root) / rest
    return Path(root) / rel


def _globs(kind: str, config: dict) -> list[str]:
    base = GLOBAL_GLOBS if kind == "global" else PROJECT_GLOBS
    return base + list(config.get("include", []))


def is_memory_path(rel: str, kind: str, config: dict | None = None) -> bool:
    """True if a relative POSIX path is a memory file for this satellite kind."""
    config = config or {}
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts or any(s in FORBIDDEN_SEGMENTS for s in p.parts):
        return False
    if any(fnmatch.fnmatch(rel, pat) for pat in config.get("exclude", [])):
        return False
    if kind != "global" and p.parts[0] == AUTO_MEMORY:
        return len(p.parts) > 1 and p.suffix == ".md"
    if kind != "global":
        if any(s in PRUNE for s in p.parts[:-1]):
            return False
        if p.name in BASENAMES:
            return True
    return any(fnmatch.fnmatch(rel, pat) for pat in _globs(kind, config))


def _is_text(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_BYTES:
            return False
        with path.open("rb") as fh:
            return b"\0" not in fh.read(8192)
    except OSError:
        return False


def _static_prefix(pattern: str) -> str:
    parts = []
    for part in PurePosixPath(pattern).parts:
        if any(c in part for c in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


class Scan(NamedTuple):
    files: list  # regular memory files, plus followed global links (relative POSIX paths)
    links: dict  # {link relpath: target relpath}: symlinks between memory files, kept as links
    warnings: list  # [(relpath, reason)]: symlinked memories that are neither recorded nor followed
    followed: dict  # {link relpath: target relpath}: global dotfile links stored by content


def _followable(target: Path, trel: str, name: str) -> str:
    """Why a global link to a non-memory file inside $HOME may NOT be followed ("" if it may)."""
    parts = PurePosixPath(trel).parts
    if any(p in FORBIDDEN_SEGMENTS or p in SENSITIVE_SEGMENTS for p in parts):
        return f"symlink points into {trel}; not followed"
    if target.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return f"symlink points to {trel}, which is executable; not followed"
    if target.suffix.lower() not in FOLLOW_SUFFIXES and target.name != name:
        return f"symlink points to {trel}, which does not look like a memory file; not followed"
    if not _is_text(target):
        return f"symlink points to {trel}, which is binary or too large; skipped"
    return ""


def classify_link(root: Path, full: Path, kind: str, config: dict | None = None
                  ) -> tuple[str | None, str | None, str]:
    """Classify a symlink at a memory path: (mode, target relpath, reason).

    mode "link": the target is a memory file in the same satellite; recorded as a link.
    mode "follow": global satellite only; the target is a memory-like text file elsewhere in
        $HOME (a dotfiles repo), stored and restored by content through the link.
    mode None: not usable; `reason` says why. Links that leave the satellite are never followed.
    """
    config = config or {}
    try:
        target = full.resolve(strict=True)
    except (OSError, RuntimeError):  # missing target, or a symlink loop (RuntimeError on 3.9)
        return None, None, "broken symlink; skipped"
    try:
        trel = target.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None, None, "symlink points outside the satellite; not followed"
    if not target.is_file():
        return None, None, "symlink does not point to a file; skipped"
    if is_memory_path(trel, kind, config):
        if not _is_text(target):
            return None, None, f"symlink points to {trel}, which is binary or too large; skipped"
        return "link", trel, ""
    if kind != "global":
        return None, None, f"symlink points to {trel}, which is not a memory file; skipped"
    why = _followable(target, trel, full.name)
    return (None, None, why) if why else ("follow", trel, "")


def followed_target(root: Path, full: Path, config: dict | None = None) -> str | None:
    """The $HOME-relative target if `full` is a global dotfile link that may be written through."""
    if not full.is_symlink():
        return None
    mode, trel, _ = classify_link(root, full, "global", config)
    return trel if mode == "follow" else None


def scan(root: Path, kind: str = "project", config: dict | None = None) -> Scan:
    """Find memories under `root`. See `Scan` for what each field holds."""
    config = config or {}
    root = Path(root)
    found: set[str] = set()
    links: dict[str, str] = {}
    followed: dict[str, str] = {}
    warnings: set[tuple[str, str]] = set()

    def consider(full: Path) -> None:
        rel = full.relative_to(root).as_posix()
        if full.is_symlink():
            if is_memory_path(rel, kind, config):
                mode, target, why = classify_link(root, full, kind, config)
                if mode == "link":
                    links[rel] = target
                elif mode == "follow":
                    found.add(rel)
                    followed[rel] = target
                else:
                    warnings.add((rel, why))
            return
        if full.is_file() and is_memory_path(rel, kind, config) and _is_text(full):
            found.add(rel)

    def walk(start: Path) -> None:
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d not in PRUNE
                           and not (d == AUTO_MEMORY and Path(dirpath) == root)]
            for name in filenames:
                consider(Path(dirpath) / name)

    if kind != "global":
        walk(root)  # basenames anywhere + project globs
        auto = auto_memory_dir(root)
        for dirpath, _, filenames in os.walk(auto) if auto.is_dir() else ():
            for name in filenames:
                full = Path(dirpath) / name
                rel = f"{AUTO_MEMORY}/{full.relative_to(auto).as_posix()}"
                if not is_memory_path(rel, kind, config):
                    continue
                if full.is_symlink():
                    warnings.add((rel, "symlink in Claude Code auto memory; skipped"))
                elif full.is_file() and _is_text(full):
                    found.add(rel)
    else:
        roots = set()
        for pat in _globs(kind, config):
            prefix = _static_prefix(pat)
            target = root / prefix if prefix else root
            if prefix == pat:
                if os.path.lexists(target):
                    consider(target)
            elif target.is_dir() and target != root:
                roots.add(target)
        for r in sorted(roots):
            walk(r)
    return Scan(sorted(found), dict(sorted(links.items())), sorted(warnings), dict(sorted(followed.items())))


def count_files(root: Path, limit: int) -> int:
    """Files under `root` (skipping PRUNE dirs, like discovery), counting no further than `limit`."""
    n = 0
    for _, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE]
        n += len(filenames)
        if n >= limit:
            return limit
    return n


def discover(root: Path, kind: str = "project", config: dict | None = None) -> list[str]:
    """Return sorted relative POSIX paths of memory files stored by content under `root`."""
    return scan(root, kind, config)[0]
