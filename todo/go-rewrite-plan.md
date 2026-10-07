# Rewriting claudewheel in Go: the plan

The owner asked for claudewheel to be rewritten entirely in Go, with no questions and no decisions sent to the owner, and with nothing run: no tests, no builds, no compiles, no installs. The design decisions below were made by a read-only investigation of the Python and approved by the orchestrating session. Implementation decisions taken during the build are appended to `todo/go-rewrite-plan.deviations.md` (append-only; a later entry overrides an earlier one).

## Rules for the build

- The live install runs straight from this repository: `~/.local/share/uv/tools/claudewheel` installs it editable from this checkout. Any edit under `claudewheel/`, `pyproject.toml`, `uv.lock`, `package.json`, or `bin/` changes what every live session runs. The build changes none of them, nor `tests/`, `.strictmetadata/`, or `.rlsbl/` (except changelog entries).
- The install is not pinned to the released 0.33.0 for this campaign: the live install runs far past it, and pinning would roll back behavior live sessions use. The build touches no installed file, so leaving it alone is safe.
- Nothing is run: no `go build`, `go vet`, `go install`, `go mod tidy`, `strictspec gen`, tests, or the binary. `~/go/bin` comes before `~/.local/bin` in PATH, so a `go install` would also shadow the live Python claudewheel for every new shell.
- The hooks never call claudewheel: every deployed hook is bash plus jq. The only Python process outside interactive use is `claudewheel-probe-runner.service` (`python3 -m claudewheel.probe_runner`).
- The heavy-command guardrail rule (`heavy-unwrapped`) is temporarily removed from `claudewheel/guardrail.py` by the owner's request. The Go guardrail model ports the committed Python model as it stands; turning the rule back on later means adding it to the Go model as well (its last definition is in this repository's history, in the commit before the one removing it).

## Inventory

### Commands

Every command is a strictcli command with a mandatory effect; only those marked consequential prompt for confirmation.

| Command | Effect | Arguments and flags | What it does |
|---|---|---|---|
| `launch` (injected when no command is given) | mutating, grant `exec-client` | session choice `-c/--cont`, `-r/--resume <id or title>`, `-p/--print-prompt`, `--picker`, `--new-session` (default); per-segment flags `--profile --github --model --directory --mcp --permissions`; `-s/--set K=V` (repeatable); `--client`; anything after `--` is forwarded | Runs the TUI (or skips it when enough is preset), then health check, pre-launch hooks, saving state, preflight steps, building the launch config, and finally `systemd-run --user --scope … -- claude` started with `execvpe` in the session's slice and scope |
| `health` | read-only | | About 18 checks; always exits 0 today |
| `config` | mutating | | Opens `$EDITOR`, falling back to `$VISUAL`, then `vi`, on `~/.claudewheel` |
| `versions` | read-only | | Lists installed Claude Code versions and marks the active symlink |
| `install <version>` | mutating, grant `download` | | Downloads from the GCS manifest, checks sha256, installs to `~/.local/share/claude/versions/<v>` |
| `uninstall <version>` | mutating | | Deletes the binary; refuses the active symlink target |
| `reset-options` | mutating | | Deletes `options.json` |
| `show` | read-only | | Prints selections, theme, and recent directories; opening the app config writes (migrations, rename recovery, `shared-settings.json`) |
| `migrate <src> <dst> [uuid]` | mutating | the uuid argument matches as a substring | Moves session files between profiles |
| `stats` | mutating | | Prints shared-store stats and deletes the legacy `shared/sentinels` |
| `mv <old> <new> [--post-hoc]` | mutating | | Renames a project directory and rewrites session data and `.claude.json` keys |
| `move-session <session> <directory>` | mutating | | Journaled move of one session (journal under `shared/session-moves`) |
| `import <source> [--from…] [--to…] [--reid]` | mutating | `--from`/`--to` all or none | Imports an external `.claude` directory |
| `deploy-hooks [name] [--all] [--force-overwrite]` | mutating | at least one of name or `--all`; the handler enforces only one | Writes the scripts, links `~/.local/bin/heavy`, writes and enables the user service |
| `patch-profiles` | mutating, consequential | none | Exact reconcile of every profile plus `shared-settings.json` |
| `reconcile-permissions [--profile]` | mutating, consequential | `--profile` optional; omitted means all | The same operation as `patch-profiles` |
| `purge-plugins` | mutating | `--profile <n>` or `--all-profiles` | Recursively deletes `plugins/` |
| `profile create` | mutating, grant `auth-login` | | Wizard, then `claude auth login` or `setup-token` under a PTY |
| `profile delete <name>` | mutating, consequential, grant `archive-delegation` | required `--force-delete` and `--force-delete-data` | Archives through saferm (offers to install it), then cleans stores |
| `profile show <name>` | read-only | | Report on one profile |
| `profile rename <old> <new>` | mutating | | Crash-safe through a `.rename_pending` breadcrumb |
| `profile fix-auth <name>` | mutating | | Strips `claudeAiOauth` from `.credentials.json` |
| `profile set-plan <name> <plan>` | mutating | plan has fixed choices | Writes tier fields into `token.json` |
| `profile check-tokens` | read-only | | Validates every token over HTTP |
| `permission add` / `remove <category> <rule>` | mutating | `--profile` or `--all-profiles`; category has no declared choices | Edits `settings.json` |
| `permission list` | read-only | required `--format grouped\|flat`, `--category`, profile choice; a `--json` payload schema | |
| `probe create oom-kill` | mutating | `--session <uuid>` or `--all-sessions`; required `--deadline`; `--count`, `--until-watched-ends`, `--until-file`; refuses anything after `--` | Finds its own session from `/proc/self/cgroup` |
| `probe list` | read-only | | |
| `probe stop` / `subscribe` / `unsubscribe` | mutating | | |
| `new-profile`, `delete-profile`, `show-profile` | deprecated names | | Print a "renamed" message |

