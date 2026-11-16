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


def _link_target(root: Path, full: Path, kind: str, config: dict) -> tuple[str | None, str]:
    """Classify a symlink: (target relpath, "") if it may be recorded, else (None, reason).

    Only links whose final target is a memory file inside the satellite root qualify. Links that
    leave the project are never followed.
    """
    try:
        target = full.resolve(strict=True)
    except (OSError, RuntimeError):  # missing target, or a symlink loop (RuntimeError on 3.9)
        return None, "broken symlink; skipped"
    try:
        trel = target.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None, "symlink points outside the satellite; not followed"
    if not target.is_file():
        return None, "symlink does not point to a file; skipped"
    if not is_memory_path(trel, kind, config):
        return None, f"symlink points to {trel}, which is not a memory file; skipped"
    if not _is_text(target):
        return None, f"symlink points to {trel}, which is binary or too large; skipped"
    return trel, ""


def scan(root: Path, kind: str = "project", config: dict | None = None
         ) -> tuple[list[str], dict[str, str], list[tuple[str, str]]]:
    """Find memories under `root`.

    Returns (files, links, warnings): sorted relative POSIX paths of regular memory files,
    {link relpath: target relpath} for symlinked memories that resolve to a memory file inside
    `root`, and [(relpath, reason)] for symlinked memories that cannot be recorded.
    """
    config = config or {}
    root = Path(root)
    found: set[str] = set()
    links: dict[str, str] = {}
    warnings: set[tuple[str, str]] = set()

    def consider(full: Path) -> None:
        rel = full.relative_to(root).as_posix()
        if full.is_symlink():
            if is_memory_path(rel, kind, config):
                target, why = _link_target(root, full, kind, config)
                if target:
                    links[rel] = target
                else:
                    warnings.add((rel, why))
            return
        if full.is_file() and is_memory_path(rel, kind, config) and _is_text(full):
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
                if os.path.lexists(target):
                    consider(target)
            elif target.is_dir() and target != root:
                roots.add(target)
        for r in sorted(roots):
            walk(r)
    return sorted(found), dict(sorted(links.items())), sorted(warnings)


def discover(root: Path, kind: str = "project", config: dict | None = None) -> list[str]:
    """Return sorted relative POSIX paths of regular (non-symlink) memory files under `root`."""
    return scan(root, kind, config)[0]
