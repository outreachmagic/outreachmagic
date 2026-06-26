# Git hygiene — prevent accidental deletions

Run these checks at the **start** of every session before making any edits.

## 1. Check for dirty working tree

```bash
git status --short
```

If output is non-empty, the working tree has pre-existing uncommitted changes. **Flag this to the user immediately.** Do not conflate these changes with your own modifications.

## 2. Warn about deleted files

Scan for deletions (`D` in the first column of status). Files that exist in HEAD but are deleted locally are particularly dangerous to commit — they could be accidental.

If you see deletions you did not create yourself, **pause and ask the user** before doing anything that could include them in a commit. Recommend `git restore <path>` for any files whose deletion looks suspicious.

## 3. Stage surgically, never broadly

Never run `git add .` or `git add -A` or `git commit -a`. Always add files by explicit path:

```bash
git add path/to/changed-file.py path/to/other-file.py
```

This prevents pre-existing changes in unrelated files from leaking into your commit.

## 4. Verify the diff before committing

```bash
git diff --cached --stat     # quick file count and insertions/deletions
git diff --cached | head -50  # spot-check the actual changes
```

If the stat looks wrong (e.g. deletions you didn't intend), reset and re-stage.

## 5. Pre-existing changes should not block the session

If the user's working tree is dirty and they don't want to commit those changes yet, suggest stashing or working on a fresh branch:

```bash
git stash -u   # stash all tracked + untracked changes
# or
git switch -c <feature-name>   # fresh branch from HEAD (stale tree stays on original branch)
```
