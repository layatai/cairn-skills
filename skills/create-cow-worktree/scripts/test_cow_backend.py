#!/usr/bin/env python3
"""Backend contract tests; real filesystem support is reported separately."""

import ctypes
import errno
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import cow_backend as backend


@unittest.skipUnless(os.name == "posix", "Forced clone backends require POSIX; Windows uses Git")
class CloneBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source with space"
        self.destination = self.root / "destination-λ\nfile"
        self.original = bytes(range(256)) * 128
        self.source.write_bytes(self.original)

    @staticmethod
    def simulate_success(destination_fd, operation, source_fd):
        """Simulate only the API boundary; this is not evidence of real CoW."""
        os.write(destination_fd, os.read(source_fd, 1024 * 1024))
        return 0

    def linux(self):
        return mock.patch.object(backend, "_select_backend", return_value=("linux-ficlone", None))

    def fake_fcntl(self, side_effect):
        return mock.patch.object(backend, "fcntl", mock.Mock(FICLONE=123, ioctl=mock.Mock(side_effect=side_effect)))

    def test_actual_clone_isolates_writes_in_both_directions(self):
        if backend.backend_name() is None:
            if os.environ.get("COW_REQUIRE_REAL") == "1":
                self.fail("CI requires a real clone backend on this platform")
            self.skipTest("No forced clone backend on this platform")
        try:
            name = backend.clone_file(self.source, self.destination)
        except backend.CloneUnavailable as exc:
            self.assertFalse(self.destination.exists(), "Unsupported clone left a partial file")
            if os.environ.get("COW_REQUIRE_REAL") == "1":
                self.fail("CI requires real CoW support: " + exc.reason)
            self.skipTest("Actual filesystem clone unavailable: " + exc.reason)
        self.assertIn(name, {"linux-ficlone", "linux-gnu-cp", "macos-clonefile"})
        self.assertEqual(self.destination.read_bytes(), self.original)
        self.assertNotEqual((self.source.stat().st_dev, self.source.stat().st_ino),
                            (self.destination.stat().st_dev, self.destination.stat().st_ino))
        with self.destination.open("r+b") as stream:
            stream.write(b"DESTINATION")
        self.assertEqual(self.source.read_bytes(), self.original)
        copied = self.destination.read_bytes()
        with self.source.open("r+b") as stream:
            stream.seek(100)
            stream.write(b"SOURCE")
        self.assertEqual(self.destination.read_bytes(), copied)
        self.destination.write_bytes(b"truncated")
        self.assertEqual(self.source.stat().st_size, len(self.original))

    def test_successful_ioctl_creates_independent_destination(self):
        with self.linux(), self.fake_fcntl(self.simulate_success):
            self.assertEqual(backend.clone_file(self.source, self.destination), "linux-ficlone")
        self.assertEqual(self.destination.read_bytes(), self.original)
        self.assertNotEqual(self.source.stat().st_ino, self.destination.stat().st_ino)

    def test_unsupported_ioctl_removes_partial_destination(self):
        for number in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL):
            with self.subTest(errno=number), self.linux(), self.fake_fcntl(OSError(number, os.strerror(number))):
                with self.assertRaises(backend.CloneUnavailable) as caught:
                    backend.clone_file(self.source, self.destination)
                self.assertIn(os.strerror(number), caught.exception.reason)
                self.assertFalse(self.destination.exists())
                self.assertEqual(self.source.read_bytes(), self.original)

    def test_real_errors_are_not_relabelled_as_unsupported(self):
        for number in (errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EIO, errno.EBADF):
            with self.subTest(errno=number), self.linux(), self.fake_fcntl(OSError(number, os.strerror(number))):
                with self.assertRaises(OSError) as caught:
                    backend.clone_file(self.source, self.destination)
                self.assertEqual(caught.exception.errno, number)
                self.assertFalse(self.destination.exists())

    def test_existing_destination_is_never_replaced(self):
        self.destination.write_bytes(b"user data")
        with self.linux(), self.fake_fcntl(self.simulate_success):
            with self.assertRaises(FileExistsError):
                backend.clone_file(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"user data")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_existing_destination_symlink_is_never_followed(self):
        self.destination.symlink_to(self.source)
        with self.linux(), self.fake_fcntl(self.simulate_success):
            with self.assertRaises(FileExistsError):
                backend.clone_file(self.source, self.destination)
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_source_symlink_is_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.source)
        with self.linux(), self.fake_fcntl(self.simulate_success):
            with self.assertRaises(OSError):
                backend.clone_file(link, self.destination)
        self.assertFalse(self.destination.exists())

    def test_source_parent_symlink_is_rejected(self):
        link = self.root / "parent-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.linux(), self.fake_fcntl(self.simulate_success):
            with self.assertRaises(OSError):
                backend.clone_file(link / self.source.name, self.destination)
        self.assertFalse(self.destination.exists())

    def test_destination_parent_symlink_is_rejected(self):
        link = self.root / "parent-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.linux(), self.fake_fcntl(self.simulate_success):
            with self.assertRaises(OSError):
                backend.clone_file(self.source, link / self.destination.name)
        self.assertFalse(self.destination.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.linux(), self.fake_fcntl(self.simulate_success):
            with self.assertRaises(OSError) as caught:
                backend.clone_file(fifo, self.destination)
        self.assertEqual(caught.exception.errno, errno.EINVAL)
        self.assertFalse(self.destination.exists())

    def test_cleanup_preserves_replaced_destination(self):
        def replace_and_fail(destination_fd, operation, source_fd):
            self.destination.unlink()
            self.destination.write_bytes(b"someone else's file")
            raise OSError(errno.EIO, "injected failure")
        with self.linux(), self.fake_fcntl(replace_and_fail):
            with self.assertRaises(OSError):
                backend.clone_file(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"someone else's file")

    def test_success_with_replaced_destination_is_not_accepted(self):
        def replace_and_succeed(destination_fd, operation, source_fd):
            self.destination.unlink()
            self.destination.write_bytes(b"someone else's file")
            return 0
        with self.linux(), self.fake_fcntl(replace_and_succeed):
            with self.assertRaises(OSError) as caught:
                backend.clone_file(self.source, self.destination)
        self.assertEqual(caught.exception.errno, errno.ESTALE)
        self.assertEqual(self.destination.read_bytes(), b"someone else's file")

    def test_missing_backend_does_not_create_output(self):
        with mock.patch.object(backend, "_select_backend", return_value=(None, None)):
            with self.assertRaises(backend.CloneUnavailable):
                backend.clone_file(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_gnu_cp_uses_forced_clone_and_open_file_descriptors(self):
        observed = []
        def run(command, **kwargs):
            observed.append(command)
            source_fd, destination_fd = kwargs["pass_fds"]
            self.assertEqual(command[-2:], [f"/proc/self/fd/{source_fd}", f"/proc/self/fd/{destination_fd}"])
            self.assertIn("--reflink=always", command)
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
            self.simulate_success(destination_fd, None, source_fd)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with mock.patch.object(backend, "_select_backend", return_value=("linux-gnu-cp", "/usr/bin/cp")), \
             mock.patch.object(backend.subprocess, "run", side_effect=run):
            self.assertEqual(backend.clone_file(self.source, self.destination), "linux-gnu-cp")
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.destination.read_bytes(), self.original)

    def test_gnu_cp_only_classifies_known_clone_errors_as_unavailable(self):
        for number in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSPC, errno.EIO, errno.EACCES):
            diagnostic = ("cp: failed to clone '/proc/self/fd/4' from '/proc/self/fd/3': " + os.strerror(number) + "\n").encode()
            with self.subTest(errno=number), \
                 mock.patch.object(backend, "_select_backend", return_value=("linux-gnu-cp", "/usr/bin/cp")), \
                 mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", diagnostic)):
                expected = backend.CloneUnavailable if number in (errno.EXDEV, errno.EOPNOTSUPP) else OSError
                with self.assertRaises(expected):
                    backend.clone_file(self.source, self.destination)
                self.assertFalse(self.destination.exists())

    def test_macos_binds_source_and_parent_descriptors(self):
        observed = []
        def clone(source_fd, parent_fd, filename, flags):
            observed.append((filename, flags))
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            try:
                os.write(fd, os.read(source_fd, 1024 * 1024))
            finally:
                os.close(fd)
            return 0
        with mock.patch.object(backend, "_select_backend", return_value=("macos-clonefile", clone)):
            self.assertEqual(backend.clone_file(self.source, self.destination), "macos-clonefile")
        self.assertEqual(observed, [(os.fsencode(self.destination.name), 0)])
        self.assertEqual(self.destination.read_bytes(), self.original)

    def test_macos_errors_are_classified_without_copying(self):
        for number in (errno.EXDEV, errno.ENOTSUP, errno.ENOSPC, errno.EIO, errno.EACCES):
            def clone(source_fd, parent_fd, filename, flags):
                ctypes.set_errno(number)
                return -1
            with self.subTest(errno=number), mock.patch.object(backend, "_select_backend", return_value=("macos-clonefile", clone)):
                expected = backend.CloneUnavailable if number in (errno.EXDEV, errno.ENOTSUP) else OSError
                with self.assertRaises(expected):
                    backend.clone_file(self.source, self.destination)
                self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
