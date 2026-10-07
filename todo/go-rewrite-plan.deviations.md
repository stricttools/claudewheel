# Deviations from the Go rewrite plan

Implementation decisions taken during the build of `todo/go-rewrite-plan.md`. Append-only: a later entry overrides an earlier one. Each entry names the package or file it concerns.

## Decided by the orchestrating session: `profile exec`

A new command `claudewheel profile exec --name <profile> -- <argv...>`, needed by the Go claudestream and miniclaude, which start Claude Code through it (Claude Code strips `CLAUDE_CODE_OAUTH_TOKEN` from tool subprocesses, so inheriting the environment does not work). Contract:

- It replaces its own process with the argv after `--` (exec, not spawn), so pipes, PID, and signals pass through.
- It applies the same environment the profile store gives a launch: `CLAUDE_CONFIG_DIR`, the OAuth token from the profile's `token.json`, the plan-tier variables, `DISABLE_AUTOUPDATER`, `DISABLE_GROWTHBOOK`, and the marketplace switch. For the `default` profile it removes those profile keys.
- An unknown profile is a hard error listing the profile names.
- It prints nothing on success.
- The token never appears in output, errors, or dry-run records (it is redacted).
- Mutating, with `--dry-run` refused (it execs).
- Built with the profile commands (`internal/profiles` provides the environment; `internal/cli` declares the command).

## guardrail: model shape

- The model is exposed as functions that build fresh values on every call (`Rules`, `AllowConflicts`, `ExpectedHookWirings`, `DisallowedToolEntries`, the canonical trees), not package variables, so a caller mutating a result cannot change the model.
- `HookOptions` marks an unset option with its zero value (`Timeout` 0, empty rewake texts) instead of Python's `None`. The Python construction-time checks (rewake texts need `asyncRewake`, a positive timeout) are not ported: the wirings are static data inside the package.
- `SettingsCoverage` has a zero value `CoverageNotApplicable` for the advise and ask tiers (Python's `None`); its `Name()` is then empty, and the docs table renders it as "n/a" itself.
- `CanonicalHookCommand` uses `filepath.Join`, which also resolves `..` where pathlib would not; scripts directories never contain one.
- The generated blocker and advise scripts keep their header comments naming `claudewheel.guardrail.generate_blocker_script()` and `guardrail.py`, so the Go output is byte-identical to what is deployed. Rewording them is a change to the deployed scripts, for after the switchover.

## terminal: resize, signals, and key decoding

- SIGWINCH is not a handler that redraws mid-read: `Terminal.ReadKey` returns `terminal.KeyResize` (after updating `Rows` and `Cols`) at the next key boundary, so every key loop redraws on it in its own goroutine. Callers that do not redraw ignore it as an unknown key.
- `terminal.WithSignals(ctx)` catches SIGINT, SIGTERM, and SIGHUP and cancels the context with cause `terminal.ErrInterrupted` (SIGINT, exit 130) or `terminal.ErrTerminated` (SIGTERM or SIGHUP, exit 1, as the Python's handlers did); both wrap `context.Canceled`. Readers return `context.Cause(ctx)`. Restoring is `Terminal.Close` (or `ExitRaw`), idempotent and deferred by the caller; there is no atexit.
- `ReadMaskedLine` reports Escape, Ctrl-D, and a Ctrl-C byte as `terminal.ErrEntryCancelled` (the Python raised KeyboardInterrupt); the mask is a required argument.
- Multi-byte UTF-8 keys decode to one character; the Python decoded each byte alone and produced replacement characters.
- No silent fallbacks: a failed size ioctl, a tty read of zero bytes, and I/O errors in the OSC 11 and mode 2031 queries are errors (the Python fell back to `shutil.get_terminal_size`, looped, or returned None). A query the terminal does not answer, or a TERM known not to support it, is `SchemeUnknown` with no error; the theme resolver decides what unknown means. `EnterRaw` while already raw is an error (the Python overwrote the saved attributes). Writes are unbuffered, so there is no Flush.
