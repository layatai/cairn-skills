#!/usr/bin/env python3
"""Forced filesystem clones; never silently substitute an ordinary copy.

Only regular files are accepted. The caller owns Git modes, content verification,
and fallback policy. Source and destination paths must have canonical parents:
symlink components are rejected, and destinations are created exclusively.
No administrator privileges are needed.
"""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
from typing import Optional, Tuple

try:
    import fcntl
except ImportError:  # Windows still supports the ordinary Git path.
    fcntl = None


class CloneUnavailable(Exception):
    """An expected optimization miss, rather than a failed ordinary file write."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_LINUX_UNSUPPORTED = {
    errno.EXDEV,
    errno.ENOSYS,
    errno.ENOTTY,
    errno.EINVAL,  # FICLONE may reject an otherwise valid regular-file pair.
    errno.EOPNOTSUPP,
    errno.ENOTSUP,
}
_MAC_UNSUPPORTED = {errno.EXDEV, errno.ENOSYS, errno.ENOTSUP}


def _gnu_cp_path() -> Optional[str]:
    """Use descriptor paths on Linux; never hand cp unvalidated source paths."""
    if not Path("/proc/self/fd").is_dir():
        return None
    executable = shutil.which("cp")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, check=False,
            env={**os.environ, "LC_ALL": "C"}, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 and b"GNU coreutils" in result.stdout:
        return executable
    return None


def _mac_clone_function():
    """fclonefileat binds both the source and destination parent by descriptor."""
    try:
        function = ctypes.CDLL(None, use_errno=True).fclonefileat
    except (AttributeError, OSError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def _select_backend() -> Tuple[Optional[str], object]:
    system = platform.system()
    if system == "Linux":
        if fcntl is not None and hasattr(fcntl, "FICLONE"):
            return "linux-ficlone", None
        executable = _gnu_cp_path()
        return ("linux-gnu-cp", executable) if executable else (None, None)
    if system == "Darwin":
        function = _mac_clone_function()
        return ("macos-clonefile", function) if function else (None, None)
    return None, None


def backend_name() -> Optional[str]:
    """Return a candidate backend; this does not probe filesystem support."""
    return _select_backend()[0]


def _identity(info: os.stat_result) -> Tuple[int, int]:
    return info.st_dev, info.st_ino


def _open_parent_nofollow(path: Path) -> int:
    """Pin every directory component without following ancestor symlinks."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _unlink_owned(parent_fd: int, name: str, identity: Tuple[int, int]) -> None:
    """Remove a partial output only while its directory entry is still ours."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(info) == identity:
        os.unlink(name, dir_fd=parent_fd)


def _gnu_cp_clone(executable: str, source_fd: int, destination_fd: int) -> None:
    # These symlinks are kernel descriptor handles for files already opened with
    # O_NOFOLLOW/O_EXCL, not user-controlled symlink paths. Dereferencing them is
    # necessary: --no-dereference would copy the /proc symlink itself.
    result = subprocess.run(
        [executable, "--reflink=always", "--dereference", "--",
         "/proc/self/fd/{}".format(source_fd),
         "/proc/self/fd/{}".format(destination_fd)],
        pass_fds=(source_fd, destination_fd), capture_output=True, check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode == 0:
        return
    message = result.stderr.decode("utf-8", errors="replace").strip()
    # GNU cp exposes errno text, not a structured errno. Match a complete suffix
    # in the C locale, and require that it describes the forced clone operation.
    if "failed to clone " in message:
        for number in _LINUX_UNSUPPORTED:
            if message.endswith(": " + os.strerror(number)):
                raise CloneUnavailable("linux-gnu-cp: " + os.strerror(number))
    raise OSError("Forced GNU cp clone failed (exit {}): {}".format(
        result.returncode, message or "no diagnostic"))


def clone_file(source: Path, destination: Path) -> str:
    """Create an independent CoW clone at an absent destination.

    Return the backend label only after a forced clone succeeds. Raise
    CloneUnavailable for known unsupported/cross-filesystem cases; propagate
    genuine I/O, permissions, disk-space, path and concurrency errors. A failed
    Linux attempt removes only the partial file it created. macOS documents
    fclonefileat as atomic, so failed calls leave no clone to remove.
    """
    source, destination = Path(source), Path(destination)
    selected, implementation = _select_backend()
    if selected is None:
        raise CloneUnavailable("No forced clone backend is available on this host")
    if source.name in {"", ".", ".."} or destination.name in {"", ".", ".."}:
        raise ValueError("Source and destination must name regular files")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CloneUnavailable("O_NOFOLLOW is unavailable on this host")

    source_fd = source_parent_fd = destination_fd = parent_fd = None
    owned_identity = None
    completed = False
    try:
        source_parent_fd = _open_parent_nofollow(source)
        source_fd = os.open(source.name, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                            dir_fd=source_parent_fd)
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise OSError(errno.EINVAL, "Clone source must be a regular file", str(source))
        parent_fd = _open_parent_nofollow(destination)

        if selected == "macos-clonefile":
            # The source FD is already checked and cannot be redirected through
            # a replaced pathname. Flags=0 requires no platform constant guesses.
            result = implementation(source_fd, parent_fd, os.fsencode(destination.name), 0)
            if result != 0:
                number = ctypes.get_errno()
                if number in _MAC_UNSUPPORTED:
                    raise CloneUnavailable("macos-clonefile: " + os.strerror(number))
                raise OSError(number, os.strerror(number), str(destination))
            destination_fd = os.open(destination.name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
            owned_identity = _identity(os.fstat(destination_fd))
        else:
            destination_fd = os.open(
                destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600, dir_fd=parent_fd,
            )
            owned_identity = _identity(os.fstat(destination_fd))
            if selected == "linux-ficlone":
                try:
                    fcntl.ioctl(destination_fd, fcntl.FICLONE, source_fd)
                except OSError as exc:
                    if exc.errno in _LINUX_UNSUPPORTED:
                        raise CloneUnavailable("linux-ficlone: " + os.strerror(exc.errno)) from exc
                    raise
            else:
                _gnu_cp_clone(implementation, source_fd, destination_fd)

        output = os.fstat(destination_fd)
        entry = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(entry) != _identity(output) or not stat.S_ISREG(entry.st_mode):
            raise OSError(errno.ESTALE, "Clone destination changed during creation", str(destination))
        if (not stat.S_ISREG(output.st_mode) or output.st_nlink != 1
                or _identity(output) == _identity(source_info)):
            raise OSError(errno.EINVAL, "Clone is not an independent regular file", str(destination))
        completed = True
        return selected
    finally:
        try:
            if not completed and owned_identity is not None and parent_fd is not None:
                _unlink_owned(parent_fd, destination.name, owned_identity)
        finally:
            for descriptor in (destination_fd, parent_fd, source_fd, source_parent_fd):
                if descriptor is not None:
                    os.close(descriptor)


__all__ = ["CloneUnavailable", "backend_name", "clone_file"]