### Modules

| Area | Python modules (lines) |
|---|---|
| CLI | cli 3157, `__init__` 46 (reads the version from `package.json`), `__main__` 5 |
| Effects | effects 906 (live mode runs directly, `--dry-run` records on `ctx.effects`), pty_runner 159 |
| Paths and stores | workspace 182, shared_store 140 (Claude Code's store-directory name encoding and base-36 hash), appdata 255, state 166, config 717 (8 versioned migrations, filling missing keys), defaults 555 |
| Guardrails | guardrail 1067 (rules, tiers, deny and ask lists, disallowed tools, hook wiring, blocker and advise bash generation), hook_scripts 1568 (bash templates, deployment, service unit), reconcile 580, patch_profiles 153, permission 147, health 957 |
| Profiles | profile_store 947, profile 67, profile_data 245, tokens 298, profile_ops 96, profile_info 312, plugins 119, processes 178, archiver 641, deletion_checklist 301 |
| Sessions | session 291, session_registry 359, session_stores 53, session_move 637, mv 738, migrate 205, import_ 552, stats 52, lifecycle 741 |
| Probes | probe 977, probe_runner 575, six strictspec-generated validators |
| Launch | launch 458, clients 330, binaries 85, install 209, hooks 61 (user `pre-launch*` scripts), preflight 829 (vanilla-choice, reconcile-guardrails, model-version-guard, release-notes-seen, plan-declaration, approved-hooks, scratchpad-cleanup), project_hooks 165, scratchpad 119, auth 127, discovery 100 (browser detection) |
| TUI | app 1597, segment 1326 (also option discovery: npm, the Anthropic models API, gh accounts, directory scans), renderer 681, terminal 525, ui 491, wizard 814, sessions_overview 631, sessions_table 615, session_rows 191, session_list 234, vertical_viewport 164, theme 140, fuzzy 46, constants 49 |

### On-disk formats and paths

Under `~/.claudewheel`:

- `config.json` (with `_schema_version`), `segments.json`, `options.json` (`values`, `pinned`, `metadata`, and `discovery` per segment), `state.json` (`last_config`, `recent_dirs`, `launch_count`, the npm and model caches, `auth_browser`, `project_hook_approvals`, `scratchpad_snooze_until`, sometimes `vanilla_guardrails_opt_in`), `themes/{dark,light}.json`. All written atomically as JSON with 2-space indent and ASCII escaping.
- `shared-settings.json`; `hooks/pre-launch*` (user scripts, given `CL_<KEY>` variables, 10-second timeout); `scripts/*` (mode 0755); `bin/saferm` (installed by claudewheel); `skills/`.
- `profiles/<n>/`: a Claude Code config directory with `settings.json` (holding a `claudewheel.disallowedTools` key), `.claude.json`, `.credentials.json`, `.claudewheel/token.json` (0600 in a 0700 directory), `.rename_pending`, symlinks to the shared store, `sessions/<pid>.json`, `jobs/`, `plugins/`.
- `shared/`: `projects/<encoded>/<uuid>.jsonl` plus `<uuid>/`, `session-env`, `file-history`, `tasks`, `todos`, `paste-cache`, `inodes.json`, `lifecycle/<uuid>.jsonl`, `session-moves/`.
- `shared/probes/`: `<id>.jsonl`, `kills.jsonl`, `sessions/`, `reports/{pending,handed,delivered,…}/`, `waiters/` (FIFOs), `journal-cursor`.
- The JSONL line shapes are owned by the six `.strictspec/*.schema.toml` files; the bash hooks write and move files in this store too.

Elsewhere: `~/.claude` (`settings.json` written only by the vanilla opt-in; `lastReleaseNotesSeen` set); `~/.local/share/claude/versions`; `~/.local/bin/{claude,heavy}`; `~/.config/systemd/user/claudewheel-probe-runner.service`; `/tmp/claude-<uid>`; `/proc/<pid>/{stat,cgroup}`; `/sys/fs/cgroup/…/cgroup.events`; a target project's `.claude/settings{,.local}.json`.

### External programs, network, and environment

- Programs: `systemd-run`, `systemctl --user` (list-units, show, stop, enable, daemon-reload, restart, is-*), `journalctl --user` (follow and query), `gh auth token|status`, `npm view @anthropic-ai/claude-code versions`, `ps -o pid=,rss=`, `df`, `kill`, `saferm` (capabilities, delete), `claude` (`auth login`, `setup-token` under a PTY, `daemon stop`), `$EDITOR`, the user's pre-launch hooks.
- HTTP: the GCS Claude Code manifest and binaries, GitHub releases for saferm, the Anthropic API (models list, token validation).
- Environment read: `CLAUDEWHEEL_CONFIG_DIR`, `EDITOR`/`VISUAL`, `TERM`, the session identity variables.

### Hook entry points (bash, run by Claude Code)

| Event | Matcher | Scripts |
|---|---|---|
| UserPromptSubmit | any | `hook-timestamp` |
| PreToolUse | Agent | `hook-block-worktree` |
| PreToolUse | Bash | `hook-block-unsafe-commands`, `hook-deliver-probe-reports` |
| PostToolUse | Bash | `hook-advise-commands` |
| PostToolUse | any | `hook-deliver-probe-reports` |
| SessionStart | any | `hook-session-start`, `hook-wait-for-probe-reports` (asyncRewake, 604800 s timeout) |
| Stop | any | `hook-wait-for-probe-reports` (same options) |
| SessionEnd | any | `hook-session-end` |
| PostToolUseFailure, SubagentStop | any | `hook-deliver-probe-reports` |

`claudewheel-tool-scope` is set as `CLAUDE_CODE_SHELL_PREFIX` and runs on every Bash command; `heavy` is a command agents run directly. None of the per-tool-call paths starts claudewheel, so Go startup time affects no hook.

### Callers outside the code

- The systemd unit's `ExecStart`, and `CLAUDEWHEEL_CONFIG_DIR` set inside it.
- `~/Projects/dialog-demo` (README and `scripts/request*.json`) uses `claudewheel patch-profiles`.
- `~/Projects/CONTEXT/afk.md` uses `claudewheel afk status|on|off`, which exists in neither version.
- `~/Projects/CONTEXT/claudewheel-profiles.md` is out of date (describes `~/.claudewheel/tokens.json` and an old discovery rule).
- a private project reads the `~/.claudewheel/profiles` layout directly; the layout does not change.
- The selfdoc directives `.strictmetadata/docs/_directives/{guardrail_table,disallowed_tools_table}.py` import the Python guardrail model.
- `scripts/{patch-profiles,argv-sweep,gates/check-autospec}` and the CI workflow are Python.
- The site pages `stricttools/site/tools/claudewheel/index.html` and `data/tools.json`.
- The bash hooks read `CLAUDEWHEEL_LAUNCH_*`, `CLAUDEWHEEL_LIFECYCLE_DIR`, and `CLAUDEWHEEL_CONFIG_DIR`.

### Dead code, not ported

`app._h_main_search`, `archiver.Unavailable.offer_lines`, `constants.INVERSE`, `guardrail._prefixed_cmd` (once the heavy rule is gone) and `all_settings_rules`, `probe.AGENT_RE` and `read_session_events`, `profile_data.remove_token`, `reconcile.PermissionDiff.is_empty` and `change_count`, `scratchpad.stale_scratchpad_dirs`, `session_registry.live_interactive_records`, `vertical_viewport.RowSlice.skip_bottom`, `cli.one_profile`, `profile.resolve_profile`, `clients.CLAUDE_ONLY_SELECTIONS`, the `segment._Segment_init_wrapper` shim, the deprecated command names, `stats`' sentinel cleanup, the config migrations, the npm shim (`bin/claudewheel.js`, `package.json`), and `scripts/patch-profiles` (imports a function that no longer exists).

## Go design

### Module and toolchain

- Module `github.com/stricttools/claudewheel` at the repository root, beside the Python until the switchover; `go 1.26.3`, `toolchain go1.26.8`.
- Requires `github.com/stricttools/strictcli/go v0.38.0`, `github.com/stricttools/strictspec/go v0.5.0`, and `golang.org/x/sys v0.47.0` (all in the module cache). No other dependencies: the PTY is built by hand on `x/sys/unix`; JSON uses the standard library.
- `go.work` is `use (. ../strictcli/go)`, as orxtra does, because the code needs unreleased strictcli API (`Read()`, `Mode`, `Timeout`, `Stdin`).
- No `go.sum` is written by the build; it is created at the switchover.
- `experiments/go.mod` and a `!go.mod` line in `experiments/.gitignore`, so a later `go build ./...` does not compile the experiments.

### Packages

| Package | Ported from |
|---|---|
| `cmd/claudewheel` | `version` set at link time by a new `scripts/build <out>` that reads `package.json`'s version until the switchover; `launch` injection; `app.Run()` |
| `internal/cli` | one file per command group; handlers read flags, call packages, print |
| `internal/effects` | effects.py's split by mode: live mode runs `os`, `exec`, and `net/http` directly with the full Python behavior (atomic write keeping the file mode, secrets created 0600, append, `SetTimes`, `Symlink`, `CopyTree`, `Kill`, `Exec`, PTY run, follow); `--dry-run` records everything on `ctx.Effects()`; operations without a handle method are recorded as the Python records them (`ln -s`, `kill -N`, `touch -d`, exec as a recorded run); reads are always performed and never recorded. Takes an explicit `*effects.FX` built from the context: no global, no context variable. |
| `internal/jsonfile` | an ordered JSON tree (`UseNumber`, key order kept) for files owned by Claude Code and for `shared-settings.json`; a Python-compatible writer for `indent=2` with ASCII escaping (lowercase `\uXXXX`, surrogate pairs) and for compact `ensure_ascii=False` (transcripts); a strict typed decode refusing unknown keys and requiring every field |
| `internal/workspace` | workspace and shared-store paths, `EncodePath` |
| `internal/appconfig` | config, defaults (except the canonical parts), appdata, state, theme files; `Load` (read-only), `Ensure` (first run, mutating commands only), `Upgrade` |
| `internal/guardrail` | guardrail.py plus the canonical profile settings, the canonical hooks, and the canonical shared settings |
| `internal/hookscripts` | `templates/*.bash` embedded with `go:embed`, the registry, deploy, PATH links, the service unit |
| `internal/tokens` | tokens, profile_data |
| `internal/profiles` | profile_store, profile_ops, profile_info, plugins, permission, processes |
| `internal/archiver` | archiver |
| `internal/reconcile` | reconcile plus hook merging |
| `internal/health` | health |
| `internal/sessions` | session, session_registry, session_stores |
| `internal/sessionmove` | session_move, mv, migrate, import |
| `internal/lifecycle` | lifecycle |
| `internal/probe` | probe |
| `internal/proberunner` | probe_runner |
| `internal/schema/{lifecycleevent,probeevent,probereport,oomkillevent,probesessionevent,sessionmovejournal}` | generated by strictspec, one package per schema (generated constants would collide in one package) |
| `internal/auth`, `internal/install` (with binaries), `internal/projecthooks`, `internal/scratchpad`, `internal/discover` | the matching modules (`discover` holds segment option discovery) |
| `internal/launch` | launch, clients, user pre-launch hooks, preflight |
| `internal/terminal` | terminal, constants |
| `internal/tui/widgets` | theme, fuzzy, ui forms and pages, the confirm component |
| `internal/tui/bar` | segment state, renderer, the app loop |
| `internal/tui/sessionsview` | overview, table, rows, list, viewport |
| `internal/tui/deletion` | deletion checklist |
| `internal/tui/wizard` | wizard plus browser detection |

### Layering (imports point only down)

| Layer | Packages |
|---|---|
| Base | `effects`, `jsonfile`, `workspace`, `guardrail`, `schema/*` |
| Data | `appconfig`, `tokens`, `lifecycle` |
| Stores | `probe`, `sessions`, `hookscripts` |
| Profiles | `archiver`, `profiles` |
| Operations | `reconcile`, `health`, `sessionmove`, `proberunner`, `install`, `auth`, `discover` |
| Interface | `terminal`, then `tui/*`, then `launch`, then `cli` |

Only `effects` (and `terminal`, for `/dev/tty`) may import `os/exec` or `net/http` or call file-changing `os` functions.

### Command surface

Kept unchanged: `health`, `versions`, `install`, `uninstall`, `show`, `mv`, `move-session`, `import`, `deploy-hooks` (its spelling appears inside the deployed bash text), every `profile` command, every `probe` command, the session choice on `launch`, and `-s/--set`.

Changes:

1. Bare `claudewheel` still starts the TUI. strictcli Go has no default-command declaration, so `main` inserts `launch` before calling `app.Run`, deciding from the names in `app.Commands()` and `app.Groups()` plus `help` and `version`, and stepping over the flags strictcli reserves for itself. There is no hand-kept list.
2. `launch`:
   - the per-segment flags are deleted; `-s KEY=VALUE` is the only way to preset a segment (adopts todo/remove-dedicated-segment-launch-flags.md);
   - arguments after `--` arrive in an optional variadic positional `client-args`, and the `_passthrough` global is deleted (adopts todo/stop-preprocessing-argv-before-strictcli.md, its first option);
   - `--client` declares its choices from the adapter registry;
   - print mode with a required segment missing is a hard error (today it warns and falls back);
   - Ctrl-C (SIGINT under cbreak) cancels the handler context; every key loop watches the context, restores the terminal, and the command exits 130.
3. `patch-profiles` absorbs `reconcile-permissions`, which is deleted. It is consequential and takes a required `--profile <n>` or `--all-profiles`; only `--all-profiles` also reconciles `shared-settings.json`. The implicit fan-out when the flag is omitted goes.
4. `permission add|remove <rule>` edit `allow` only, and the category argument is dropped: the launch preflight resets deny and ask to the canonical lists on every launch, so a hand-added deny or ask rule was silently undone. An allow rule listed in the guardrail's allow conflicts is refused when added. `permission list` keeps `--category`, now with declared choices.
5. `migrate <src> <dst>` takes a required `--session <full lowercase uuid>` or `--all-sessions`; substring matching goes.
6. `stats` is read-only; the sentinel cleanup is deleted. `reset-options` writes the default `options.json` instead of deleting the file.
7. `config` uses `$EDITOR` only and refuses when it is unset.
8. `health` exits 1 when any check is not OK.
9. New `probe run-service`: the probe runner the unit starts; mutating, `--dry-run` refused (a long-running service).
10. New `upgrade-workspace`: mutating, previewable. It converts a workspace last written by the Python: removes `_schema_version` and `scratchpad_snooze_until`, and adds missing keys from the defaults. Every other command refuses a workspace still holding a retired key, naming this command.
11. Deleted: the deprecated names, the npm shim, the config migration engine, `CLAUDEWHEEL_CONFIG_DIR` (the root is `$HOME/.claudewheel`; an unset `HOME` is a hard error), and silent first-run writes from read-only commands. Read-only commands use `Load`; a missing file is an error naming `claudewheel launch`. A leftover rename breadcrumb is an error naming `claudewheel profile rename <from> <to>`, which finishes the interrupted rename when rerun.

### Behavior todos adopted

- Confirm keys (todo/confirm-key-unification.md): `y`/`Y` accepts, `n`/`N` declines, Escape skips, Enter does nothing. It replaces raw-key prompts, typed-line prompts (the multiple-candidate number entry becomes a selection list), and the deletion checklist's final key. The sessions-overview prune asks for confirmation. For scratchpad cleanup, `n` permanently dismisses each offered directory (new state key `scratchpad_dismissed`); the snooze is deleted.
- Client adapter record (todo/client-adapter-and-preflight-generalization.md): each adapter declares which segments and which values do not apply to it. For miniclaude, `version` is hidden and `mcp=strict` is rejected in the TUI and on the command line. Client availability moves onto the adapter. Preflight steps declare which clients they apply to; `approved-hooks` applies to every client.
- Preflight error handling: the reconcile no longer swallows exceptions; a reconcile error aborts the launch, and a reconcile that changed anything prints its report. Scratchpad deletion errors abort the launch.

Not adopted, each needing a format change, a guardrail model change, or a design ruling: settings single authority, the guardrail and git-staging overhauls, capability stripping, attachments, the `.claude.json` canonical layer, GitHub credentials, instrumentation, reversibility, `afk`, the lifecycle session segment, the rescue image, and the open residual decisions (atomic writes stay without fsync).

### Formats

- Every path and JSON key stays, apart from the two retired keys and the new scratchpad key.
- Files claudewheel owns are decoded strictly into structs. Claude Code's files and `shared-settings.json` stay ordered trees, keeping key order and unknown keys.
- The JSONL shapes are unchanged.
- The deployed bash scripts are copied byte for byte into `templates/*.bash`: the raw-string bodies verbatim, with the non-raw `hook-timestamp` and `hook-block-worktree` unescaped. The `@…@` placeholders are filled from Go constants, and the blocker and advise scripts are generated by a Go port of the generators.
- The unit becomes `ExecStart=<os.Executable()> probe run-service` with `SuccessExitStatus=143`, and no `Environment` line.

### Hooks and startup

Hooks stay bash in this rewrite: keeping their output identical to what the Python deploys is what lets the switchover check the Go port, and the Go binary sits on no hook path. Building the app does no file or network I/O, and scripts are generated only by the commands that need them; target `claudewheel --version` under 10 ms, measured at the switchover. Moving hooks into a `claudewheel hook <name>` command is separate work after the switchover.

### strictspec

Six `lang="go"` targets are added to `strictspec.toml` (`internal/schema/<pkg>/<name>_gen.go`); they are generated at the switchover, so until then the code imports packages that do not exist yet. The Go code uses only `ValidateBytes(line, syntax)` from them; the API reference is the existing probe output at `experiments/ssgo/gen/lifecycle_event_gen.go`. Typed values come from the code's own `encoding/json` structs.

## Build order

The build is checked by reading, never by compiling. Each slice ends with a read-back of that slice only: every import exists and is used; every identifier used from another package exists (by grep) with the signature it is called with; no import breaks the layering table; every strictcli and strictspec call matches `../strictcli/go/strictcli/*.go` or the strictspec probe output; gofmt-style formatting; no todo markers or plan names in the code. For strictcli usage patterns, follow orxtra's `internal/cli`. Each slice is committed with safegit with a non-user-facing changelog entry (read `~/Projects/CONTEXT/rlsbl-changelog.md` first).

| Slice | Contents |
|---|---|
| 1 | `go.mod`, `go.work`, `scripts/build`, `experiments/go.mod` and its `.gitignore` line, the Go targets in `strictspec.toml`, skeletons of `cmd` and `cli` |
| 2 | `effects` (PTY included), `jsonfile`, `workspace` |
| 3 | `appconfig` (defaults, Load, Ensure, Upgrade, options and state files), `tokens` |
| 4 | `guardrail`, `hookscripts` (copy the templates with read and write, then diff-read them against the Python source) |
| 5 | `lifecycle`, `probe` |
| 6 | `sessions`, `projecthooks`, `scratchpad`, `auth`, `install` |
| 7 | `profiles`, `archiver` |
| 8 | `reconcile`, `health` |
| 9 | `sessionmove` |
| 10 | `proberunner` |
| 11 | `terminal`, `tui/widgets` (confirm component) |
| 12 | `discover`, `tui/bar` |
| 13 | `tui/sessionsview`, `tui/deletion`, `tui/wizard` |
| 14 | `launch` (clients, preflight, pre-launch hooks, exec) |
| 15 | `cli`: every command, `main`'s launch injection, `upgrade-workspace`, `probe run-service` |
| 16 | a whole-module read pass for unresolved identifiers and duplicated constants |

## The switchover (separate work, not part of this build)

During the build the Python stays live and untouched, and the Go binary is never built or installed. Baseline recorded before the build: `patch-profiles --dry-run` showed every profile and `shared-settings.json` canonical; `health` showed every check OK except existing hook drift on `heavy`.

1. `strictspec gen`, then `GOPROXY=off go mod tidy`, build, vet, and fix until it compiles; commit `go.sum` and `go.work.sum`.
2. Port the test suite, red-green.
3. Check against the live workspace without changing it: Go-generated scripts byte-identical to the Python's; Go `health` equal to the baseline; Go `patch-profiles --all-profiles --dry-run` reporting everything canonical before any Go launch; `probe list` and the sessions overview matching; `mv`, `import`, and `move-session` tried on copies of transcripts.
4. `upgrade-workspace --dry-run`, then for real.
5. `uv tool uninstall claudewheel`, install the Go binary, and `deploy-hooks claudewheel-probe-runner.service --force-overwrite`; the service restart replaces the Python runner, so the two never run together.
6. Delete the Python: `claudewheel/`, `tests/`, `pyproject.toml`, `uv.lock`, the npm files, the Python scripts, the Python targets in strictspec and `.gitattributes`; rework the selfdoc directives and `selfdoc.json`, the rlsbl targets and checks, CI, the README and CLAUDE templates, the CLI schema, and the Python paths in todo files; user-facing changelog entries for every behavior change above.
7. Update the outside callers: dialog-demo to `patch-profiles --all-profiles`; `~/Projects/CONTEXT/claudewheel-profiles.md`.

## Risks

- Nothing is compiled or run, so compile errors and wrong API guesses (strictcli's unreleased local API, strictspec's generated names) accumulate until the switchover.
- Bash templates and guardrail regexes are copied by hand: a wrong byte is hook drift, and a difference in canonical hook or deny order means the first Go launch rewrites every profile, so the dry-run check comes before any Go launch.
- JSON fidelity (key order, number text, escaping) when rewriting transcripts.
- An accidental `go install` would shadow the live binary; any edit to the Python touches live sessions.
- TUI fidelity: cbreak mode, OSC 11, mode 2031, SIGWINCH, the changed Ctrl-C exit code, the PTY token capture.
- The service needs `SuccessExitStatus=143`; after `upgrade-workspace`, a rollback to the Python would rerun its migrations.
