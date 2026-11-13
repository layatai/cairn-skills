#!/usr/bin/env python3
"""Integration tests for the public CoW worktree CLI.

All repositories, branches, hooks, and checkout files are disposable. The tests
derive expected contents from their own fixtures, not from helper internals.
Run with: python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

from __future__ import annotations

import importlib
import importlib.util
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent
HELPER = SCRIPTS / "cow_worktree.py"


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cow-worktree-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "source with spaces"
        self.destination = self.root / "task with spaces"
        # Ignore machine-wide Git settings without changing HOME or user config.
        self.env = dict(os.environ)
        for name in list(self.env):
            if name.startswith("GIT_"):
                del self.env[name]
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.command("git", "init", "-b", "main", str(self.repo))
        self.git("config", "user.name", "CoW Worktree Tests")
        self.git("config", "user.email", "cow-tests@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write("plain.txt", b"committed content\n")
        self.write("nested/space name.txt", b"nested committed content\n")
        self.write(".gitignore", b"ignored/\n")
        self.commit("Initial fixture")
        self.initial_commit = self.git("rev-parse", "HEAD").stdout.decode().strip()

    def command(self, *arguments, check=True, cwd=None, input=None):
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=self.env,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if check and completed.returncode:
            self.fail(
                f"Command failed: {arguments!r}\n"
                f"stdout: {completed.stdout.decode(errors='replace')}\n"
                f"stderr: {completed.stderr.decode(errors='replace')}"
            )
        return completed

    def git(self, *arguments, repo=None, check=True, input=None):
        return self.command(
            "git", "-C", str(repo or self.repo), *arguments, check=check, input=input
        )

    def write(self, relative, contents):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-m", message)

    def source_state(self):
        index = Path(self.git("rev-parse", "--git-path", "index").stdout.decode().strip())
        if not index.is_absolute():
            index = self.repo / index
        paths = self.git("ls-files", "-z").stdout.split(b"\0")
        files = {}
        for raw in paths:
            if not raw:
                continue
            path = self.repo / os.fsdecode(raw)
            if path.is_symlink():
                files[raw] = ("symlink", os.fsencode(os.readlink(path)))
            elif path.is_file():
                files[raw] = ("file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            elif path.is_dir():
                files[raw] = ("directory",)
            else:
                files[raw] = ("missing",)
        return {
            "head": self.git("rev-parse", "HEAD").stdout,
            "index": index.read_bytes(),
            "files": files,
        }

    def repository_state(self):
        return {
            "source": self.source_state(),
            "worktrees": self.git("worktree", "list", "--porcelain", "-z").stdout,
            "refs": self.git("for-each-ref", "--format=%(refname):%(objectname)").stdout,
        }

    def invoke(self, *arguments, success=True, parse=True, destination=None):
        self.assertTrue(HELPER.is_file(), f"Missing implementation: {HELPER}")
        result = self.command(
            sys.executable,
            str(HELPER),
            "create",
            "--repo", str(self.repo),
            "--path", str(destination or self.destination),
            *arguments,
            "--json",
            check=False,
        )
        diagnostic = (
            f"stdout: {result.stdout.decode(errors='replace')}\n"
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
        if success:
            self.assertEqual(result.returncode, 0, diagnostic)
        else:
            self.assertNotEqual(result.returncode, 0, diagnostic)
        if not parse:
            return result
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.fail(f"CLI did not emit one JSON document.\n{diagnostic}")
        if not success:
            self.assertEqual(payload.get("status"), "error", diagnostic)
            self.assertTrue(payload.get("error"), diagnostic)
        return payload

    def assert_checkout(self, destination=None, commit=None):
        destination = destination or self.destination
        commit = commit or self.initial_commit
        self.assertTrue((destination / ".git").is_file())
        self.assertEqual(
            self.git("rev-parse", "HEAD", repo=destination).stdout.decode().strip(),
            commit,
        )
        self.assertEqual(self.git("status", "--porcelain=v1", "-z", repo=destination).stdout, b"")
        self.assertEqual(
            self.git("write-tree", repo=destination).stdout,
            self.git("rev-parse", f"{commit}^{{tree}}").stdout,
        )
        source_gitdir = self.git("rev-parse", "--absolute-git-dir").stdout
        destination_gitdir = self.git("rev-parse", "--absolute-git-dir", repo=destination).stdout
        self.assertNotEqual(source_gitdir, destination_gitdir)


class WorktreeIntegrationTests(RepositoryFixture):
    def test_off_creates_clean_worktree_and_preserves_source(self):
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/off", "--cow", "off")
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual(result["commit"], self.initial_commit)
        self.assertEqual(result["branch"], "agent/off")
        self.assertEqual(result["cloned_files"], 0)
        self.assertEqual(result["logical_bytes_cloned"], 0)
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)

    def test_detached_worktree(self):
        result = self.invoke("--detach", "--cow", "off")
        self.assertIsNone(result["branch"])
        self.assert_checkout()
        self.assertNotEqual(self.git("symbolic-ref", "-q", "HEAD", repo=self.destination, check=False).returncode, 0)

    def test_existing_branch(self):
        self.git("branch", "existing-task")
        result = self.invoke("--branch", "existing-task", "--cow", "off")
        self.assertEqual(result["branch"], "existing-task")
        self.assert_checkout()

    def test_requested_upstream_is_preserved(self):
        remote = self.root / "remote.git"
        self.command("git", "clone", "--bare", str(self.repo), str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("fetch", "origin")
        result = self.invoke(
            "--new-branch", "agent/tracked", "--start-point", "origin/main",
            "--track", "origin/main", "--cow", "off",
        )
        self.assertEqual(result["commit"], self.initial_commit)
        self.assertEqual(self.git("config", "branch.agent/tracked.remote").stdout, b"origin\n")
        self.assertEqual(self.git("config", "branch.agent/tracked.merge").stdout, b"refs/heads/main\n")
        self.assert_checkout()

    @unittest.skipIf(os.name == "nt", "POSIX executable hook fixture")
    def test_automatic_remote_upstream_is_visible_to_checkout_hook(self):
        remote = self.root / "remote.git"
        self.command("git", "clone", "--bare", str(self.repo), str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("fetch", "origin")
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(
            "#!/bin/sh\n"
            "git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' "
            "> upstream-in-hook.txt || exit 23\n"
        )
        hook.chmod(0o755)
        before = self.source_state()
        result = self.invoke(
            "--new-branch", "agent/automatic-upstream", "--start-point", "origin/main",
            "--cow", "auto",
        )
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual((self.destination / "upstream-in-hook.txt").read_bytes(), b"origin/main\n")
        self.assertEqual(self.git("config", "branch.agent/automatic-upstream.remote").stdout, b"origin\n")
        self.assertEqual(self.git("config", "branch.agent/automatic-upstream.merge").stdout, b"refs/heads/main\n")
        self.assertEqual(self.git("rev-parse", "HEAD", repo=self.destination).stdout.decode().strip(), self.initial_commit)
        self.assertEqual(self.source_state(), before)

    def test_existing_branch_rejects_explicit_start_point(self):
        self.git("branch", "existing-task")
        before = self.repository_state()
        self.invoke("--branch", "existing-task", "--start-point", "HEAD", "--cow", "off", success=False, parse=False)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.repository_state(), before)

    def test_new_branch_collision_preserves_branch(self):
        self.git("branch", "existing-task")
        before = self.repository_state()
        self.invoke("--new-branch", "existing-task", "--cow", "off", success=False)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.repository_state(), before)

    def test_checked_out_branch_is_not_forced(self):
        before = self.repository_state()
        self.invoke("--branch", "main", "--cow", "off", success=False)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.repository_state(), before)

    def test_existing_destination_is_preserved(self):
        self.destination.mkdir()
        sentinel = self.destination / "keep.txt"
        sentinel.write_bytes(b"existing user data\n")
        before = self.repository_state()
        self.invoke("--new-branch", "agent/collision", "--cow", "auto", success=False)
        self.assertEqual(sentinel.read_bytes(), b"existing user data\n")
        self.assertEqual(list(self.destination.iterdir()), [sentinel])
        self.assertEqual(self.repository_state(), before)

    def test_existing_empty_destination_is_rejected(self):
        self.destination.mkdir()
        before = self.repository_state()
        self.invoke("--new-branch", "agent/empty", "--cow", "off", success=False)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(self.repository_state(), before)

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_dangling_destination_symlink_is_preserved(self):
        missing = self.root / "missing-target"
        os.symlink(str(missing), self.destination)
        before = self.repository_state()
        self.invoke("--new-branch", "agent/symlink", "--cow", "auto", success=False)
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(os.readlink(self.destination), str(missing))
        self.assertFalse(missing.exists())
        self.assertEqual(self.repository_state(), before)

    def test_nested_destination_is_rejected(self):
        before = self.repository_state()
        nested = self.repo / "nested-worktree"
        self.invoke("--new-branch", "agent/nested", "--cow", "auto", destination=nested, success=False)
        self.assertFalse(nested.exists())
        self.assertEqual(self.repository_state(), before)

    def test_invalid_revision_leaves_no_state(self):
        before = self.repository_state()
        self.invoke("--new-branch", "agent/invalid", "--start-point", "no-such-ref", "--cow", "auto", success=False)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.repository_state(), before)

    def test_ignored_and_untracked_source_files_are_excluded(self):
        self.write("ignored/private.txt", b"ignored local contents")
        self.write("untracked.txt", b"untracked local contents")
        self.write(".env", b"LOCAL_ONLY=not-a-real-secret\n")
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/exclusions", "--cow", "auto")
        self.assertEqual(result["status"], "created")
        for relative in ("ignored", "untracked.txt", ".env"):
            self.assertFalse((self.destination / relative).exists())
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)

    def test_dirty_source_falls_back_to_committed_contents(self):
        self.write("plain.txt", b"uncommitted edits must stay here\n")
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/dirty", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertTrue(result["fallback_reason"])
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)

    def test_staged_changes_fall_back_without_carrying_index(self):
        self.write("plain.txt", b"staged changes\n")
        self.write("staged-new.txt", b"not committed\n")
        self.git("add", "--all")
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/staged", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assertFalse((self.destination / "staged-new.txt").exists())
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)

    def test_required_with_dirty_source_fails_without_creating_worktree(self):
        self.write("plain.txt", b"local edits\n")
        before = self.repository_state()
        self.invoke("--new-branch", "agent/strict", "--cow", "required", success=False)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.repository_state(), before)

    def test_different_requested_commit_uses_git(self):
        self.write("plain.txt", b"newer committed content\n")
        self.commit("Second fixture commit")
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/old", "--start-point", self.initial_commit, "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual(result["commit"], self.initial_commit)
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)

    def test_assume_unchanged_does_not_leak_uncommitted_contents(self):
        self.git("update-index", "--assume-unchanged", "plain.txt")
        self.write("plain.txt", b"untracked in stat checks\n")
        before = self.source_state()
        self.invoke("--new-branch", "agent/assumed", "--cow", "auto")
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)

    def test_dry_run_does_not_create_files_branches_or_worktrees(self):
        before = self.repository_state()
        top_level_before = sorted(path.name for path in self.root.iterdir())
        result = self.invoke("--new-branch", "agent/preview", "--cow", "auto", "--dry-run")
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["commit"], self.initial_commit)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), top_level_before)

    @unittest.skipIf(os.name == "nt", "POSIX executable and symlink semantics")
    def test_executable_symlink_empty_and_unusual_names(self):
        executable = self.write("run.sh", b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        self.write("empty", b"")
        self.write("tieng-Viet-\u0111\u1ea5t.txt", b"unicode filename\n")
        self.write("line\nbreak.txt", b"newline filename\n")
        os.symlink("plain.txt", self.repo / "link-to-plain")
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside file\n")
        os.symlink(str(outside), self.repo / "outside-link")
        self.commit("Fixture file types")
        commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        before = self.source_state()
        self.invoke("--new-branch", "agent/types", "--cow", "auto")
        self.assertEqual((self.destination / "run.sh").stat().st_mode & 0o111, 0o111)
        self.assertEqual((self.destination / "empty").read_bytes(), b"")
        self.assertEqual((self.destination / "line\nbreak.txt").read_bytes(), b"newline filename\n")
        self.assertEqual((self.destination / "tieng-Viet-\u0111\u1ea5t.txt").read_bytes(), b"unicode filename\n")
        self.assertTrue((self.destination / "link-to-plain").is_symlink())
        self.assertEqual(os.readlink(self.destination / "link-to-plain"), "plain.txt")
        self.assertTrue((self.destination / "outside-link").is_symlink())
        self.assertEqual(os.readlink(self.destination / "outside-link"), str(outside))
        self.assertEqual(outside.read_bytes(), b"outside file\n")
        self.assert_checkout(commit=commit)
        self.assertEqual(self.source_state(), before)

    @unittest.skipIf(os.name == "nt", "POSIX executable hook fixture")
    def test_post_checkout_hook_runs_through_normal_git(self):
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text('#!/bin/sh\nprintf "%s\\n" "$3" > hook-ran.txt\n')
        hook.chmod(0o755)
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/hook", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual((self.destination / "hook-ran.txt").read_bytes(), b"1\n")
        self.assertFalse(result["clean"])
        self.assertFalse((self.repo / "hook-ran.txt").exists())
        self.assertEqual(self.source_state(), before)

    @unittest.skipIf(os.name == "nt", "POSIX executable hook fixture")
    def test_custom_hooks_path_is_respected(self):
        hooks = self.root / "custom hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text('#!/bin/sh\nprintf "custom\\n" > custom-hook.txt\n')
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks))
        result = self.invoke("--new-branch", "agent/custom-hook", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual((self.destination / "custom-hook.txt").read_bytes(), b"custom\n")

    @unittest.skipIf(os.name == "nt", "POSIX shell quoting for filter fixture")
    def test_external_filter_runs_through_normal_git(self):
        marker = self.root / "filter-ran.txt"
        filter_script = self.root / "identity-filter.py"
        filter_script.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        )
        filter_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(filter_script))}"
        self.git("config", "filter.cow-test.smudge", filter_command)
        self.git("config", "filter.cow-test.clean", filter_command)
        self.write(".gitattributes", b"*.txt filter=cow-test\n")
        self.commit("External checkout filter")
        commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        marker.unlink(missing_ok=True)
        result = self.invoke("--new-branch", "agent/filter", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertTrue(marker.is_file())
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assert_checkout(commit=commit)

    def test_crlf_checkout_uses_git_conversion(self):
        self.write(".gitattributes", b"*.txt text eol=crlf\n")
        self.commit("Explicit CRLF checkout")
        commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        result = self.invoke("--new-branch", "agent/crlf", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\r\n")
        self.assert_checkout(commit=commit)

    def test_sparse_checkout_uses_git(self):
        self.write("selected/a.txt", b"selected\n")
        self.write("omitted/b.txt", b"omitted\n")
        self.commit("Sparse fixture")
        self.git("sparse-checkout", "init", "--cone")
        self.git("sparse-checkout", "set", "selected")
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/sparse", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertTrue(result["fallback_reason"])
        self.assertEqual(self.source_state(), before)
        self.assertEqual(self.git("status", "--porcelain=v1", repo=self.destination).stdout, b"")

    def test_gitlink_uses_normal_git(self):
        self.write(".gitmodules", b'[submodule "dependency"]\n\tpath = dependency\n\turl = ../dependency\n')
        self.git("add", ".gitmodules")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.initial_commit},dependency")
        (self.repo / "dependency").mkdir()
        self.git("commit", "-m", "Uninitialized submodule fixture")
        commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        before = self.source_state()
        result = self.invoke("--new-branch", "agent/gitlink", "--cow", "auto")
        self.assertEqual(result["mode"], "git-checkout")
        self.assertTrue(result["fallback_reason"])
        self.assert_checkout(commit=commit)
        self.assertEqual(self.source_state(), before)

    def concurrent_creates(self, destinations):
        processes = []
        try:
            for number, destination in enumerate(destinations):
                processes.append(subprocess.Popen(
                    [sys.executable, str(HELPER), "create", "--repo", str(self.repo),
                     "--path", str(destination), "--new-branch", f"agent/concurrent-{number}",
                     "--cow", "off", "--json"],
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                ))
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                try:
                    payload = json.loads(stdout)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.fail(f"Concurrent CLI emitted invalid JSON: {stdout!r}; {stderr!r}")
                results.append((process.returncode, payload, stderr))
            return results
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_concurrent_distinct_worktrees_are_independent(self):
        before = self.source_state()
        destinations = [self.root / "concurrent-a", self.root / "concurrent-b"]
        for returncode, payload, stderr in self.concurrent_creates(destinations):
            self.assertEqual(returncode, 0, f"{payload!r}; {stderr!r}")
            self.assertEqual(payload["status"], "created")
        for destination in destinations:
            self.assert_checkout(destination=destination)
            self.assertEqual((destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assertEqual(self.source_state(), before)

    def test_concurrent_same_destination_preserves_winning_worktree(self):
        before = self.source_state()
        results = self.concurrent_creates([self.destination, self.destination])
        self.assertEqual(sum(code == 0 for code, _, _ in results), 1, repr(results))
        self.assertEqual(sum(payload["status"] == "error" for _, payload, _ in results), 1)
        self.assert_checkout()
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assertEqual(self.source_state(), before)

    def test_real_cow_and_bidirectional_write_isolation(self):
        # A successful forced clone is the evidence that the real filesystem
        # supports CoW. An ordinary copy would also pass isolation assertions.
        sys.path.insert(0, str(SCRIPTS))
        try:
            backend = importlib.import_module("cow_backend")
        finally:
            sys.path.pop(0)
        probe_source = self.root / "probe-source"
        probe_destination = self.root / "probe-destination"
        probe_source.write_bytes(b"nonempty allocated probe" * 4096)
        try:
            backend.clone_file(probe_source, probe_destination)
        except backend.CloneUnavailable as error:
            if os.environ.get("COW_REQUIRE_REAL") == "1":
                self.fail(f"This job requires real filesystem CoW, but cloning failed: {error}")
            self.skipTest(f"Real filesystem CoW unavailable: {error}")
        finally:
            probe_source.unlink(missing_ok=True)
            probe_destination.unlink(missing_ok=True)

        before = self.source_state()
        result = self.invoke("--new-branch", "agent/real-cow", "--cow", "required")
        self.assertEqual(result["mode"], "cow")
        self.assertGreater(result["cloned_files"], 0)
        self.assertGreater(result["logical_bytes_cloned"], 0)
        self.assertTrue(result["backend"])
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)
        source = self.repo / "plain.txt"
        target = self.destination / "plain.txt"
        source_stat, target_stat = source.stat(), target.stat()
        self.assertNotEqual(
            (source_stat.st_dev, source_stat.st_ino),
            (target_stat.st_dev, target_stat.st_ino),
            "Worktree files must not be hardlinks",
        )
        with target.open("r+b") as stream:
            stream.write(b"DEST")
            stream.truncate(4)
        self.assertEqual(source.read_bytes(), b"committed content\n")
        source.write_bytes(b"SOURCE")
        self.assertEqual(target.read_bytes(), b"DEST")


@unittest.skipIf(os.name == "nt", "CoW orchestration is supported on macOS/Linux; Windows uses ordinary Git")
class MockedCloneOrchestrationTests(RepositoryFixture):
    """Exercise Git transactions with independent copies, not physical CoW.

    Only the clone primitive and backend availability are mocked. Assertions
    inspect real Git repositories and real files. No test here is evidence of
    filesystem block sharing; the real backend tests establish that separately.
    Windows deliberately has no CoW backend, so forcing these POSIX setup paths
    there would test unsupported behavior. The ordinary Windows CLI remains
    covered by WorktreeIntegrationTests.
    """

    @classmethod
    def setUpClass(cls):
        if not HELPER.is_file():
            raise unittest.SkipTest(f"Missing implementation: {HELPER}")

    def load_helper(self):
        name = "cow_worktree_orchestration_test_module"
        spec = importlib.util.spec_from_file_location(name, HELPER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        sys.path.insert(0, str(SCRIPTS))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        self.addCleanup(sys.modules.pop, name, None)
        return module

    def stage_for(self, destination):
        directory = Path(destination).parent
        while directory != self.root.parent:
            if (directory / ".git").is_file():
                return directory
            directory = directory.parent
        self.fail(f"No registered stage contains {destination}")

    @staticmethod
    def independent_copy(source, destination):
        if os.path.lexists(destination):
            raise FileExistsError(str(destination))
        shutil.copyfile(source, destination)
        return "test-copy"

    def invoke_with_clone(self, module, clone, *, mode="auto", success=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(module, "backend_name", return_value="test-copy"),
            mock.patch.object(module, "clone_file", side_effect=clone),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = module.main([
                "create", "--repo", str(self.repo), "--path", str(self.destination),
                "--new-branch", "agent/orchestration", "--cow", mode, "--json",
            ])
        diagnostic = f"stdout: {stdout.getvalue()}\nstderr: {stderr.getvalue()}"
        if success is True:
            self.assertEqual(returncode, 0, diagnostic)
        elif success is False:
            self.assertNotEqual(returncode, 0, diagnostic)
        try:
            payload = json.loads(stdout.getvalue())
        except json.JSONDecodeError:
            self.fail(diagnostic)
        if success is not None:
            self.assertEqual(payload["status"], "created" if success else "error", diagnostic)
        return payload

    def test_mocked_clone_preserves_files_through_branch_activation(self):
        module = self.load_helper()
        if os.name != "nt":
            executable = self.write("run.sh", b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            os.symlink("plain.txt", self.repo / "tracked-link")
        self.write("empty", b"")
        self.write("untracked.txt", b"stay in source")
        if os.name != "nt":
            self.git("add", "run.sh", "tracked-link", "empty")
        else:
            self.git("add", "empty")
        self.git("commit", "-m", "Orchestration file types")
        commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        before = self.source_state()
        cloned_inodes = {}

        def clone(source, destination):
            result = self.independent_copy(source, destination)
            stage = self.stage_for(destination)
            info = Path(destination).stat()
            cloned_inodes[Path(destination).relative_to(stage)] = (info.st_dev, info.st_ino)
            return result

        result = self.invoke_with_clone(module, clone, mode="required")
        self.assertEqual(result["mode"], "cow")
        self.assertEqual(result["backend"], "test-copy")
        self.assertGreater(result["cloned_files"], 0)
        self.assertFalse((self.destination / "untracked.txt").exists())
        self.assert_checkout(commit=commit)
        self.assertEqual(self.source_state(), before)
        for relative, expected_inode in cloned_inodes.items():
            info = (self.destination / relative).stat()
            self.assertEqual((info.st_dev, info.st_ino), expected_inode, str(relative))
        if os.name != "nt":
            self.assertEqual((self.destination / "run.sh").stat().st_mode & 0o111, 0o111)
            self.assertTrue((self.destination / "tracked-link").is_symlink())
            self.assertEqual(os.readlink(self.destination / "tracked-link"), "plain.txt")

    def test_mocked_source_mutation_retries_normal_git(self):
        module = self.load_helper()
        before = self.source_state()
        stages = set()

        def clone(source, destination):
            stages.add(self.stage_for(destination))
            if Path(source).name == "plain.txt":
                Path(source).write_bytes(b"concurrent source edit\n")
            return self.independent_copy(source, destination)

        result = self.invoke_with_clone(module, clone)
        self.assertEqual(result["mode"], "git-checkout")
        self.assertTrue(result["fallback_reason"])
        self.assertEqual((self.destination / "plain.txt").read_bytes(), b"committed content\n")
        self.assertEqual((self.repo / "plain.txt").read_bytes(), b"concurrent source edit\n")
        self.assert_checkout()
        after = self.source_state()
        self.assertEqual(after["head"], before["head"])
        self.assertEqual(after["index"], before["index"])
        for stage in stages:
            self.assertFalse(stage.exists())

    def test_mocked_source_mutation_required_rolls_back(self):
        module = self.load_helper()
        before = self.repository_state()
        stages = set()

        def clone(source, destination):
            stages.add(self.stage_for(destination))
            if Path(source).name == "plain.txt":
                Path(source).write_bytes(b"concurrent source edit\n")
            return self.independent_copy(source, destination)

        self.invoke_with_clone(module, clone, mode="required", success=False)
        self.assertFalse(self.destination.exists())
        after = self.repository_state()
        self.assertEqual(after["refs"], before["refs"])
        self.assertEqual(after["worktrees"], before["worktrees"])
        self.assertEqual(after["source"]["index"], before["source"]["index"])
        self.assertEqual((self.repo / "plain.txt").read_bytes(), b"concurrent source edit\n")
        for stage in stages:
            self.assertFalse(stage.exists())

    def test_mocked_fallback_keeps_original_commit_when_source_head_advances(self):
        module = self.load_helper()
        mutated_state = None
        stage = None

        def clone(source, destination):
            nonlocal mutated_state, stage
            stage = self.stage_for(destination)
            self.git("commit", "--allow-empty", "-m", "Concurrent source commit")
            mutated_state = self.source_state()
            raise module.CloneUnavailable("fixture clone unsupported after HEAD advanced")

        result = self.invoke_with_clone(module, clone)
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual(result["commit"], self.initial_commit)
        self.assert_checkout()
        self.assertIsNotNone(mutated_state)
        self.assertNotEqual(mutated_state["head"].decode().strip(), self.initial_commit)
        self.assertEqual(self.source_state(), mutated_state)
        self.assertFalse(stage.exists())

    def test_mocked_failure_preserves_index_only_stage_changes(self):
        module = self.load_helper()
        before = self.source_state()
        stage = expected_index = blob = None

        def clone(source, destination):
            nonlocal stage, expected_index, blob
            stage = self.stage_for(destination)
            blob = self.git("hash-object", "-w", "--stdin", repo=stage,
                            input=b"user staged content without a working-file edit\n").stdout.decode().strip()
            self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},plain.txt", repo=stage)
            expected_index = self.git("ls-files", "--stage", "-z", repo=stage).stdout
            raise OSError(errno.ENOSPC, "fixture failure after index-only edit")

        result = self.invoke_with_clone(module, clone, success=False)
        self.assertIsNotNone(stage)
        self.assertTrue(stage.is_dir(), "Index-only user changes must prevent force-removal")
        self.assertEqual(Path(result["recovery_path"]), stage)
        self.assertEqual(self.git("ls-files", "--stage", "-z", repo=stage).stdout, expected_index)
        self.assertEqual(self.git("rev-parse", ":plain.txt", repo=stage).stdout.decode().strip(), blob)
        self.assertFalse((stage / "plain.txt").exists())
        self.assertEqual(self.source_state(), before)

    def test_mocked_failure_preserves_detached_commit_metadata(self):
        module = self.load_helper()
        before = self.source_state()
        stage = user_commit = None

        def clone(source, destination):
            nonlocal stage, user_commit
            stage = self.stage_for(destination)
            tree = self.git("rev-parse", "HEAD^{tree}", repo=stage).stdout.decode().strip()
            user_commit = self.git("commit-tree", tree, "-p", self.initial_commit, repo=stage,
                                   input=b"Concurrent detached commit\n").stdout.decode().strip()
            self.git("update-ref", "HEAD", user_commit, self.initial_commit, repo=stage)
            raise OSError(errno.EIO, "fixture failure after detached commit")

        result = self.invoke_with_clone(module, clone, success=False)
        self.assertIsNotNone(stage)
        self.assertTrue(stage.is_dir(), "A detached commit must prevent force-removal")
        self.assertEqual(Path(result["recovery_path"]), stage)
        self.assertEqual(self.git("rev-parse", "HEAD", repo=stage).stdout.decode().strip(), user_commit)
        self.assertFalse((stage / "plain.txt").exists())
        self.assertEqual(self.source_state(), before)

    def test_mocked_move_destination_race_reports_exact_recovery_path(self):
        module = self.load_helper()
        before = self.source_state()
        real_git = module.git
        stage = None
        injected = False
        marker = self.destination / "user-marker.txt"

        def clone(source, destination):
            nonlocal stage
            stage = self.stage_for(destination)
            return self.independent_copy(source, destination)

        def racing_git(repo, *arguments, **kwargs):
            nonlocal injected
            if arguments[:2] == ("worktree", "move") and not injected:
                # The caller's existence check has already run. Git interprets
                # an existing destination directory as a parent directory.
                try:
                    self.destination.mkdir()
                except FileExistsError:
                    # A future implementation may reserve the destination
                    # atomically, preventing this competing mkdir entirely.
                    pass
                else:
                    marker.write_bytes(b"preserve the competing user directory\n")
                    injected = True
            return real_git(repo, *arguments, **kwargs)

        with mock.patch.object(module, "git", side_effect=racing_git):
            result = self.invoke_with_clone(module, clone, success=None)
        if injected:
            self.assertEqual(result["status"], "error", "A nested worktree must not be reported at its parent")
            nested = self.destination / stage.name
            self.assertEqual(Path(result["recovery_path"]), nested)
            self.assertTrue((nested / ".git").is_file())
            self.assertEqual(marker.read_bytes(), b"preserve the competing user directory\n")
            self.assertEqual(self.git("rev-parse", "HEAD", repo=nested).stdout.decode().strip(), self.initial_commit)
            self.assertIn(os.fsencode(str(nested)), self.git("worktree", "list", "--porcelain", "-z").stdout)
        else:
            self.assertEqual(result["status"], "created")
            self.assert_checkout()
        self.assertEqual(self.source_state(), before)

    def test_mocked_clone_unavailable_after_partial_progress_falls_back(self):
        module = self.load_helper()
        before = self.source_state()
        stages = set()
        calls = 0

        def clone(source, destination):
            nonlocal calls
            calls += 1
            stages.add(self.stage_for(destination))
            if calls == 2:
                raise module.CloneUnavailable("fixture clone unsupported")
            return self.independent_copy(source, destination)

        result = self.invoke_with_clone(module, clone)
        self.assertEqual(calls, 2)
        self.assertEqual(result["mode"], "git-checkout")
        self.assertEqual(result["cloned_files"], 0)
        self.assert_checkout()
        self.assertEqual(self.source_state(), before)
        for stage in stages:
            self.assertFalse(stage.exists())

    def test_mocked_enospc_is_error_and_owned_stage_is_cleaned(self):
        module = self.load_helper()
        before = self.repository_state()
        stages = set()
        calls = 0

        def clone(source, destination):
            nonlocal calls
            calls += 1
            stages.add(self.stage_for(destination))
            if calls == 2:
                raise OSError(errno.ENOSPC, "fixture no space")
            return self.independent_copy(source, destination)

        self.invoke_with_clone(module, clone, success=False)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.repository_state(), before)
        for stage in stages:
            self.assertFalse(stage.exists())

    def test_mocked_failure_preserves_unknown_file_added_to_stage(self):
        module = self.load_helper()
        before = self.source_state()
        user_file = None
        calls = 0

        def clone(source, destination):
            nonlocal calls, user_file
            calls += 1
            if calls == 2:
                user_file = self.stage_for(destination) / "concurrent-user.txt"
                user_file.write_bytes(b"keep this user work\n")
                raise OSError(errno.EIO, "fixture I/O error")
            return self.independent_copy(source, destination)

        self.invoke_with_clone(module, clone, success=False)
        self.assertIsNotNone(user_file)
        self.assertTrue(user_file.is_file())
        self.assertEqual(user_file.read_bytes(), b"keep this user work\n")
        self.assertTrue((user_file.parent / ".git").is_file())
        self.assertEqual(self.source_state(), before)

    def test_mocked_failure_preserves_edits_to_an_owned_clone(self):
        module = self.load_helper()
        before = self.source_state()
        first_clone = None

        def clone(source, destination):
            nonlocal first_clone
            if first_clone is not None:
                first_clone.write_bytes(b"concurrent edit to keep\n")
                raise OSError(errno.ENOSPC, "fixture no space")
            result = self.independent_copy(source, destination)
            first_clone = Path(destination)
            return result

        self.invoke_with_clone(module, clone, success=False)
        self.assertIsNotNone(first_clone)
        self.assertTrue(first_clone.is_file())
        self.assertEqual(first_clone.read_bytes(), b"concurrent edit to keep\n")
        self.assertTrue((self.stage_for(first_clone) / ".git").is_file())
        self.assertEqual(self.source_state(), before)

if __name__ == "__main__":
    unittest.main(verbosity=2)
