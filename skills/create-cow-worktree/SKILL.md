---
name: create-cow-worktree
description: Create isolated Git worktrees using filesystem copy-on-write when supported, with verified contents and ordinary Git fallback. Use before creating a worktree for implementation, review, testing, or parallel agent work, or when asked to reduce duplicated worktree data. Supports macOS and Linux clone backends and ordinary Git creation on other platforms.
---

# Create CoW Worktree

Use the bundled helper to create a real Git linked worktree. Prefer `--cow auto` unless the user requires or disables copy-on-write. Treat filesystem cloning as an optimization; preserve the requested Git commit, branch, and checkout behavior.

## Requirements

- Use Python 3.9 or newer and Git available on `PATH`.
- Run commands in the execution environment that owns the repository and destination filesystem.
- Resolve the helper relative to this skill's directory, not the repository being edited.
- Read repository instructions before creating a worktree. Honor existing branch and destination conventions.

## Create a worktree

Choose an absent destination outside existing worktrees. Specify one branch mode: create a new branch, attach an existing branch, or detach.

```bash
python3 /path/to/create-cow-worktree/scripts/cow_worktree.py create \
  --repo /path/to/repo \
  --path /path/to/repo-task \
  --new-branch agent/task-name \
  --start-point HEAD \
  --cow auto \
  --json
```

For an existing local branch, use `--branch branch-name` and omit `--start-point`. For a detached worktree, use `--detach` and supply the desired `--start-point` if it is not `HEAD`.

For a new branch that should track an upstream, supply both the intended starting point and `--track`, for example `--start-point origin/main --track origin/main`. `--track` applies only to `--new-branch`.

| Option | Meaning |
| --- | --- |
| `--cow auto` | Attempt CoW when eligible; otherwise use ordinary Git checkout. |
| `--cow required` | Require successful cloning of the files selected for CoW; fail when the optimization cannot be completed. |
| `--cow off` | Create the worktree through ordinary Git. |
| `--dry-run` | Inspect the request without creating a worktree or probing by writing files. |
| `--json` | Emit a structured result for the calling agent. |

Treat a dry-run CoW candidate as eligibility information. Filesystem capability remains unknown until a real clone succeeds. A backend name or filesystem label alone does not prove CoW support.

## Follow the helper's workflow

1. Resolve the requested starting point to a pinned commit. Keep branch and upstream intent explicit.
2. Find an existing worktree at that commit with clean tracked state and compatible checkout settings.
3. Build the file manifest from the pinned Git tree. Exclude all untracked and ignored source files.
4. Create a private sibling staging worktree through Git with checkout deferred.
5. Force-clone regular files; recreate tracked symbolic links without dereferencing them; preserve Git's executable mode.
6. Build a fresh destination index from the pinned tree and verify each cloned file's content, type, and mode independently of the source index.
7. Attach the requested branch and move the verified worktree to its destination using Git's worktree operations.
8. Report the actual creation mode and verification result before starting the assigned work there.

Let the helper manage this sequence. Copying a directory and editing its `.git` file by hand does not create a valid independent worktree.

## Apply compatibility fallback

Use ordinary Git when no eligible source exists or the host/filesystem cannot clone the selected files. Ordinary checkout happens at the requested final path so checkout hooks see the intended location.

In this version, also use ordinary Git for checkout hooks, sparse or split indexes, filesystem monitors, per-worktree or conditional configuration, submodules, external filters such as LFS, and content transformations whose equivalence is not established. Read [Git semantics](references/git-semantics.md) when investigating an eligibility decision.

Use [platform behavior](references/platforms.md) to interpret backend selection and clone failures. Treat permission, disk-space, and I/O failures as errors; do not relabel them as unsupported CoW or suppress them with another copying tool.

## Preserve correctness

- Keep Git-created administrative metadata. Share repository objects through Git; keep each worktree's index and `HEAD` independent.
- Clone only the pinned commit's paths. Do not carry untracked edits, ignored `.env` files, dependency directories, build output, or caches into the new worktree.
- Use independent cloned regular files. Do not substitute hardlinks or symlinks to source files for CoW.
- Leave the source files, index, and branch alone. Do not stash, reset, or clean the source to make it eligible.
- Verify destination bytes against the pinned commit. Source cleanliness checks and stat caches are insufficient when another agent may edit the source concurrently.
- Respect existing destinations, branch occupancy, and locks. Do not add force flags to defeat a collision.
- Keep cleanup limited to objects owned by the current attempt. Preserve state and report interference if another actor changes it. Do not run broad worktree pruning as rollback.
- Do not install filesystem tools, change mounts, or request administrator privileges merely to enable the optimization.

## Interpret and report results

On a successful creation, use the reported final `path`; do not start work in a staging directory. Check `status`, `mode`, the pinned `commit`, branch identity, and verification information.

The helper distinguishes `created`, `planned`, and `error` statuses. Creation modes include `cow` and `git-checkout`; a dry run may report `cow-candidate`. Read `fallback_reason` when ordinary Git was selected.

Report successful clone count and logical bytes cloned when useful. Do not claim exact physical disk savings, a speedup, or native-platform validation from those counters. A normal copy also isolates edits; edit isolation alone is not evidence of CoW.

If creation fails, explain the actual error and any `recovery_path`. Preserve that path: it can contain another actor's working files, staged changes, or detached commit. Address the cause before retrying. Do not report a usable worktree until verification or ordinary Git creation has completed successfully.

## Scope

Optimize tracked files from an existing clean worktree at the same commit. Let Git handle other starting points in this version. Keep cross-commit file reuse and copying dependency directories as separate future features.
