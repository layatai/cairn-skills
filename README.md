# Cairn Skills

Reusable agent skills with executable helpers and focused tests.

## Create CoW Worktree

[`create-cow-worktree`](skills/create-cow-worktree/SKILL.md) creates isolated Git linked worktrees and uses filesystem copy-on-write when it can verify that the optimization is supported and compatible with the repository.

- Use native clone operations on macOS and Linux.
- Fall back to ordinary Git checkout when the filesystem or repository is unsuitable.
- Preserve branch intent, checkout hooks, content transformations, and existing user work.
- Verify cloned contents against a pinned Git commit.
- Report actual clone operations and logical bytes cloned without inventing disk-savings estimates.

The initial optimization reuses tracked files from a clean worktree at the same commit. Different commits and advanced checkout settings use ordinary Git. Ignored dependency directories and generated files are not copied.

### Use with an agent

Copy the complete `skills/create-cow-worktree` directory into the skills directory used by your agent, following that agent's installation instructions. Keep its `scripts`, `references`, and `agents` directories together. Alternatively, direct an agent that supports local skill files to the skill's `SKILL.md`.

Example request:

> Use create-cow-worktree to create a new worktree for this task, preferring copy-on-write when available.

### Run the helper directly

Requirements: Python 3.9 or newer and Git on `PATH`. Backend selection is automatic. Linux uses Python's `FICLONE` support when exposed, or GNU `cp --reflink=always` where available. Other platforms can use the ordinary Git path.

```bash
python3 skills/create-cow-worktree/scripts/cow_worktree.py create \
  --repo /path/to/repo \
  --path /path/to/repo-task \
  --new-branch agent/task-name \
  --start-point HEAD \
  --cow auto \
  --json
```

Use `--branch existing-local-branch` instead of `--new-branch` to attach an existing branch, and omit `--start-point` in that case. Use `--detach` for a detached worktree. Add `--track origin/main` with a new branch when upstream tracking is intended.

| Mode | Result |
| --- | --- |
| `--cow auto` | Attempt CoW and use ordinary Git when unavailable. |
| `--cow required` | Require the CoW path or return an error. |
| `--cow off` | Use ordinary Git. |
| `--dry-run` | Inspect eligibility without writing a worktree; filesystem support remains unprobed. |

The helper returns structured creation or error information with `--json`. Treat a reported fallback as an ordinary Git checkout, not as proof of cloning. See the [platform reference](skills/create-cow-worktree/references/platforms.md) and [Git behavior reference](skills/create-cow-worktree/references/git-semantics.md) for limits and implementation choices.

### Run tests

```bash
python3 -m unittest discover \
  -s skills/create-cow-worktree/scripts \
  -p 'test_*.py' \
  -v
```

Run native clone tests on their actual platforms and filesystems. Unsupported environments may skip those cases; mocked backend tests do not establish native-platform support.

Set `COW_REQUIRE_REAL=1` when testing on a filesystem that must support clones; the native checks then fail instead of skipping. The supplied CI workflow covers Python 3.9 and 3.12 on Linux, macOS, and Windows, requires actual APFS clones on macOS, and uses a disposable Btrfs loopback filesystem for Linux reflink checks. The filesystem setup belongs only to CI; the helper never installs tools or mounts filesystems.
