"""Find AI memory files (CLAUDE.md, AGENTS.md, napkins, cursor rules, ...)."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath

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


def discover(root: Path, kind: str = "project", config: dict | None = None) -> list[str]:
    """Return sorted relative POSIX paths of memory files under `root`."""
    config = config or {}
    root = Path(root)
    found: set[str] = set()

    def consider(full: Path) -> None:
        if full.is_symlink() or not full.is_file():
            return
        rel = full.relative_to(root).as_posix()
        if is_memory_path(rel, kind, config) and _is_text(full):
            found.add(rel)

    def walk(start: Path) -> None:
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d not in PRUNE]
            for name in filenames:
                consider(Path(dirpath) / name)

    if kind != "global":
        walk(root)  # basenames anywhere + project globs
    else:
        roots = set()
        for pat in _globs(kind, config):
            prefix = _static_prefix(pat)
            target = root / prefix if prefix else root
            if prefix == pat:
                consider(target)
            elif target.is_dir() and target != root:
                roots.add(target)
        for r in sorted(roots):
            walk(r)
    return sorted(found)
