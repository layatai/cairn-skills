# Platform behavior

## Backend selection

| Host | Candidate backend | Capability evidence |
| --- | --- | --- |
| macOS | `macos-clonefile`: `fclonefileat(2)` through Python `ctypes` | A successful native clone for the actual source/destination pair. |
| Linux with Python exposing `fcntl.FICLONE` | `linux-ficlone`: whole-file clone ioctl | A successful forced clone ioctl. |
| Linux with GNU coreutils and descriptor paths available | `linux-gnu-cp`: `cp --reflink=always` | Successful completion of the forced reflink operation. |
| Other hosts or missing backend prerequisites | No CoW backend | Ordinary Git checkout in `auto` mode. |

Use the backend result, not the filesystem's name, as the evidence. Filesystem pairs, mount boundaries, and individual file properties can affect support. Even a successful diagnostic probe cannot prove that every later file can be cloned.

Python added the Linux `FICLONE` constant in version 3.12 on supported Linux builds. Feature detection permits older Python versions to use the GNU `cp` backend without hardcoding an ioctl value across architectures.

## macOS

Apple documents the clone APIs as creating files with shared data blocks and independent subsequent writes. A clone destination must not already exist. Cross-filesystem and unsupported-volume attempts return errors.

Use individual regular-file clones. Apple discourages cloning whole directory hierarchies directly with these APIs. The helper walks a Git-derived manifest instead.

Do not infer cloning from `cp -c` exit status: Apple's `cp` documentation explicitly allows it to fall back to a normal copy when cloning is unavailable. The native clone call used here has no such silent copy fallback.

The backend binds the source and destination parent through file descriptors. The helper remains responsible for validating path traversal, Git file modes, and the resulting content.

## Linux

`FICLONE` shares the data of a whole regular file with a separate destination. The files must be on the same filesystem. Future writes remain private to the modified file.

Per-file clone atomicity is not an atomic snapshot of the entire worktree. Verify every destination against the pinned commit after cloning.

The GNU backend uses `--reflink=always`, which fails instead of performing an ordinary copy when reflinking is unavailable. It uses descriptor paths for already opened files; dereferencing those descriptor handles is different from following an arbitrary source symlink.

## Failure handling

Treat documented unsupported-operation and cross-filesystem failures as optimization misses. In `auto`, discard only owned incomplete setup state and create an ordinary Git worktree. In `required`, report that the clone requirement could not be satisfied.

Propagate permission, I/O, no-space, unexpected path, and interference errors. Preserve their diagnostics instead of retrying them as plain copies.

A backend may create a partial destination before a failed clone. Remove that file only while its identity still matches the output created by the attempt. Do not overwrite or remove a pre-existing destination.

## Reporting and validation

- Count successful clone operations separately from correctness checks.
- Report logical bytes cloned; do not equate that number with physical bytes saved.
- Distinct inodes rule out hardlinks but do not distinguish a reflink from an ordinary copy.
- An edit-isolation test verifies independent writes but does not prove CoW.
- Do not use `du` or `st_blocks` as a portable exact measure of shared-space savings.
- Test each native backend on its actual platform. Mock success is not native-platform validation.
- Treat empty files as correctness cases; they contain no data blocks whose sharing establishes a useful saving.

## Primary sources

- [Apple clonefile and fclonefileat manual](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/man/man2/clonefile.2)
- [Apple cp manual](https://github.com/apple-oss-distributions/file_cmds/blob/main/cp/cp.1)
- [Linux FICLONE manual](https://man7.org/linux/man-pages/man2/FICLONE.2const.html)
- [Python 3.12 fcntl documentation](https://docs.python.org/3.12/library/fcntl.html)
- [GNU cp documentation](https://www.gnu.org/software/coreutils/manual/html_node/cp-invocation.html)
