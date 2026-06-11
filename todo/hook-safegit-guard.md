# Hook: command guard

## Problem

Claude Code sessions are expected to use `safegit` for git and `saferm` for deletion, but agents fall back to raw commands (`git add`, `git stash`, `rm`) out of habit. A `deny` permission rule blocks the command but gives no context -- the agent doesn't understand why it was denied or what to use instead. It retries, tries variations, or asks the user.

## Solution

A `PreToolUse` hook (same pattern as `hook-block-worktree`) that:

1. Matches `Bash` tool calls
2. Inspects the command string for forbidden patterns:

   **Git commands (use safegit instead):**
   - `git add` (any form: `git add .`, `git add -A`, `git add --all`, `git add -u`, `git add <file>`)
   - `git stash` (any subcommand)
   - `git restore` (any form)
   - `git checkout -- <file>` (destructive checkout, not branch switching)
   - `git reset --hard`

   **Deletion commands (use saferm instead):**
   - `rm` (any form: `rm file`, `rm -rf dir/`, `rm -f`, etc.)

3. Rejects with a message explaining WHY and WHAT TO USE INSTEAD:
   - For `git add`: "Use `safegit commit -m 'message' -- file1 file2` instead. safegit handles both tracked and untracked files."
   - For `git stash`: "Stashing is forbidden in multi-session worktrees. Commit on a temporary branch instead."
   - For `git restore`/`git checkout --`: "Destructive working-tree resets are forbidden. Use Edit to revert specific lines."
   - For `git reset --hard`: "Hard resets are forbidden. Use Edit to revert specific changes."
   - For `rm`: "Use `saferm delete --description 'why' file1 file2` instead. saferm provides an audit trail and undo capability."

## Implementation

- Add as a built-in hook script in `claudewheel/hook_scripts.py` (like `hook-block-worktree`)
- Deploy via `c deploy-hooks --all` or `c deploy-hooks safegit-guard`
- The hook is a bash script that receives the tool input as JSON on stdin, extracts the command, and pattern-matches against the forbidden list
- Uses `jq` to parse input (same as `hook-block-worktree`)

## Patterns to match

These should be matched regardless of quoting, leading whitespace, or chained commands (e.g., `cd foo && git add .`):

**Git:**
- `git add` (all forms)
- `git stash` (all subcommands)
- `git restore`
- `git checkout -- ` (note the `-- ` to distinguish from branch switching)
- `git reset --hard`

**Deletion:**
- `rm ` (with arguments — bare `rm` with no args is harmless)
- `rm -rf`, `rm -f`, `rm -r` (flag variations)

## Effort

Small. The existing `hook-block-worktree` is ~20 lines of bash. This would be similar in structure, just matching different patterns.
