# Git semantics and compatibility

## Worktree identity

Git linked worktrees share repository objects and most refs while retaining their own administrative directory, `HEAD`, and index. Use `git worktree` to register and move them. Keep the generated `.git` link; never copy the source's `.git` directory or index.

Resolve the requested start point once to a full commit ID. Use that ID as the content authority throughout setup. Preserve requested branch and upstream intent separately from commit resolution. A branch name can move while setup is running.

Reject existing destinations and occupied branches instead of forcing a checkout. An existing local branch uses its own resolved commit; do not combine `--branch` with a second `--start-point`. Apply `--track` only when creating a new branch.

## Source eligibility and content authority

Select a source from the same repository at the pinned commit. Require clean tracked state, but do not copy untracked or ignored files even when they are present.

Build the manifest from the commit tree with NUL-delimited output. Do not recursively copy everything in the source and attempt to exclude unwanted names afterward.

Keep source operations read-only, including avoiding optional index refresh writes. Do not copy source index flags or trust `assume-unchanged`, `skip-worktree`, fsmonitor state, or cached stat information as proof of source bytes.

Another actor can edit the source after a cleanliness check. Verify every cloned destination file against the pinned tree, including type and executable mode. For symbolic links, verify the link-target bytes. Raw blob hashing is valid only when checkout content transformations have been excluded.

## Staging and index initialization

Create the CoW candidate as a private sibling staging worktree with `git worktree add --no-checkout`. Clone the committed paths there, build a fresh index from the pinned tree, and verify before moving the worktree to its requested final path.

`git read-tree <commit>` initializes the index without rewriting working files when used without a working-tree update. `git update-index --refresh` associates file stat information with existing index entries. Refreshing metadata does not replace independent content verification.

Attach the requested branch only after content verification. Move the registered worktree through Git so its links are updated consistently. Verify the actual registered path after movement: if another actor creates the destination directory during publication, Git can interpret it as a container and move the worktree beneath it. Return an error with the exact preserved `recovery_path` in that case.

## Checkout behavior that requires fallback

### Hooks

`git worktree add --no-checkout` suppresses `post-checkout`. A manual population would therefore omit repository setup performed by that hook.

Use ordinary Git creation when an applicable checkout hook exists, including hooks selected through `core.hooksPath`. Run ordinary creation at the requested final path; a staging path can be significant to a hook. Preserve the normal hook's exit status and resulting working-tree state.

### Attributes and filters

Git may convert line endings, apply `working-tree-encoding`, expand `ident`, or run clean/smudge/process filters. The bytes in a blob and the bytes expected in a checked-out file can differ.

Inspect effective attributes and configuration, including repository, global, and system attributes where relevant. When equivalent checkout behavior is not established, use ordinary Git instead of comparing raw cloned bytes to blobs. Treat LFS through this filter boundary.

### Sparse checkout and submodules

Use ordinary Git for sparse checkout and submodule/gitlink cases in this version. Let Git apply its normal materialization rules; do not clone submodule administrative links or reconstruct sparse index state manually. Ordinary worktree creation does not imply that submodules have been initialized.

Also use ordinary Git for split indexes, filesystem monitors, per-worktree configuration, and conditional configuration. Their additional metadata or location-dependent behavior falls outside the initial fast path. Apply explicit and inferred upstream settings before ordinary checkout so hooks can inspect them. Create new branches and detached checkouts at the pinned commit, even if the source ref advances while a failed CoW attempt is being cleaned up.

## Transaction boundaries

Keep a record of the paths, Git registration, and branch created by the attempt. Limit rollback to that record. Preserve existing refs, configuration, files, and other worktrees.

Before deleting attempt-owned state, check working files and private Git metadata, including the index and resolved HEAD. Validate the expected tree before accepting metadata produced by the helper's own operations. On interference, stop destructive cleanup and report remaining state. Delete an attempt-created branch only at its expected commit and when no other registered worktree uses it. Avoid broad `git worktree prune` or source resets; the helper's targeted forced removal is limited to a verified, owned staging worktree.

`git worktree lock` protects worktrees against certain administrative operations; it does not prevent another process from modifying source files. Helper coordination locks likewise do not replace destination content verification.

If the source changes during cloning, an automatic attempt may discard its owned staging worktree and retry through ordinary Git. I/O and permission failures remain errors rather than optimization misses.

## Primary sources

- [git-worktree](https://git-scm.com/docs/git-worktree)
- [git-read-tree](https://git-scm.com/docs/git-read-tree)
- [git-update-index](https://git-scm.com/docs/git-update-index)
- [Git hooks: post-checkout](https://git-scm.com/docs/githooks#_post_checkout)
- [Git attributes and checkout transformations](https://git-scm.com/docs/gitattributes)
- [git-ls-tree](https://git-scm.com/docs/git-ls-tree)
- [git-hash-object](https://git-scm.com/docs/git-hash-object)
