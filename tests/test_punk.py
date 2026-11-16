import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from punk_records import secrets  # noqa: E402
from punk_records.discover import discover, is_memory_path  # noqa: E402
from punk_records.mcp import handle  # noqa: E402
from punk_records.store import Store, normalize_remote  # noqa: E402

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
                             input=msgs, capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=True).stdout
        replies = [json.loads(x) for x in out.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])


if __name__ == "__main__":
    unittest.main()
