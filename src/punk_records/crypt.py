"""Encrypted remotes: the store stays plain text locally, the remote only ever sees an age file.

`punk encrypt init <url>` sets up a machine-local bare repo at `.git/punk-crypt` inside the store,
pointing at <url>. Syncing then works like an air-gapped bundle exchange, over git:

    pull:  fetch the remote's single file `punk-records.bundle.age`, decrypt it with this machine's
           age key, and `git pull` the bundle into the store (merges, including union merges, happen
           here, in plain text)
    push:  bundle the store, encrypt it, commit it as the only file of a parentless commit, and push
           with --force-with-lease, so a push that raced another machine is refused, re-merged and
           retried, and the remote never grows beyond one encrypted file

Who can read it: this machine's own key plus recipients added *on this machine* (`.git/punk-state.json`).
Recipients are never synced: age does not authenticate senders, so anyone able to write to the remote
could otherwise add their own key to a synced list and read every later push.

age provides confidentiality, not authenticity: a hostile remote can still upload a bundle encrypted
to your public key. The usual restore rules (memory paths only) still apply to what it contains.
Requires the `age` and `age-keygen` programs (https://age-encryption.org); no Python dependencies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .store import PunkError, Store, locked_store

BUNDLE = "punk-records.bundle.age"
REF = "refs/heads/main"
PLAIN_REMOTE = ("this store also has a plain git remote ({names}); syncing would push memories "
                "unencrypted. Remove it (`punk git remote remove {first}`) or stop using encryption.")


def key_path() -> Path:
    return Path(os.environ.get("PUNK_AGE_KEY") or Path.home() / ".config" / "punk" / "age.key").expanduser()


def _need_age() -> None:
    missing = [t for t in ("age", "age-keygen") if not shutil.which(t)]
    if missing:
        raise PunkError(f"encryption needs {' and '.join(missing)} on PATH: install age "
                        f"(https://age-encryption.org, e.g. `brew install age` or `apt install age`)")


def _run(args: list[str], data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=data, capture_output=True)


def _err(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()


class Crypt:
    def __init__(self, store: Store):
        self.store = store
        self.dir = store.root / ".git" / "punk-crypt"
        self.tmp = store.root / ".git" / "punk-crypt-tmp"

    # ----------------------------------------------------------------- basics
    @property
    def enabled(self) -> bool:
        return (self.dir / "HEAD").is_file()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = _run(["git", "--git-dir", str(self.dir), *args])
        if check and r.returncode != 0:
            raise PunkError(f"git {' '.join(args)} (encrypted remote) failed:\n{_err(r)}")
        return r

    def url(self) -> str:
        return self.git("config", "--get", "remote.origin.url", check=False).stdout.decode().strip()

    def ensure_key(self) -> tuple[Path, bool]:
        path = key_path()
        if path.exists():
            return path, False
        _need_age()
        path.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["age-keygen", "-o", str(path)])
        if r.returncode != 0:
            raise PunkError(f"age-keygen failed:\n{_err(r)}")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path, True

    def public_key(self) -> str:
        path, _ = self.ensure_key()
        r = _run(["age-keygen", "-y", str(path)])
        if r.returncode != 0:
            raise PunkError(f"cannot read the age key {path}:\n{_err(r)}")
        return r.stdout.decode().strip()

    def extra_recipients(self) -> list[str]:
        return list(self.store.load_state().get("crypt_recipients", []))

    def recipients(self) -> list[str]:
        return [self.public_key(), *self.extra_recipients()]

    def _check_plain_remotes(self) -> None:
        names = self.store.git("remote").stdout.split()
        if names:
            raise PunkError(PLAIN_REMOTE.format(names=", ".join(names), first=names[0]))

    # ------------------------------------------------------------------ setup
    @locked_store
    def setup(self, url: str) -> None:
        self.store.require()
        _need_age()
        self._check_plain_remotes()
        if self.enabled:
            raise PunkError(f"an encrypted remote is already set up ({self.url()}); see `punk encrypt status`")
        self.ensure_key()
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "init", "-q", "--bare", str(self.dir)])
        if r.returncode != 0:
            raise PunkError(f"git init failed:\n{_err(r)}")
        self.git("remote", "add", "origin", url)
        self.git("config", "user.name", "punk")
        self.git("config", "user.email", "punk@punk-records.invalid")

    @locked_store
    def add_recipient(self, recipient: str) -> None:
        recipient = recipient.strip()
        _need_age()
        r = _run(["age", "-r", recipient], data=b"")  # let age itself validate the recipient
        if not recipient.startswith("age1") or r.returncode != 0:
            raise PunkError(f"not an age public key: {recipient!r} (it starts with age1...)")
        state = self.store.load_state()
        extra = state.setdefault("crypt_recipients", [])
        if recipient not in extra and recipient != self.public_key():
            extra.append(recipient)
            self.store.save_state(state)

    # --------------------------------------------------------------- exchange
    def remote_head(self) -> str:
        out = self.git("ls-remote", "origin", REF).stdout.decode().split()
        return out[0] if out else ""

    def _scratch(self) -> Path:
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True)
        return self.tmp

    def pull(self, head: str) -> None:
        """Decrypt the remote bundle at `head` and merge it into the store."""
        self.git("fetch", "-q", "origin", f"+{REF}:refs/remotes/origin/main")
        data = self.git("cat-file", "blob", f"{head}:{BUNDLE}").stdout
        tmp = self._scratch()
        try:
            (tmp / "in.age").write_bytes(data)
            r = _run(["age", "-d", "-i", str(key_path()), "-o", str(tmp / "in.bundle"), str(tmp / "in.age")])
            if r.returncode != 0:
                raise PunkError(
                    f"cannot decrypt the remote's {BUNDLE} with {key_path()} ({_err(r)}). Copy the key file "
                    f"from a machine that can read it, or run `punk encrypt add-recipient "
                    f"{self.public_key()}` on such a machine and sync there first.")
            bundle = str(tmp / "in.bundle")
            self.store.git("bundle", "verify", "-q", bundle)
            heads = [ln.split()[1] for ln in self.store.git("bundle", "list-heads", bundle).stdout.splitlines()
                     if len(ln.split()) == 2 and ln.split()[1].startswith("refs/heads/")]
            if not heads:
                raise PunkError(f"the remote's {BUNDLE} contains no branches")
            mine = f"refs/heads/{self.store.branch()}"
            self.store.pull(bundle, mine if mine in heads else heads[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def push(self, expected: str) -> bool:
        """Encrypt the store and replace the remote file. False if the remote moved since `expected`."""
        tmp = self._scratch()
        try:
            bundle, enc = tmp / "out.bundle", tmp / "out.age"
            self.store.git("bundle", "create", "-q", str(bundle), self.store.branch())
            args = ["age", "-e", "-o", str(enc)]
            for r in self.recipients():
                args += ["-r", r]
            r = _run([*args, str(bundle)])
            if r.returncode != 0:
                raise PunkError(f"age could not encrypt the store:\n{_err(r)}")
            blob = self.git("hash-object", "-w", str(enc)).stdout.decode().strip()
            tree = _run(["git", "--git-dir", str(self.dir), "mktree"], data=f"100644 blob {blob}\t{BUNDLE}\n".encode())
            if tree.returncode != 0:
                raise PunkError(f"git mktree failed:\n{_err(tree)}")
            commit = self.git("commit-tree", tree.stdout.decode().strip(), "-m", "punk records (encrypted)")
            commit = commit.stdout.decode().strip()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        r = self.git("push", "-q", f"--force-with-lease={REF}:{expected}", "origin", f"{commit}:{REF}", check=False)
        if r.returncode == 0:
            return True
        if self.remote_head() != expected:
            return False  # another machine pushed in between: caller re-merges and retries
        raise PunkError(f"could not push to the encrypted remote:\n{_err(r)}")

    @locked_store
    def exchange(self, push: bool = True, attempts: int = 3) -> bool:
        self._check_plain_remotes()
        for _ in range(attempts):
            head = self.remote_head()
            if head:
                self.pull(head)
            if not push or self.push(head):
                return True
        raise PunkError("the encrypted remote kept changing while pushing; run `punk sync` again")
