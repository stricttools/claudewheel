+++
title = "CLAUDE.md"
+++
# claudewheel

A TUI Claude Code Launcher that lets you have more than one profile, manage sessions lifecycle, pick the exact CC version, model to use (even older unlisted ones), pick which GitHub account to use, etc.

## Release workflow

This project uses [rlsbl](https://github.com/stricttools/rlsbl) for release orchestration.

- Cover every commit with an entry in `.strictmetadata/changelog/claudewheel/unreleased.jsonl`, added with `rlsbl changelog add`
- `CHANGELOG.md` is generated from those entries and the release archives -- never edit it by hand
- Write `.strictmetadata/releases/claudewheel/unreleased.toml` with `rlsbl release init`, set the bump and description in it, and commit it
- Release with `rlsbl release run --watch --approve-consequential`; CI builds the Linux and macOS archives with GoReleaser, and the Go module is served from its tag
- Build a local binary with `scripts/build <output>`, which stamps the version in `VERSION`

## Layout

- `cmd/claudewheel` is the binary; a bare `claudewheel` runs `launch`. `internal/cli` holds its strictcli commands, one file per command family.
- `internal/tui` holds the terminal screens: `bar` (the launch bar), `sessionsview`, `wizard` (profile creation), `deletion` (the deletion checklist), and `widgets` (themes and shared widgets); `internal/terminal` is the raw terminal I/O under them.
- `internal/launch` starts a session; `internal/discover` finds each segment's options; `internal/appconfig` owns the workspace's own files and `internal/workspace` every path.
- `internal/guardrail` is the canonical guardrail model; `internal/reconcile` makes every managed profile and `shared-settings.json` exactly canonical; `internal/hookscripts` deploys the hook scripts, the `heavy` wrapper, and the probe runner's unit.
- `internal/profiles`, `internal/tokens`, and `internal/auth` are the profile store and its tokens; `internal/sessions`, `internal/sessionmove`, `internal/lifecycle`, `internal/probe`, and `internal/proberunner` cover sessions, their moves, their lifecycle, and OOM kill reports.
- `internal/effects` is where every write, subprocess, and network call happens, so `--dry-run` records them; `internal/schema` holds the strictspec-generated validators.

## Commands

:-: table-commands

### Confirmation and preview

- Ordinary commands need no approval flag. `claudewheel launch`, `claudewheel deploy-hooks --all`, `claudewheel stats` and `claudewheel permission add` are the bare, correct invocations from a script, hook or agent -- including the bare `claudewheel` that starts a session, which prompts for nothing.
- The CLI framework prompts only for commands that declare themselves `consequential`: in claudewheel, `profile delete` and `patch-profiles`. Each refuses with `error: stdin is not interactive; pass --approve-consequential to confirm` when there is no terminal, so a script that means to run one passes `--approve-consequential`.
- `profile delete` is there because the profile stops existing: its directory goes with its `.credentials.json`, `settings.json` and stored OAuth token, every process holding it loses its configuration directory, and it is de-registered. It is recoverable rather than irreversible -- the directory is handed to [saferm](https://github.com/stricttools/saferm), which archives it before removing it, and the deletion prints the `saferm undelete <uuid>` that puts all of it back, token included. saferm is a **precondition**, not an optimisation: with it absent, too old to answer `saferm capabilities`, or missing one of the four features the delegation uses (`machine-payloads`, `on-error-modes`, `git-index-switches`, `uuid-handles`), the deletion is refused. At a terminal claudewheel offers to install it, verifying the download's SHA-256 against the release's published checksum manifest; without one the refusal is a hard error naming the install, because there is deliberately no flag that deletes without the archive. `patch-profiles` is there because the reconciliation is EXACT: a run with `--all-profiles` rewrites every managed profile plus `shared-settings.json`, pruning hand-authored permission rules, hook entries and `disallowedTools` drift, with nothing backed up and nothing that reconstructs a pruned entry. Run it with `--dry-run` first; the per-target diff is the informative preview a blind `Proceed?` is not.
- `--quiet`, `--verbose`, `--dry-run` and `--approve-consequential` are framework-owned: no short forms, recognized anywhere in the command line, and never valid as claudewheel's own flag names.
- `--dry-run` previews instead of writing: every subprocess launch, filesystem mutation and network call is recorded in a would-do log and nothing under `~/.claudewheel/` is touched. It also suppresses the confirmation, so a preview never has to be consented to.

### Selections, and what absence means

- **Two selections are declared, and each elects exactly one member per invocation.** `--profile <name>` / `--all-profiles` chooses what `patch-profiles`, `purge-plugins`, and the three `permission` commands act on, and naming neither is refused with `one of --profile, --all-profiles is required`. `--cont` / `--resume <session>` / `--print-prompt <prompt>` / `--picker` / `--new-session` chooses which session a launch starts in, and naming none of them elects `--new-session`, the plain launch a bare `claudewheel` performs. Naming two is a parse error naming both.
- **The session members keep their short forms.** `-c` elects `--cont`, `-r <session>` elects `--resume`, `-p <prompt>` elects `--print-prompt`, and `-s` (`--set`) is an ordinary flag with its own short. A short takes its value as the next argument, so `-r <id>` is the spelling and `-r=<id>` is not one -- the `=` form belongs to the long flag.
- **`--no-<member>` declines rather than chooses.** `--no-all-profiles` selects nothing and is refused; `--no-cont` leaves the launch on its default member.
- **Every flag and positional argument declares whether it is required, optional, or defaulted.** No mutating command carries a value default -- a value the framework picks is a value the framework writes -- so an opt-in switch (`--all`, `--force-overwrite`, `--reid`, `--post-hoc`) is optional and names in its own help what its absence means.

## Config system

- Config files live in `~/.claudewheel/` (config.json, segments.json, options.json, state.json, themes/); `internal/appconfig` decodes them strictly.
- A launch writes the defaults for a missing file; read-only commands write nothing and refuse a workspace that is not set up.
- `claudewheel upgrade-workspace` removes retired keys and adds the keys the defaults declare that a file lacks, never changing a value already present; every other command refuses a workspace holding a retired key and names it.

## Viewport scrolling

When the segment bar overflows the terminal width, the renderer (`internal/tui/bar/render.go`) activates a scrolling viewport:
- `computeLayout` pre-computes logical column positions for all segments
- `computeViewport` centers the focused segment with `ArrowMargin` (4 columns) reserved on each side
- Segments outside the viewport are skipped; partially visible ones are clipped at the margins
- Edge arrows show off-screen segment counts; minimap shows colored squares in the top-right
- Config key `"minimap"` controls visibility: `"auto"` (only when scrolling) or `"always"`
- Theme section `"overflow"` controls arrow/minimap colors and the minimap character
