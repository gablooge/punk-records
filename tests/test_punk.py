import json
import os
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
        os.symlink("../CLAUDE.md", p / "docs" / "GEMINI.md")
        os.symlink("README.md", p / "AGENT.md")
        os.symlink(str(self.tmp / "outside.md"), p / "napkin.md")
        os.symlink("missing.md", p / "CLAUDE.local.md")
        os.symlink("CLAUDE.md", p / "notes.txt")  # not a memory path: ignored silently
        files, links, warnings = scan(p)
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
        os.symlink("../CLAUDE.md", pa / "sub" / "AGENTS.md")
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
        self.assertEqual(s.append(sid, "GEMINI.md", "- note"), "GEMINI.md")
        self.assertEqual((self.tmp / "outside.md").read_text(), "elsewhere\n")

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


if __name__ == "__main__":
    unittest.main()
