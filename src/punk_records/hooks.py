"""Claude Code hooks: restore memories when a session starts, save and sync them when it ends.

`punk install-hooks` merges two command hooks into a Claude Code settings file:

    SessionStart (startup|resume)  ->  punk hook session-start   pull if online, then restore
    SessionEnd   (every reason)    ->  punk hook session-end     save, then pull + push if online

Both act only on the global satellite and the project the session runs in. They use exec form
(`command` + `args`, no shell), with the Python that runs punk, so they work the same under pipx on
macOS, Linux and Windows. See https://code.claude.com/docs/en/hooks for the settings format.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .store import GLOBAL_ID, PunkError, Store, _read_json, _write_text

# event -> (matcher, punk hook action, spinner text)
EVENTS = {
    "SessionStart": ("startup|resume", "session-start", "punk: restoring memories"),
    "SessionEnd": (None, "session-end", "punk: saving memories"),
}
# Seconds. SessionEnd hooks otherwise share a 1.5 s budget, too short for a git push; a per-hook
# timeout raises that budget (Claude Code caps it at 60 s).
TIMEOUT = 30
ACTIONS = tuple(action for _, action, _ in EVENTS.values())
_SHELL_FORM = re.compile(r"\bpunk(_records)?\b.*\bhook\s+session-(start|end)\b")


def settings_path(project: Path | None = None) -> Path:
    """User settings, or the project's personal (not shared) settings file."""
    if project is not None:
        return Path(project) / ".claude" / "settings.local.json"
    return Path.home() / ".claude" / "settings.json"


def handler(store: Store, action: str, status: str) -> dict:
    return {
        "type": "command",
        "command": sys.executable,
        "args": ["-m", "punk_records", "--home", str(store.root.resolve()), "hook", action],
        "timeout": TIMEOUT,
        "statusMessage": status,
    }


def is_ours(h) -> bool:
    if not isinstance(h, dict):
        return False
    args = h.get("args")
    if isinstance(args, list) and "punk_records" in args and args[-2:-1] == ["hook"] and args[-1] in ACTIONS:
        return True
    return isinstance(h.get("command"), str) and "args" not in h and bool(_SHELL_FORM.search(h["command"]))


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = _read_json(path)
    except ValueError as e:
        raise PunkError(f"{path} is not valid JSON ({e}); fix it first, nothing was changed") from None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise PunkError(f"{path} does not look like Claude Code settings (\"hooks\" must be an object); "
                        f"nothing was changed")
    return data


def _strip(settings: dict) -> int:
    """Remove punk's handlers; drop matcher groups, events and "hooks" that end up empty."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                kept = [h for h in g["hooks"] if not is_ours(h)]
                removed += len(g["hooks"]) - len(kept)
                if kept or not g["hooks"]:
                    kept_groups.append({**g, "hooks": kept})
            else:
                kept_groups.append(g)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return removed


def install(store: Store, project: Path | None = None, *, uninstall: bool = False,
            dry_run: bool = False) -> tuple[Path, str]:
    """Merge (or remove) punk's hooks. Returns (settings path, installed|updated|removed|unchanged)."""
    path = settings_path(project)
    before = _load(path)
    after = json.loads(json.dumps(before))
    had_ours = _strip(after) > 0
    if not uninstall:
        hooks = after.setdefault("hooks", {})
        for event, (matcher, action, status) in EVENTS.items():
            group = {"matcher": matcher} if matcher else {}
            group["hooks"] = [handler(store, action, status)]
            hooks.setdefault(event, []).append(group)
    if after == before:
        return path, "unchanged"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.with_name(path.name + ".punk-bak").write_bytes(path.read_bytes())
        tmp = path.with_name(path.name + ".punk-tmp")
        _write_text(tmp, json.dumps(after, indent=2) + "\n")
        tmp.replace(path)
    if uninstall:
        return path, "removed"
    return path, "updated" if had_ours else "installed"


def _satellites(store: Store, cwd: Path) -> list[str]:
    paths = store.load_state()["paths"]
    ids = [GLOBAL_ID] if GLOBAL_ID in paths else []
    project = store.satellite_for(cwd)
    if project and project in paths:
        ids.append(project)
    return ids


def _first_line(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0]


def run(store: Store, action: str, stdin_text: str) -> tuple[int, str, str]:
    """Run one hook. Returns (exit code, stdout, stderr). Never raises: a hook must not break a session.

    SessionStart: stdout is added to Claude's context, so success prints nothing; problems go to the
    user as a JSON systemMessage. SessionEnd: problems exit 2, which shows stderr to the user.
    """
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cwd = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    notes = []
    try:
        if not store.exists():
            return 0, "", ""
        ids = _satellites(store, cwd)
        if not ids:
            return 0, "", ""
        if action == "session-start":
            try:
                store.exchange(push=False)
            except PunkError as e:
                notes.append(f"punk: could not reach the remote ({_first_line(e)}); using the local store")
            actions = store.restore(ids)
            from .team import Team
            notes += [f"punk: could not update subscription {name} ({error})" for name, error in Team(store).update()]
        else:
            actions = store.save(ids)
            try:
                store.exchange()
            except PunkError as e:
                notes.append(f"punk: saved locally but could not sync ({_first_line(e)})")
        bad = [a for a in actions if a.kind in ("conflict", "blocked")]
        notes += [f"punk: {a.kind} {a.sat}/{a.rel}" + (f" ({a.note})" if a.note else "") for a in bad]
        if bad:
            notes.append("Run `punk status`, and `punk diff <satellite> <path>` for conflicts.")
    except Exception as e:  # noqa: BLE001 - report anything, never crash the agent's session
        notes.append(f"punk: hook {action} failed: {_first_line(e)}")
    if not notes:
        return 0, "", ""
    message = "\n".join(notes)
    if action == "session-start":
        return 0, json.dumps({"systemMessage": message}) + "\n", ""
    return 2, "", message + "\n"


def runnable() -> bool:
    """True if the hook command works outside this shell: Claude Code will not have our PYTHONPATH."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-c", "import punk_records"], env=env, capture_output=True)
    return r.returncode == 0


def gitignored(project: Path, rel: str) -> bool | None:
    """True/False if `rel` is git-ignored in `project`, None if `project` is not a git work tree."""
    from .store import _git_run
    r = _git_run(["-C", str(project), "check-ignore", "-q", rel])
    return {0: True, 1: False}.get(r.returncode)
