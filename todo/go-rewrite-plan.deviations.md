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
