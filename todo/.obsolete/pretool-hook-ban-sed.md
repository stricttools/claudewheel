# PreToolUse hook: ban `sed` in Bash commands, point at Read/Write/Edit

## Context

Claude Code sessions launched via claudewheel inherit shared hooks from
`~/.claudewheel/shared-settings.json`. Sessions keep reaching for `sed` inside
Bash commands to read file slices (`sed -n '1,40p' file`) or edit in place
(`sed -i`), even though the harness provides dedicated Read/Write/Edit tools
that are strictly better: they render properly for the user, they track file
state, and they participate in the permission system. The harness's own
guidance already discourages `sed` (alongside `cat`, `head`, `tail`, `awk`,
`echo`), but guidance is routinely ignored — this was observed live in a
session where the assistant used `sed -n` to read a file and had to be called
out by the user. Per the agent-experience philosophy: hard errors, not
warnings.

## Problem

There is no mechanical enforcement. A PreToolUse hook on the Bash tool can
provide it: Claude Code PreToolUse hooks receive the pending tool call as JSON
on stdin (`tool_name`, `tool_input.command`), and an exit code of 2 blocks the
call and feeds stderr back to the model as feedback. A hook that detects `sed`
in the command and exits 2 with the message

    use read/write/edit tools instead

converts the ignored guidance into a blocked call with an actionable message.

## Design

- **Placement**: a hook script shipped/managed by claudewheel, wired into the
  `hooks` section of `~/.claudewheel/shared-settings.json` as a `PreToolUse`
  entry matching the `Bash` tool, so every profile inherits it.
- **Detection**: match `sed` only in command position, not as a substring —
  the word appears innocently in paths and arguments (`parsed`, `used`,
  `*.sed`). Minimum bar: a word-boundary match anchored at command starts
  (beginning of string, after `|`, `;`, `&&`, `||`, `$(`, backtick, newline).
  Also catch the indirect spawners in argument position: `xargs sed`,
  `find ... -exec sed`. Perfect shell parsing is not attainable in a hook;
  prefer false positives over false negatives — a wrongly blocked command
  costs one rephrase, a missed `sed -i` costs a bypassed guardrail.
- **Blocking, not warning**: exit 2 with the message on stderr. No allowlist,
  no override flag, per the no-escape-hatches rule. `sed` running inside a
  script file the session executes is out of scope — hooks only see the
  top-level Bash command string; that is accepted, not worked around.
- **Message**: exactly the actionable instruction, e.g.
  `BLOCKED: sed is banned. Use the Read/Write/Edit tools instead.` Keep it
  one line; the model reads it as tool feedback.

## Options

1. **Ban `sed` only** (the literal request). Smallest surface, no false
   positives from the wider family.
2. **Ban the whole read/inspect family the harness already discourages**
   (`sed`, `awk`, plus `cat`/`head`/`tail` when they target files). Same
   mechanism, one script, covers the sibling pattern — but `head`/`tail` have
   many legitimate pipeline uses (`some-command | head -5` is fine; only
   `head file` is the anti-pattern), so detection must distinguish
   file-operand use from pipeline use, which is harder and more
   false-positive-prone. Could ship as: hard-ban `sed`/`awk`, leave the rest
   to guidance.
3. **Configurable banned-command list** in shared-settings, hook reads the
   list. Most general; slightly against the mandatory-explicitness philosophy
   only if it invites per-profile weakening — if added, make it extend-only
   (profiles can add bans, never remove shared ones).

Recommendation: start with option 1 exactly as requested; structure the
script so option 2/3 is an additive change (a list of patterns, not a
hardcoded regex), and revisit the family question when the sed ban has run
for a while.

## Affected files

- The shared-settings template/generation path in claudewheel that produces
  `~/.claudewheel/shared-settings.json` (hooks section).
- A new hook script (repo-owned, installed into the claudewheel-managed
  location the other shared hooks use).
- Docs describing the shared hook set.
- Tests: command strings that must block (`sed -n '1p' f`, `foo | sed s/a/b/`,
  `xargs sed`, `find . -exec sed ...`) and strings that must pass
  (`echo parsed`, `ls used/`, `cat notes.sed` under option 1).

## Effort

Small: one script, one settings wiring, tests, docs. The only genuinely
fiddly part is the command-position matcher.
