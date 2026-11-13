#!/usr/bin/env python3
"""Create a real Git linked worktree, using verified filesystem clones when possible."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from cow_backend import CloneUnavailable, backend_name, clone_file


class WorktreeError(Exception):
    def __init__(self, message, recovery_path=None):
        super().__init__(message)
        self.recovery_path = recovery_path


class VerificationError(WorktreeError):
    pass


def git_env(readonly=False):
    env = os.environ.copy()
    # -C alone does not override these. Never allow an inherited alternate index
    # or worktree to redirect a mutation away from the explicitly selected repo.
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_NAMESPACE", "GIT_PREFIX"):
        env.pop(key, None)
    if readonly:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo, *args, data=None, check=True, readonly=True):
    command = ["git", "-C", os.fspath(repo)]
    if readonly:
        command += ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
    proc = subprocess.run(command + list(args), input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=git_env(readonly))
    if check and proc.returncode:
        detail = os.fsdecode(proc.stderr).strip() or os.fsdecode(proc.stdout).strip()
        raise WorktreeError("Git {} failed: {}".format(args[0], detail))
    return proc


def line(raw):
    # Strip exactly the protocol's final LF, not LF characters in a pathname.
    return os.fsdecode(raw[:-1] if raw.endswith(b"\n") else raw)


def config(repo, key, default=""):
    result = git(repo, "config", "--get", key, check=False)
    if result.returncode == 1:
        return default
    if result.returncode:
        raise WorktreeError("Cannot read Git configuration: " + key)
    return line(result.stdout)


def config_bool(repo, key, default=False):
    result = git(repo, "config", "--type=bool", "--get", key, check=False)
    if result.returncode == 1:
        return default
    if result.returncode:
        raise WorktreeError("Invalid Git boolean configuration: " + key)
    return line(result.stdout) == "true"


def tracking_policy(repo):
    value = config(repo, "branch.autoSetupMerge", "true").lower()
    if value in ("always", "simple", "inherit"):
        return value
    return "true" if config_bool(repo, "branch.autoSetupMerge", True) else "false"


def common_dir(repo):
    return Path(line(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout)).resolve()


def private_dir(repo):
    return Path(line(git(repo, "rev-parse", "--absolute-git-dir").stdout)).resolve()


def worktrees(repo):
    records = []
    for block in git(repo, "worktree", "list", "--porcelain", "-z").stdout.split(b"\0\0"):
        if not block:
            continue
        item = {}
        for field in block.split(b"\0"):
            if field:
                key, _, value = field.partition(b" ")
                item[os.fsdecode(key)] = os.fsdecode(value)
        if "worktree" in item:
            records.append(item)
    return records


def is_within(path, parent):
    return path == parent or parent in path.parents


def validate_branch(repo, name):
    if not name or name.startswith("-") or git(repo, "check-ref-format", "refs/heads/" + name,
                                               check=False).returncode:
        raise WorktreeError("Invalid branch name: " + str(name))


@dataclass
class Request:
    repo: Path
    path: Path
    common: Path
    commit: str
    new_branch: str | None
    branch: str | None
    detached: bool
    upstream: str | None
    cow: str
    dry_run: bool
    start_point: str
    inherited_remote: str | None = None
    inherited_merges: tuple = ()

    @property
    def selected_branch(self):
        return self.new_branch or self.branch


def resolve_request(args):
    repo = Path(args.repo).expanduser().resolve(strict=True)
    common = common_dir(repo)
    if args.branch and args.start_point is not None:
        raise WorktreeError("--start-point must be omitted with --branch")
    if args.track and not args.new_branch:
        raise WorktreeError("--track requires --new-branch")
    branch = args.new_branch or args.branch
    if branch:
        validate_branch(repo, branch)
    if args.new_branch and git(repo, "show-ref", "--verify", "--quiet",
                               "refs/heads/" + args.new_branch, check=False).returncode == 0:
        raise WorktreeError("Branch already exists: " + args.new_branch)
    start = "refs/heads/" + args.branch if args.branch else (args.start_point or "HEAD")
    commit = line(git(repo, "rev-parse", "--verify", "--end-of-options", start + "^{commit}").stdout)
    supplied = Path(os.path.abspath(os.path.expanduser(args.path)))
    if not supplied.name:
        raise WorktreeError("Destination must name a new worktree directory")
    parent = supplied.parent.resolve(strict=True)
    dest = parent / supplied.name
    if os.path.lexists(dest):
        raise WorktreeError("Destination already exists: " + str(dest))
    for item in worktrees(repo):
        existing = Path(item["worktree"]).resolve()
        if existing == dest:
            raise WorktreeError("Destination is already registered as a worktree")
        if "bare" not in item and is_within(dest, existing):
            raise WorktreeError("Destination must be outside existing worktrees")
        if args.branch and item.get("branch") == "refs/heads/" + args.branch:
            raise WorktreeError("Branch is already checked out: " + args.branch)
    upstream = args.track
    inherited_remote = None
    inherited_merges = ()
    if upstream:
        full = line(git(repo, "rev-parse", "--symbolic-full-name", "--verify", "--end-of-options", upstream).stdout)
        if not full.startswith(("refs/heads/", "refs/remotes/")):
            raise WorktreeError("Tracking target must be a local or remote branch")
        upstream = full
    elif args.new_branch:
        symbolic = git(repo, "rev-parse", "--symbolic-full-name", "--verify", "--end-of-options", start,
                       check=False)
        full = line(symbolic.stdout) if symbolic.returncode == 0 else ""
        policy = tracking_policy(repo)
        if policy == "true" and full.startswith("refs/remotes/"):
            upstream = full
        elif policy == "always" and full.startswith(("refs/heads/", "refs/remotes/")):
            upstream = full
        elif policy == "simple" and full.startswith("refs/remotes/"):
            remote_path = full[len("refs/remotes/"):]
            remotes = sorted(os.fsdecode(git(repo, "remote").stdout).splitlines(), key=len, reverse=True)
            for remote in remotes:
                if remote_path.startswith(remote + "/"):
                    if remote_path[len(remote) + 1:] == args.new_branch:
                        upstream = full
                    break
        elif policy == "inherit" and full.startswith("refs/heads/"):
            prefix = "branch." + full[len("refs/heads/"):] + "."
            inherited_remote = config(repo, prefix + "remote") or None
            merges = git(repo, "config", "--null", "--get-all", prefix + "merge", check=False)
            inherited_merges = tuple(os.fsdecode(v) for v in merges.stdout.split(b"\0") if v)
        elif policy not in ("true", "false", "always", "simple", "inherit"):
            raise WorktreeError("Unsupported branch.autoSetupMerge value: " + policy)
    return Request(repo, dest, common, commit, args.new_branch, args.branch, args.detach,
                   upstream, args.cow, args.dry_run, start, inherited_remote, inherited_merges)


class RepoLock:
    """Serialize this helper's setup only; this does not lock editors or other Git clients."""
    def __init__(self, common):
        self.path = common / "cow-worktree-setup.lock"
        self.fd = None

    def __enter__(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(self.path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(self.fd).st_mode):
            os.close(self.fd)
            self.fd = None
            raise WorktreeError("Setup lock is not a regular file")
        deadline = time.monotonic() + 20
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(self.fd).st_size == 0:
                        os.write(self.fd, b"0")
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    os.close(self.fd)
                    self.fd = None
                    raise
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise WorktreeError("Another worktree setup is busy; retry after it finishes")
                time.sleep(0.05)

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    oid: str


def manifest(repo, commit):
    entries = []
    for record in git(repo, "ls-tree", "-rz", "--full-tree", commit).stdout.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        name = os.fsdecode(path)
        if name.startswith("/") or any(p in ("", ".", "..") or p.lower() == ".git" for p in name.split("/")):
            raise WorktreeError("Unsupported or unsafe committed path")
        entries.append(Entry(name, mode, oid))
    return entries


def compatibility_reason(repo, entries, commit):
    if any(e.mode == "160000" for e in entries):
        return "submodules require ordinary Git checkout"
    if any(e.mode not in ("100644", "100755", "120000") for e in entries):
        return "unsupported committed file type"
    if config_bool(repo, "extensions.worktreeConfig"):
        return "per-worktree configuration requires ordinary Git checkout"
    if config_bool(repo, "core.sparseCheckout") or config_bool(repo, "index.sparse"):
        return "sparse checkout requires ordinary Git checkout"
    if config_bool(repo, "core.splitIndex"):
        return "split index requires ordinary Git checkout"
    if config(repo, "core.fsmonitor", "false").lower() not in ("false", "no", "off", "0"):
        return "filesystem monitor configuration requires ordinary Git checkout"
    if config(repo, "core.autocrlf", "false").lower() not in ("false", "no", "off", "0"):
        return "line-ending conversion requires ordinary Git checkout"
    if config(repo, "core.eol", "native").lower() not in ("lf", "native"):
        return "line-ending configuration requires ordinary Git checkout"
    if any(e.mode == "120000" for e in entries) and not config_bool(repo, "core.symlinks", True):
        return "symlink emulation requires ordinary Git checkout"
    if config(repo, "core.hooksPath"):
        return "custom hook configuration requires ordinary Git checkout"
    hook = Path(line(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/post-checkout").stdout))
    if hook.is_file() and os.access(hook, os.X_OK):
        return "post-checkout hook requires ordinary Git checkout"
    hooks = git(repo, "config", "--get-regexp", r"^hook\.", check=False)
    if hooks.returncode == 0 and hooks.stdout:
        return "configured hooks require ordinary Git checkout"
    if tracking_policy(repo) not in ("true", "false"):
        return "branch tracking policy requires ordinary Git checkout"
    if config(repo, "branch.autoSetupRebase", "never").lower() != "never":
        return "branch rebase policy requires ordinary Git checkout"
    # A path-conditioned include could change semantics after moving the staging
    # worktree. Keep that case on Git's ordinary path instead of approximating it.
    includes = git(repo, "config", "--get-regexp", r"^includeif\.", check=False)
    if includes.returncode == 0 and includes.stdout:
        return "conditional configuration requires ordinary Git checkout"
    if entries:
        names = b"".join(os.fsencode(e.path) + b"\0" for e in entries)
        attrs = git(repo, "check-attr", "--source=" + commit, "-z", "--stdin",
                    "filter", "text", "eol", "working-tree-encoding", "ident", data=names, check=False)
        if attrs.returncode:
            return "Git cannot resolve checkout attributes from the pinned commit"
        fields = attrs.stdout.split(b"\0")
        for i in range(0, len(fields) - 2, 3):
            if fields[i + 2] not in (b"unspecified", b"unset"):
                return "checkout attribute {} requires ordinary Git checkout".format(os.fsdecode(fields[i + 1]))
    if not any(e.mode in ("100644", "100755") and
               line(git(repo, "cat-file", "-s", e.oid).stdout) != "0" for e in entries):
        return "no nonempty regular files to clone"
    return None


def select_source(req, entries):
    if backend_name() is None:
        return None, "no supported filesystem clone backend is available"
    reason = compatibility_reason(req.repo, entries, req.commit)
    if reason:
        return None, reason
    reasons = []
    for item in worktrees(req.repo):
        if item.get("HEAD") != req.commit or "bare" in item or "prunable" in item:
            continue
        source = Path(item["worktree"]).resolve()
        if not source.is_dir():
            continue
        reason = compatibility_reason(source, entries, req.commit)
        if reason:
            reasons.append(reason)
            continue
        if git(source, "status", "--porcelain=v1", "-z", "--untracked-files=no").stdout:
            reasons.append("source worktree has tracked changes")
            continue
        return source, None
    return None, reasons[0] if reasons else "no clean source worktree at the requested commit"


def current_branch(repo):
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return line(result.stdout) if result.returncode == 0 else None


def clean(repo):
    return not git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout


def verify_identity(req, location):
    if line(git(location, "rev-parse", "HEAD").stdout) != req.commit:
        raise VerificationError("Worktree HEAD changed from the pinned commit", str(location))
    if current_branch(location) != req.selected_branch:
        raise VerificationError("Worktree branch does not match the request", str(location))
    if common_dir(location) != req.common:
        raise VerificationError("Worktree is linked to a different repository", str(location))
    if private_dir(location) == req.common:
        raise VerificationError("Destination has no private linked-worktree metadata", str(location))


def result_for(req, mode, **extra):
    result = {"status": "created", "mode": mode, "path": str(req.path), "commit": req.commit,
              "branch": req.selected_branch, "source": None, "backend": None,
              "cloned_files": 0, "logical_bytes_cloned": 0, "fallback_reason": None}
    result.update(extra)
    return result


def setup_tracking(req, repo):
    if req.upstream:
        git(repo, "branch", "--set-upstream-to=" + req.upstream, req.new_branch, readonly=False)
    elif req.inherited_remote and req.inherited_merges:
        prefix = "branch." + req.new_branch + "."
        git(repo, "config", prefix + "remote", req.inherited_remote, readonly=False)
        for merge in req.inherited_merges:
            git(repo, "config", "--add", prefix + "merge", merge, readonly=False)
    remote = config(repo, "branch." + req.new_branch + ".remote")
    rebase = config(repo, "branch.autoSetupRebase", "never").lower()
    if remote and (rebase == "always" or rebase == ("local" if remote == "." else "remote")):
        git(repo, "config", "branch." + req.new_branch + ".rebase", "true", readonly=False)


def ordinary_checkout(req, reason):
    if os.path.lexists(req.path):
        raise WorktreeError("Destination appeared during setup; preserved: " + str(req.path))
    args = ["worktree", "add"]
    if req.new_branch:
        # Create at the pinned OID and configure tracking before checkout: hooks
        # see their expected upstream even if the source branch moves meanwhile.
        git(req.repo, "branch", "--no-track", "--", req.new_branch, req.commit, readonly=False)
        try:
            setup_tracking(req, req.repo)
        except WorktreeError as error:
            raise WorktreeError("Created branch " + req.new_branch + "; tracking setup failed: " + str(error)) from error
    elif req.detached:
        args += ["--detach"]
    start = req.new_branch or req.branch or req.commit
    args += ["--", str(req.path), start]
    proc = git(req.repo, *args, check=False, readonly=False)
    if proc.returncode:
        raise WorktreeError("Ordinary Git worktree creation failed: " + os.fsdecode(proc.stderr).strip(),
                            str(req.path) if os.path.lexists(req.path) else None)
    try:
        verify_identity(req, req.path)
        return result_for(req, "git-checkout", fallback_reason=reason, clean=clean(req.path),
                          verification="git-identity", upstream=req.upstream)
    except WorktreeError as error:
        raise WorktreeError(str(error), str(req.path)) from error


def file_signature(path):
    st = os.lstat(path)
    identity = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if stat.S_ISLNK(st.st_mode):
        return identity, os.fsencode(os.readlink(path))
    if not stat.S_ISREG(st.st_mode):
        raise WorktreeError("Unexpected file type in temporary worktree")
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        end = os.fstat(stream.fileno())
    if (end.st_dev, end.st_ino, end.st_mode, end.st_size, end.st_mtime_ns, end.st_ctime_ns) != identity:
        raise VerificationError("File changed while checking temporary worktree")
    return identity, digest.digest()


class Attempt:
    """Own only a private staging worktree and files explicitly created there."""
    def __init__(self, req):
        self.req = req
        self.path = Path(tempfile.mkdtemp(prefix=".cow-worktree-", dir=req.path.parent))
        self.files = {}
        self.dirs = {"": self.dir_identity(self.path)}
        self.registered = False
        self.owned_branch = False
        self.published = False
        self.metadata = None
        self.git_directory = None

    @staticmethod
    def dir_identity(path):
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise WorktreeError("Expected a directory in temporary worktree")
        return st.st_dev, st.st_ino

    def record(self, path):
        self.files[str(path.relative_to(self.path))] = file_signature(path)

    def metadata_state(self):
        directory = private_dir(self.path)
        if self.git_directory is not None and directory != self.git_directory:
            raise VerificationError("Temporary worktree's Git directory changed")
        files = {}
        for base, dirs, names in os.walk(directory, followlinks=False):
            if any((Path(base) / name).is_symlink() for name in dirs):
                raise VerificationError("Unexpected symbolic link in private Git metadata")
            for name in names:
                path = Path(base) / name
                files[str(path.relative_to(directory))] = file_signature(path)
        return files, line(git(self.path, "rev-parse", "HEAD").stdout), current_branch(self.path)

    def capture_metadata(self, *, with_index=False, branch=None):
        before = self.metadata_state()
        allowed = {"commondir", "gitdir", "HEAD", str(Path("logs") / "HEAD")}
        if with_index:
            allowed.add("index")
        if set(before[0]) - allowed or before[1] != self.req.commit or before[2] != branch:
            raise VerificationError("Unexpected private Git state; temporary worktree preserved", str(self.path))
        if with_index and git(self.path, "diff-index", "--cached", "--quiet", self.req.commit, "--",
                              check=False).returncode:
            raise VerificationError("Temporary index differs from the pinned commit", str(self.path))
        after = self.metadata_state()
        if after != before:
            raise VerificationError("Private Git state changed during verification", str(self.path))
        self.metadata = after

    def assert_metadata_unchanged(self):
        if self.metadata is None or self.metadata_state() != self.metadata:
            raise WorktreeError("Private Git state changed; temporary worktree preserved", str(self.path))

    def registered_location(self):
        link = Path(line((self.git_directory / "gitdir").read_bytes()))
        if not link.is_absolute():
            link = self.git_directory / link
        return link.parent.resolve()

    def parents(self, path):
        relative = path.relative_to(self.path)
        cursor = self.path
        for part in relative.parts[:-1]:
            cursor /= part
            key = str(cursor.relative_to(self.path))
            if key not in self.dirs:
                os.mkdir(cursor)
                self.dirs[key] = self.dir_identity(cursor)
            elif self.dir_identity(cursor) != self.dirs[key]:
                raise VerificationError("Temporary directory changed during setup", str(self.path))

    def unchanged(self):
        try:
            if self.registered:
                self.assert_metadata_unchanged()
            if self.dir_identity(self.path) != self.dirs[""]:
                return False
            found = set()
            for base, dirs, files in os.walk(self.path, followlinks=False):
                for name in list(dirs):
                    path = Path(base) / name
                    key = str(path.relative_to(self.path))
                    if path.is_symlink():
                        dirs.remove(name)
                        files.append(name)
                    elif key not in self.dirs or self.dir_identity(path) != self.dirs[key]:
                        return False
                for name in files:
                    path = Path(base) / name
                    key = str(path.relative_to(self.path))
                    if key not in self.files or file_signature(path) != self.files[key]:
                        return False
                    found.add(key)
            return found == set(self.files)
        except (OSError, WorktreeError):
            return False

    def cleanup(self):
        if self.published:
            return
        if not self.unchanged():
            raise WorktreeError("Setup stopped; temporary worktree contains unexpected changes and was preserved",
                                str(self.path))
        if self.registered:
            git(self.req.repo, "worktree", "remove", "--force", "--", str(self.path), readonly=False)
        else:
            self.path.rmdir()
        if self.owned_branch:
            ref = "refs/heads/" + self.req.new_branch
            if any(item.get("branch") == ref for item in worktrees(self.req.repo)):
                raise WorktreeError("Temporary worktree removed; branch is now used elsewhere and was preserved: " + self.req.new_branch)
            # Compare-and-delete protects a branch advanced by another actor.
            proc = git(self.req.repo, "update-ref", "-d", ref, self.req.commit, check=False, readonly=False)
            if proc.returncode:
                raise WorktreeError("Temporary worktree removed; changed branch preserved: " + self.req.new_branch)


def source_path(source, entry):
    path = source / entry.path
    cursor = source
    for part in Path(entry.path).parts[:-1]:
        cursor /= part
        if not stat.S_ISDIR(os.lstat(cursor).st_mode):
            raise VerificationError("Source parent is no longer a directory")
    return path


def blob_hash(path, algorithm):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError("Expected a regular cloned file")
        digest = hashlib.new(algorithm)
        digest.update(b"blob " + str(before.st_size).encode("ascii") + b"\0")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise VerificationError("Cloned file changed during verification")
    return digest.hexdigest()


def verify_files(req, location, entries):
    algorithm = line(git(req.repo, "rev-parse", "--show-object-format").stdout)
    for entry in entries:
        path = location / entry.path
        st = os.lstat(path)
        if entry.mode == "120000":
            if not stat.S_ISLNK(st.st_mode):
                raise VerificationError("Cloned path has incorrect type: " + entry.path)
            target = os.fsencode(os.readlink(path))
            digest = hashlib.new(algorithm, b"blob " + str(len(target)).encode("ascii") + b"\0" + target).hexdigest()
        else:
            if not stat.S_ISREG(st.st_mode) or bool(st.st_mode & stat.S_IXUSR) != (entry.mode == "100755"):
                raise VerificationError("Cloned path has incorrect type or mode: " + entry.path)
            digest = blob_hash(path, algorithm)
        if digest != entry.oid:
            raise VerificationError("Cloned content differs from the pinned commit: " + entry.path)


def cow_checkout(req, source, entries):
    attempt = Attempt(req)
    count = total = 0
    used = set()
    try:
        git(req.repo, "worktree", "add", "--detach", "--no-checkout", "--", str(attempt.path),
            req.commit, readonly=False)
        attempt.registered = True
        attempt.git_directory = private_dir(attempt.path)
        attempt.record(attempt.path / ".git")
        attempt.capture_metadata()
        reason = compatibility_reason(attempt.path, entries, req.commit)
        if reason:
            raise CloneUnavailable(reason)
        for entry in entries:
            dest = attempt.path / entry.path
            attempt.parents(dest)
            if entry.mode == "120000":
                target = git(req.repo, "cat-file", "blob", entry.oid).stdout
                os.symlink(os.fsdecode(target), dest)
            else:
                src = source_path(source, entry)
                used.add(clone_file(src, dest))
                dst_st = os.lstat(dest)
                src_st = os.lstat(src)
                if (dst_st.st_dev, dst_st.st_ino) == (src_st.st_dev, src_st.st_ino):
                    raise VerificationError("Backend produced a hardlink instead of an independent clone")
                # Git tracks the executable bit, not the source's ownership, ACLs
                # or accidental read-only mode. Apply normal umask-sensitive mode.
                mask = os.umask(0)
                os.umask(mask)
                os.chmod(dest, (0o777 if entry.mode == "100755" else 0o666) & ~mask)
                count += 1
                total += dst_st.st_size
            attempt.record(dest)
        verify_files(req, attempt.path, entries)
        attempt.assert_metadata_unchanged()
        git(attempt.path, "read-tree", req.commit, readonly=False)
        attempt.capture_metadata(with_index=True)
        attempt.assert_metadata_unchanged()
        git(attempt.path, "-c", "core.fsmonitor=false", "update-index", "--refresh", readonly=False)
        attempt.capture_metadata(with_index=True)
        if not clean(attempt.path):
            raise VerificationError("Cloned worktree is not clean")
        if req.new_branch:
            attempt.assert_metadata_unchanged()
            git(attempt.path, "-c", "branch.autoSetupMerge=false", "switch", "--no-guess", "-c",
                req.new_branch, req.commit, readonly=False)
            attempt.owned_branch = True
            attempt.capture_metadata(with_index=True, branch=req.new_branch)
        elif req.branch:
            attempt.assert_metadata_unchanged()
            git(attempt.path, "switch", "--no-guess", "--", req.branch, readonly=False)
            attempt.capture_metadata(with_index=True, branch=req.branch)
        verify_identity(req, attempt.path)
        verify_files(req, attempt.path, entries)
        # Branch attachment must not have rewritten the clones or introduced files.
        if not attempt.unchanged() or not clean(attempt.path):
            raise VerificationError("Temporary worktree changed before publication", str(attempt.path))
        if os.path.lexists(req.path):
            raise WorktreeError("Destination appeared during setup; preserved: " + str(req.path))
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(attempt.path, 0o777 & ~mask)
        git(req.repo, "worktree", "move", "--", str(attempt.path), str(req.path), readonly=False)
        attempt.published = True
        attempt.path = attempt.registered_location()
        if attempt.path != req.path:
            raise WorktreeError("Destination appeared while publishing; worktree preserved at its actual registered path",
                                str(attempt.path))
        verify_identity(req, req.path)
        if req.new_branch:
            setup_tracking(req, req.path)
        return result_for(req, "cow", source=str(source), backend=",".join(sorted(used)),
                          cloned_files=count, logical_bytes_cloned=total, clean=True,
                          verification="full-content-and-git-identity", upstream=req.upstream)
    except BaseException as error:
        if attempt.published:
            raise WorktreeError("Worktree was created but final verification/setup failed: " + str(error),
                                getattr(error, "recovery_path", None) or str(attempt.path)) from error
        try:
            attempt.cleanup()
        except (WorktreeError, OSError) as cleanup_error:
            raise WorktreeError(str(error) + "; " + str(cleanup_error), str(attempt.path)) from error
        raise


def run_create(args):
    req = resolve_request(args)
    if req.dry_run:
        entries = manifest(req.repo, req.commit)
        source, reason = (None, "CoW disabled") if req.cow == "off" else select_source(req, entries)
        return result_for(req, "cow-candidate" if source else "git-checkout", status="planned",
                          source=str(source) if source else None, fallback_reason=reason,
                          capability="unprobed", would_fail=req.cow == "required" and source is None)
    with RepoLock(req.common):
        # Another invocation may have created the destination/branch while waiting.
        req = resolve_request(args)
        if req.cow == "off":
            return ordinary_checkout(req, "CoW disabled")
        entries = manifest(req.repo, req.commit)
        source, reason = select_source(req, entries)
        if source is None:
            if req.cow == "required":
                raise WorktreeError("Copy-on-write required: " + reason)
            return ordinary_checkout(req, reason)
        try:
            return cow_checkout(req, source, entries)
        except (CloneUnavailable, VerificationError) as error:
            if req.cow == "required":
                raise WorktreeError("Copy-on-write required: " + str(error)) from error
            return ordinary_checkout(req, str(error))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Create an isolated Git worktree")
    create.add_argument("--repo", required=True)
    create.add_argument("--path", required=True)
    mode = create.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new-branch")
    mode.add_argument("--branch")
    mode.add_argument("--detach", action="store_true")
    create.add_argument("--start-point", default=None)
    create.add_argument("--track", metavar="UPSTREAM")
    create.add_argument("--cow", choices=("auto", "required", "off"), default="auto")
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run_create(args)
    except (WorktreeError, OSError, CloneUnavailable) as error:
        result = {"status": "error", "error": str(error)}
        recovery = getattr(error, "recovery_path", None)
        if recovery:
            result["recovery_path"] = recovery
        if args.json:
            print(json.dumps(result, ensure_ascii=True))
        else:
            print("Error: " + result["error"], file=sys.stderr)
            if recovery:
                print("Preserved worktree: " + recovery, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print("{} {} ({})".format(result["status"].capitalize(), result["path"], result["mode"]))
        if result["fallback_reason"]:
            print("Reason: " + result["fallback_reason"])
        if result["mode"] == "cow":
            print("{} files cloned; {} logical bytes; backend {}".format(
                result["cloned_files"], result["logical_bytes_cloned"], result["backend"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
