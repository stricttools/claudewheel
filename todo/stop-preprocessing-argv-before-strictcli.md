# Stop preprocessing argv before strictcli sees it

## Context

strictcli is removing passthrough commands, the construct that let a command
receive raw argv. claudewheel never used that construct, but it does something
of the same class one level up: `main()` in `claudewheel/cli.py` edits
`sys.argv` before handing it to strictcli.

Two edits happen there:

- Everything after a bare `--` is cut out of `sys.argv` and stashed in the
  module-level `_passthrough` list. The `launch` flow later appends that list
  to the extra flags it forwards to the launched Claude Code process, and
  passes it into the launch functions as `passthrough=`.
- `_inject_launch` inserts the `launch` subcommand when the first
  non-reserved token is neither a known subcommand nor an app-level flag, so a
  bare `claudewheel` starts the TUI.

## Problem

Both edits are argument parsing done outside the parser. The `--` handling
duplicates a rule strictcli already has: every token after a bare `--` is
positional data, delivered to a declared variadic positional arg. Because the
tokens are removed before strictcli runs, `launch`'s help does not mention
them, `--dump-schema` does not export them, and the MCP tool export cannot
carry them. The `_inject_launch` edit keeps a hand-maintained list of
subcommands and app-level flags (`_SUBCOMMANDS`, `_APP_LEVEL_FLAGS`,
`_RESERVED_QUARTET`) that must track strictcli's routing rules and
claudewheel's own registrations by hand, and it re-implements the framework's
"which token is the command" decision, which strictcli is also reworking as
part of the same release.

## Solutions

### Option A: declare the forwarded args on `launch` (recommended)

Declare an optional variadic positional arg on `launch`, with help stating
that the tokens are forwarded verbatim to the launched process, and read it
from kwargs. Delete the `--` cut in `main()`, the `_passthrough` global, and
the `passthrough=` parameter threading. Everything after `--` arrives in the
arg with no preprocessing.

Pros: one rule (strictcli's) instead of two; the forwarded args appear in
help, schema and MCP; a module-level mutable global is deleted.

Cons: the arg exists only on `launch`, so `claudewheel <other-command> -- x`
becomes a refusal (unexpected argument), which is correct: no other command
forwards anything.

### Option B: also delete `_inject_launch`

Default-command injection is a routing decision. strictcli has no
declared default command today, so this option depends on either strictcli
adding one (a declared default command when no command token is present) or
claudewheel accepting that a bare `claudewheel` prints app help and the TUI
is started with `claudewheel launch`.

Pros: the hand-maintained token lists and the pre-strictcli argv rewrite
disappear entirely; routing has one authority.

Cons: without a framework default-command declaration, the bare invocation
changes meaning. If strictcli ships such a declaration, this option is the
one to take.

### Option C: keep the preprocessing

Pros: nothing changes.

Cons: the duplicate `--` rule stays; the forwarded args stay invisible to
help, schema and MCP; the token lists keep tracking two systems by hand; and
strictcli's reworked routing may change what the rewrite has to predict.

## Affected files

- `claudewheel/cli.py` (`main`, `_inject_launch`, `_passthrough`, the
  `launch` registration and the launch call sites that pass `passthrough=`)
- the launch functions that accept `passthrough=`
- tests covering `--` forwarding and the bare invocation

## Effort

Small for Option A. Option B is small in claudewheel but depends on a
strictcli decision.
