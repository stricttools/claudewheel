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

## jsonfile: number text, NaN, and equality

- Numbers keep their original text through a read and a rewrite. The Python re-serialized floats through `repr` (`1e5` became `100000.0`, `1.50` became `1.5`); the Go writer leaves them as written. Integers were already exact in both.
- `NaN`, `Infinity`, and `-Infinity`, which Python's `json.loads` accepts, are refused as invalid JSON.
- `Equal` compares numbers by value as Python does (`1 == 1.0`) and ignores object key order (Python dict equality), but keeps booleans and numbers apart (Python has `True == 1`).
- A lone UTF-16 surrogate from a `\uXXXX` escape is held in a Go string as its three-byte generalized UTF-8 form, and every writer escapes it back, so a transcript line round-trips as it did in Python. In strictly decoded struct fields it becomes U+FFFD (encoding/json).
- Go float fields marshal in encoding/json's format, not Python's `repr`; claudewheel's own files hold no floats.

## jsonfile: the writers

The Python writes JSON in these layouts, each now a function: `MarshalIndented` (`json.dumps(x, indent=2) + "\n"`, every owned file), `MarshalCompactASCII` (`separators=(",", ":")`, the probe and lifecycle JSONL lines), `MarshalSortedCompactASCII` (`sort_keys=True` compact, the project hooks fingerprint), `MarshalSpacedASCII` (bare `json.dumps(x)`, health and reconcile messages), and `MarshalTranscriptLine` (`mv._dump_record`: `ensure_ascii=False` compact with lone surrogates escaped). Each takes a tree or any Go value; non-tree values go through encoding/json first, so struct fields keep declaration order.

## jsonfile: strict decoding

`DecodeStrict` refuses duplicate keys (the Python owned-file readers accepted them, last wins), unknown keys, and any document that writing the decoded value back would not reproduce key for key: a missing key, a null in a non-pointer field, or an optional key present with the value its writer omits. An optional key is a pointer field tagged `omitempty`; a required pointer field accepts null.

## workspace: HOME and path encoding

- `Default()` refuses an unset, empty, or relative `HOME`. `FromHome(home)` builds every path from one home directory and replaces `Workspace.open`'s per-directory overrides, which only tests used.
- `EncodePath` and its hash treat each byte that is not valid UTF-8 as U+DC00 plus the byte, as Python's `surrogateescape` decoding of a filesystem path does.

## effects: how an FX is built, and the read-only refusal

`effects.New(ctx)` serves mutating commands (records on `ctx.Effects()` under `--dry-run`, performs otherwise), `effects.ReadOnly(ctx)` serves read-only commands and refuses every mutation with an error wrapping `effects.ErrReadOnly` (the Python performed a mutation from a read-only command silently), and `effects.Standalone(out)` performs everything directly outside a dispatch (tests). Live mode records nothing on the strictcli handle, as in the Python. The Python `issue(dry_run)` predicate is not ported: Go has no unbound dry-run path, so `fx.Previewing()` is the one switch.

## effects: departures in single operations

- `WriteSecretAtomic` under `--dry-run` records one write carrying `mode: 0600` (strictcli's `Mode` option) instead of a write followed by a chmod.
- `Move` falls back to copy-and-delete only when the rename fails with EXDEV (the Python fell back on any rename error, against its own documented intent).
- `CopyTree` stops at the first error instead of collecting every error as `shutil.copytree` does.
- `RunPTY` returns an error when the program cannot be started; the Python's forked child exited 127 instead.
- Install's streaming download is one operation, `fx.Download(req, path, onChunk)`: live, it streams into `path` calling `onChunk` per 1 MiB chunk (hashing and progress); under `--dry-run` it records the `net` request and the write of `path` from its output, which is what the Python's preview branch recorded. Its `Timeout` restarts with every chunk, the closest match to urllib's per-socket-operation timeout.
- Every HTTP request requires a positive `Timeout` (urllib allowed none).

## hookscripts: the constants it fills placeholders from

The templates' placeholders are filled from the packages that own the values, which do not exist yet; they must export these names: `lifecycle.SessionUUIDRE` (a `*regexp.Regexp`), `probe.BindLineRE` (a `*regexp.Regexp`), `probe.OOMKillFix` and `probe.OverlapSentence` (strings), `probe.HookWaitSeconds` (an int), and `probe.ServiceName` (a string). The bash hooks embed the two patterns through `String()`, so their source text must stay the Python pattern text byte for byte. `probe` must never import `hookscripts` (both are in the stores layer).

## hookscripts: registry and deploy

- The registry is functions (`Names`, `IsScript`, `Script`, `PathCommands`, `Exit2Hooks`), built per call; a script's text is generated only when `Script` is called. A declared placeholder missing from its template is an error.
- `DeployScripts` checks every name before writing anything (the Python raised KeyError mid-loop). `MissingScripts` is the shared "registered and not deployed" filter of the reconcile, the vanilla opt-in, and the launch; `CheckDeployed` returns per-script states for the drift health check.
- A stat error other than "does not exist" is an error (pathlib's `exists()` swallowed some).
- `ServiceUnit(executable)` writes `ExecStart=<executable> probe run-service` unquoted, as the plan states, followed by `SuccessExitStatus=143`; it refuses a relative path and one holding whitespace, a control character, or any of `"'\%$;`, which systemd would not take literally.

## hookscripts: the unit's ExecStart path stays quoted (orchestrating session)

The unit writes `ExecStart="<executable>" probe run-service`, double-quoted as the Python wrote its path, so an executable path may hold spaces. A relative path, or one holding a control character, `"`, `\`, `%`, or `$`, is refused. This overrides the unquoted form recorded earlier.

## sessions: shapes and transcript reading

- `DiscoverProfileDirs(profileDirs, sharedDir)` takes the enumerated profile directories from its caller: the profile store lives in `profiles`, a layer above `sessions`.
- `SessionRecord` text fields are empty when absent or not strings (Python's `None`); only `StartedAt` is a pointer. Python's `kind` field is `Category`, with `CategoryInteractive` and `BackgroundCategories()`. `ReadProfileRecords` takes an ordered slice of `ProfileConfigDir` instead of a mapping.
- `Prune(fx, records)` returns an error wrapping `effects.ErrReadOnly` when given a read-only FX, instead of skipping every file silently; other removal errors are still skipped as in Python.
- `PIDExists` sends signal 0 with `syscall.Kill` directly, outside `effects`: it probes and delivers nothing (the Python's exemption).
- Transcripts are split on `\n` only (Python's `splitlines` also split inside lines on U+2028 and other separators, dropping such lines), and each line is decoded with `jsonfile.Decode`, so a line holding invalid UTF-8 is skipped (Python read `recorded_store_cwds` with `surrogateescape` and parsed it; its other readers crashed on it).
- `FindSession` looks up `<store dir>/<id>.jsonl` by exact name in each non-hidden store directory, in name order, instead of a glob that would interpret metacharacters in the id.

## projecthooks: inputs and paths

- `TargetDirectory(directorySelection)` takes the directory selection itself ("" when unset) rather than the whole selections mapping.
- `~` expansion and path printing reproduce `str(Path(d).expanduser())` (repeated and trailing slashes and `.` parts dropped, `..` kept), because `project_hook_approvals` in `state.json` is keyed by that string. An unset `HOME` with a `~` directory is an error (Python fell back to the password database).
- `Fingerprint()` returns an error from the writer instead of being a property; it equals the Python's stored fingerprints unless the hooks hold a float Python re-wrote through `repr` (see the jsonfile number-text entry).

## scratchpad: API

- `SCRATCHPAD_SNOOZE_DAYS` is not ported: the plan replaces the snooze with per-directory dismissal (`scratchpad_dismissed`), which the launch applies to `ScanDirs`' result.
- `Remove(fx, dirs)` attempts every directory and returns the failures joined, each naming its directory; the plan makes a non-nil result abort the launch.
- `ScanTree(root)` is exported so `health`'s `/tmp/claude` size check uses the same walk instead of a second copy (`_real_disk_usage`); its size counts the same files.
- Ages use `time.Time`; a root whose lstat fails has the zero time (Python used epoch 0), so it reads as stale either way.

## auth: validation inputs and the token shape

- `ValidateToken(fx, token)` uses the fixed `ValidateTimeout` (Python's default argument, which no caller changed) and returns `(Status, error)`: an empty token is an error instead of a request (`effects` refuses an empty redaction value). Every failed request is still `Unreachable`, never an error.
- `LooksLikeToken` no longer accepts a trailing newline after the token (Python's `$` matched before one).
- `ExtractToken` returns `(string, bool)` for Python's `str | None`.

## install: binaries, manifest checks, and failure cleanup

- `install` holds binaries.py, the uninstall logic of `cli._do_uninstall` (`Uninstall(fx, locator, version)`, returning errors instead of printing), and `VersionSortKey` plus `CompareVersions` (from `segment.version_sort_key`), which option discovery imports from here.
- `LocatorFor(ws)` derives the home directory as the parent of `ws.Root()`; `workspace` has no home accessor.
- `CheckVersionName` refuses a version that is empty, `.`, `..`, or holds a `/` before install or uninstall touches a path (Python joined it unchecked, so `../x` escaped the versions directory).
- Manifest fields are checked before any download: a non-string checksum or binary, or a size that is not a non-negative integer, is a malformed-manifest error (Python failed later, or formatted the value into the URL).
- The staging file is removed after any failure from the download on (Python kept it after a mid-stream `OSError`). The binary is requested only after `mkdir` of the versions directory, since `fx.Download` opens the request and the file together.
- The platform comes from `runtime.GOARCH`/`GOOS` (the binary's own platform) instead of `platform.machine()` (the kernel's); they differ only for a 32-bit binary on a 64-bit kernel.
- `SymlinkTarget` keeps Python's non-strict resolve: a dangling `claude` link still names its target's version.

## lifecycle: validation and the event model

- The generated `ValidateBytes` runs strictspec's format_version check first and stops there when it fails, so the Python's separate `version_gate` call before validation is not ported; the diagnostics are the same.
- Events are value types (`StartedEvent`, `EndedEvent`, `NamedEvent`, `MarkEvent`, `MovedEvent`) embedding a `Header` (id, at, session, source) behind a sealed `Event` interface. `AppendEvent[E Event]` returns the stamped copy as the same type, as the Python returned the same dataclass.
- `States()` returns the ordered list; the three partitions are `LiveStates()`, `LooseEndStates()`, and `HiddenByDefaultStates()`, each a fresh set (the guardrail's functions-not-variables shape).
- `ParseTimestampMS` accepts RFC 3339 only (Python's `fromisoformat` accepted more ISO 8601 forms); a timestamp without an offset is reported as not RFC 3339. The schema allows only offset datetimes, so no stored line differs.
- Error messages quote values as `'value'`; Python's `repr` escaping of quotes and control characters inside them is not reproduced.
- `AppendLine` (the separator-aware JSONL append) is exported and shared with the probe store, which appended the same way in a second copy.

## probe: store shapes

- A subscription carries `Bound bool` and `Agent *string` (nil is the main conversation) instead of the Python's `"unbound"` sentinel in the agent field.
- `LoadProbes` returns the probes as a slice sorted by id (the Python dict's order, which decides the order of a kill's `probes` list and of report writes); a probe's subscriptions are a slice in log order.
- Kills are a typed `Kill` struct in schema order; `AppendKill` writes nil `reports` and `probes` as empty arrays.
- `ListReports` refuses an unknown state name (the Python iterated any name given), skips a session entry that vanished or is a broken link, and reports an unreadable report file as a `*probe.Error`.
- `NowMS` and the timestamp helpers come from `lifecycle`; the probe package does not repeat them.

## probe: waking a waiter

`WakeWaiter(fx, store, session)` writes its one byte with raw `syscall.Open`/`Write` on the FIFO, outside `effects`: it changes no file, and an `*os.File` would turn a write to a full pipe into a wait (the runtime poller) instead of EAGAIN. Under `--dry-run` it wakes nothing. Departures from the Python, which swallowed every OSError: a path that is not a FIFO is an error, and only ENXIO (no reader) and ENOENT (removed after the check) mean "nothing to wake"; any other open or write error except EAGAIN is returned.

## appconfig: Load, Ensure, and Upgrade

- Load, Ensure, and every single-file reader (`ReadOptions`, `ReadState`, the option and state mutators) refuse a file that Upgrade would change, naming `claudewheel upgrade-workspace` (`ErrUpgradeNeeded`). That covers the retired keys and also a key the defaults declare that the file lacks, since the strict decode would refuse that file anyway and the error should name the fix. A missing file is an error naming `claudewheel launch` (`ErrNotSetUp`); a corrupt file is an error. The Python fell back to the defaults silently in both cases.
- Upgrade: config.json loses `_schema_version` and gains missing top-level keys; segments.json completes each listed default segment; options.json gains `values` and `pinned` on each listed default segment, plus the `model` entry and its `discovery` when absent (the Python added exactly those two); state.json loses `scratchpad_snooze_until` and a non-boolean `vanilla_guardrails_opt_in` (the Python read null and the old per-project object as "not chosen", which absence now means) and gains missing `DEFAULT_STATE` keys; both theme files gain missing keys. Every converted file is checked to decode before any file is written. Leftovers of the deleted migrations (such as `session_memory_max` from a workspace below schema 8) are not removed: Upgrade reports such a file as still unreadable.
- Not ported: appending the default model seed to an existing `model.values` on every start. The seed is the first-run list, and model discovery accumulates new models.
- Ensure also creates `shared-settings.json` when missing, as the Python store's construction did. Under `--dry-run`, the store holds the defaults the missing files would get.
- `LoadTheme` still fills a theme file's missing keys from the default theme for that read only, as the Python `load_theme` did, so a partial custom theme still works. A missing or corrupt theme file, custom names included, is an error, where the Python used the default theme. A theme name must be a plain file name.
- "auto" is resolved by `ResolveThemeName(name, background func() (string, error))`: the caller passes the terminal query, `""` (no answer) resolves to dark, and a query error is returned.

## appconfig: map order, floats, and state keys

- options.json's segments and metadata, `last_config`, `project_hook_approvals`, and the theme segments are Go maps, so the first Go write sorts their keys (the Python kept insertion order); contents do not change. `shared/inodes.json` stays an ordered tree.
- state.json does hold floats (`npm_versions_cache.fetched_at`, `model_list_cache.fetched_at`), unlike the jsonfile note says. They are `float64` and are written in encoding/json's shortest form, the same digits as Python's `repr` except that an integral value loses its `.0`.
- `scratchpad_dismissed` is an optional list of the absolute paths of the `/tmp/claude-<uid>/<project>` directories the user declined to delete, in the order they were declined. Like the other out-of-band keys, `SaveState` lets the copy on disk win.
- `ProjectKey` resolves symbolic links with `filepath.EvalSymlinks`, so a directory that does not exist is an error (Python's `realpath` resolved whatever part existed). `RecordInode` returns errors where the Python returned silently (a failed stat, a corrupt inodes.json).
- `OptionsFile.set_metadata` had no callers and is not ported. The state and option mutators require the file to exist; the Python created it from the defaults.

## tokens: the entry and its store

- `Entry` is a strict struct whose fields are all optional, since an entry written only to declare a plan holds no token. A date that does not parse is an error, where the Python assumed a fresh token. An entry with no dates at all still reports `TokenTTLDays` remaining, as in the Python.
- `BuildEntry` refuses an empty token. The zero `ExpiryDisposition` is invalid, so every writer chooses one. `PlanTier`'s validation when a tier was constructed is not ported, because the plan list is static data.

## proberunner: the entry point and its departures

- `Run(ctx, fx, ws, log)` follows the journal until ctx is cancelled (the CLI cancels it on SIGTERM and SIGINT) and then returns nil; signal handling is the CLI's. Progress lines go to `log` (the CLI passes stderr) with the service-name prefix the Python printed. journalctl stopping on its own returns `ErrJournalEnded` (the Python printed the same line and exited 1). Any store error ends the run with that error, as an exception ended the Python's; systemd's `Restart=on-failure` restarts it.
- journalctl's lines are read by a goroutine from `effects.Follower.ReadLine`; the store is kept moving after every entry and whenever no line arrives within `TickInterval`, as the Python's select loop did. The first pass checks pending reports for expiry at once (the Python's monotonic-clock start made that true on any machine up longer than a minute).
- A journal line that is JSON but not an object is logged as unreadable and skipped (the Python crashed on it). A missing or non-integer `__REALTIME_TIMESTAMP` on an OOM entry is an error, as it was.
- `--until-file` and `/proc/<pid>` are checked with a stat where only a missing path (or a non-directory parent) means absent; any other stat error is returned (pathlib's `exists()` swallowed some).
- `SettleReports` expires only reports in the pending state; the Python treated every non-handed state it was given as pending. Its unused `now` parameter is not ported.
- The duplicate-report check reads each state directory for `<report-id>.*.json` instead of a glob.

## tui/widgets: theme, fuzzy, forms, pages, and the confirm component

- `ParseTheme` takes the strictly decoded `appconfig.Theme`. A color must be `#rrggbb`; anything else is an error naming its key (the Python silently drew no color, and also accepted a missing or doubled `#`). An empty string is the empty sequence, as is a nil `unavailable_fg`. The state colors come from `lifecycle.States()` through `SessionsTheme.StateFg`, so a state the theme struct lacks is an error. `Colors.Segment(key)` reports a segment with no theme entry as `false` instead of an empty dict; `Colors.StateFg` refuses a non-state name.
- Fuzzy matching lowercases rune by rune (`unicode.ToLower`) instead of Python's full case mapping, so match positions are always rune indices into the candidate; ties keep candidate order as Python's stable sort did.
- Widgets require a terminal already in cbreak mode and never enter, leave, or restore it: the Python's owned-raw mode of `screen_session` and its signal swaps are replaced by the caller's `EnterRaw` and deferred `Close`, `KeyResize`, and `WithSignals`. The inline (non-alt-screen) form rendering had no caller and is not ported.
- Mode 2031 color-scheme notifications are ignored by every widget loop; `show_page` returned them as a keypress, which closed the page without the user pressing anything.
- `RunForm` returns whether the form was submitted; answers are read from the fields (`Value` for text, readonly, radio, and select, `Checked` for checkboxes). `Get` panics on an unknown key (Python `KeyError`). A visibility change leaving no focusable field is an error. `RunSelection` with no options is an error (Python returned None); an `initialKey` no option has still focuses the first option, which the wizard's browser picker relies on.
- `ShowPage` requires a hint (`HintAnyKey` is the old default text). `Confirm` takes the meaning of each answer and builds the hint itself (`ConfirmHint`, `y: … n: … esc: …`), so the key letters are spelled in one place; `ConfirmAnswer(key)` is the shared mapping for surfaces with their own key loop (the deletion checklist). A Ctrl-C byte maps to Skip as the todo specifies; under cbreak Ctrl-C arrives as SIGINT instead and every loop returns `terminal.ErrInterrupted`.

## tui/sessionsview: inputs, the prune confirmation, and errors

- The overview takes its inputs as `Sources`: the workspace, the enumerated profiles as `[]sessions.ProfileConfigDir` (the profile store is in `profiles`), a `MemoryReader` (processes.resident_memory, which `profiles` ports), a clock, the identity, and the home directory.
- Prune asks first through `widgets.Confirm`, listing the crashed rows' registry files (as many as fit, then "… and N more"). Accept prunes and reports "Pruned N crashed record(s)"; Decline reports "Pruned nothing"; Escape returns to the table silently. With no crashed row on screen it asks nothing and reports "No crashed records to prune" (the Python pruned an empty list and reported "Pruned 0").
- A resize or a mode 2031 notification redraws without clearing the footer message or leaving mark mode (in the Python any key left mark mode).
- An unknown style, or a state style naming no lifecycle state, is an error (the Python drew an unknown state in no color; the list renderer drew an unknown style in the field color). A negative resident memory and a state line with no style are errors that propagate from `FormatRow` and `BuildFrame`.
- `CurrentIdentity` takes a lookup function (`os.LookupEnv`) and accepts ASCII digits only for the pid (Python's `isdigit` also took other Unicode digits).
- Every width, truncation, and clip counts code points, as Python's `len` and slicing did.
- `ListRow`'s optional tick is a `Selection` (`NoSelector`, `Selected`, `Unselected`) on `SessionBlock`; an expanded row of `None` is `-1`.

## discover: inputs, state, and refresh failures

- Discovery reads an `Env` (FX, home directory, clock, a copy of `appconfig.State`, `profiles.Store`) and never changes the state: a refreshed npm or model cache and the pruned recent directories come back on the `Result`, and `Result.ApplyToState` writes them into the caller's state before it saves (the Python mutated the state dict it was given, and the TUI deep-copied it for the background thread).
- A failed refresh still falls back to the cache however stale, as in the Python, but is no longer swallowed: the failure is `Result.RefreshError` (npm or gh missing, timed out, or failing; every token's models request failing; a profile enumeration or token read failing), for the bar to show. No profile holding a token is not a failure. A gh error other than "not found" or a timeout, an unreadable directory, and a corrupt token file during profile discovery are errors, as Python's uncaught exceptions were.
- An unknown discovery type, and a declaration missing the field its type reads (`path`, `count`, `parents`, `field`), are errors; the Python skipped an unknown type and defaulted `count` to 15 and `parents` to none. `count` must be positive (Python's `[-0:]` returned the whole list). `state_field` and `field` may name only `recent_dirs`, the one list state.json holds.
- A stat error other than a missing path (or a non-directory parent) is an error, where pathlib's `is_dir`/`is_file` read every error as false. A "~name" naming no user is left as written, as `expanduser` did; directory scan entries are joined with `filepath.Join`, which also resolves `..` where pathlib kept it.
- npm and gh run with an empty stdin, so a background child never reads the bar's keys. An npm answer that is not a JSON list of strings is a refresh failure.
- `GitHubToken(fx, account)` (launch's `fetch_gh_token`) lives here with the other gh call and returns an error when gh is missing, times out, exits nonzero, or prints nothing; the Python returned None and the launch went ahead without `GH_TOKEN`.
- `EvaluateRequires` takes the per-segment requirements and the selections and returns the unavailable sets, instead of mutating the bar's segments.

## archiver: detection, the delegation, and the install

- `Detect(fx, root)` returns `(*Tool, *Unavailable, error)`; the reason is `Unavailable.Reason` (`absent`, `no-verb`, `missing-features`). The install offer is not drawn here: `MayOfferInstall(previewing, interactive)`, `Verb`, `Diagnosis`, `Stakes`, `Remedy`, and `RefusalError` are the decision data, and `Install(fx, root, onProgress)` the install. The caller detects again after installing.
- `ArchiveUnreadable` is its own type, `*ArchiveUnreadableError`, beside `*ArchiveError` (Python subclassed it); callers check it first.
- The archive payload is read strictly: a `group_id` or `path` that is not a string, and a `size` that is not a whole number, are unreadable answers (Python stringified any value and truncated floats).
- PATH lookup skips empty and relative PATH entries. The asset name uses `runtime.GOOS` and `runtime.GOARCH` (the binary's own platform; Python used the kernel's machine name and called every non-macOS, non-Windows system linux). The checksum comparison ignores the manifest digest's letter case.
- The asset is fetched with `fx.HTTPRead` into memory, as the Python did, so nothing touches the disk before the SHA-256 matched; only a regular file named exactly `saferm` in the tarball is accepted.

## profiles: store shape and entry points

- `profiles.New(ws)` builds the store; every write goes through `appconfig`'s options and state functions, so the Python's optional write stores and their guards are gone. A missing options.json or state.json is `appconfig`'s error, where the Python fell back to an empty profile segment.
- Every entry point (enumeration, `LaunchEnv`, create, delete, rename, fix-auth, set-plan, the report, the permission and plugin targets) refuses a leftover `.rename_pending` with a `*PendingRenameError` naming `claudewheel profile rename <from> <to>`; `CheckPendingRenames()` is exported for entry points outside this package. `Rename` rerun with the breadcrumb's names finishes the interrupted rename (store updates only when the directory already moved, the whole rename when it did not) and reports `resumed`. A breadcrumb that cannot be read, names no source or target, or sits in a directory named neither is an error describing the manual repair; the recovery that silently finished or dropped breadcrumbs is not ported.
- `Rename` and `Create` also apply the CLI's name policy (`CheckNewName`: the charset and the reserved names); `Rename` also refuses a new name already registered and a profile with a live interactive session, as the Python CLI did, so the TUI and CLI share one check.
- `Create` writes `.claude.json` as a fresh `{"hasCompletedOnboarding": true}`: the directory was just made, so there is nothing to merge (the Python merged, and replaced a corrupt file with `{}`). `CreateOptions` has no defaults; callers state both choices.
- The data-destruction refusal names `--force-delete-data` instead of the Python's `allow_data_destruction=True`.

## profiles: launch environment

`Store.LaunchEnv(name)` returns `LaunchEnv{Set, Unset, Token}`, shared by the launch and `profile exec`; `Apply(environ)` applies it. `Token` is the OAuth token value for redaction (also present in `Set`). For `default` nothing is checked on disk (the launch never resolved `default` through the store): `Set` is empty and `Unset` is every `ProfileEnvKeys()` variable. A named profile keeps the Python's behavior of setting only what it has, so an inherited `CLAUDE_CODE_OAUTH_TOKEN` or plan variable is not removed for a named profile without a token or plan. `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` is set as the Python sets it.

## profiles: reports, permissions, plugins, and processes

- The report's `HasToken` means the token entry holds a token (the Python reported any non-empty entry, so a plan-only entry printed "Token: present"). A corrupt settings.json is an error instead of "no settings.json"; disk usage still skips unreadable entries (a display estimate).
- `permission add` and `remove` write settings.json only when the rule list changed (the Python rewrote it on "already present" too). `AddRule` and `RemoveRule` keep a category argument for the reconcile; the command functions `AddAllowRule` and `RemoveAllowRule` edit allow only. A permissions block or category that is not an object or a list of strings is an error.
- The plugin inventory returns listing and size errors instead of skipping them. `PluginTargets` holds purge-plugins' target policy (default excluded).
- `ResidentMemory` returns an error when ps cannot run (the Python returned no measurements); `Terminate` returns the kill error except ESRCH (the Python returned false); `StopDaemon` returns an error when the command cannot run or times out, and false for a nonzero exit or a preview.

## launch: an unfetchable GitHub token refuses the launch (orchestrating session)

When the selected GitHub account's token cannot be fetched (`gh auth token` fails), the launch is refused with an error naming the account. The Python silently started without `GH_TOKEN`; a configured selection must work or fail, never degrade silently.

## profiles: a named profile's launch environment removes what it does not set (orchestrating session)

`LaunchEnv` for a named profile removes every `ProfileEnvKeys` variable the profile does not set (an OAuth token or plan tier it has none of), so a launch or `profile exec` started inside another profile's session never inherits that profile's token or tier. The Python left inherited values in place.

## sessionmove: inputs and modes

- Every operation takes the profiles from its caller as `[]sessions.ProfileConfigDir` (name and config directory); `Migrate` takes the source and destination profiles already resolved the same way, so the package never imports `profiles`.
- `Migrate` takes a `SessionChoice`: one session by full lowercase uuid, or `All`. Both, neither, or a value that is not a full lowercase uuid is refused, and a chosen session the source holds no artifact of is an error (the Python substring filter reported zero sessions and succeeded).
- The launch's rename prompt counted a move by calling `run_mv(dry_run=True)` inside a live dispatch, which issued nothing. That is now the explicit `MvOptions.CountOnly`. Under `--dry-run` (`fx.Previewing()`), `Mv` records every change it would make, the transcript rewrites and the `.claude.json` writes included; the Python narrated those two but left them out of the would-do log.
- Progress lines go through `fx.Info` (the Python printed them); import's dangling-link warnings go to the required `ImportOptions.Warnings` writer (stderr in the Python). Import collisions without `Reid` come back in `ImportResult.Collisions` with a nil error, as in the Python; the CLI exits 1 on them.

## sessionmove: departures

- Transcripts are read and written as bytes. The Python's `read_text` translated `\r\n` and `\r` to `\n` in every transcript it rewrote or imported; Go keeps line endings.
- A stat error other than "missing", ENOTDIR, or ELOOP is an error (pathlib swallowed some); move-session's inbound-link walk refuses an unreadable directory (os.walk skipped it); migrate's shared-store check returns a failed symlink resolution as an error (the Python read it as "not a link"). `mv`'s directory decoding still skips a directory it cannot list: it is one of three proofs, and a name no proof resolves is refused anyway.
- Import: an unreadable transcript is an error (the Python logged it, skipped the file, and counted the session as imported); the unmapped-cwd error asks for a `--from` and `--to` pair (the Python named a `--map` flag that does not exist); targets are kept per scanned transcript, not per uuid (the Python sent two source store dirs holding one uuid to the later one's target); a mapping whose source path has no component is refused. The path patterns with lookaheads are hand-written matchers (RE2 has none), with `\w` as Unicode letters, numbers, and `_`.
- `ResolveUserPath` (Python's `Path(p).expanduser().resolve()`) refuses an unknown `~name` (Python left it unexpanded) and a `~` with HOME unset or empty.
- The move journal is decoded with `jsonfile.DecodeStrict` after the schema validation; log lines quote values as `'value'` without Python's repr escaping, as lifecycle's messages do.

## tui/deletion: the answer keys, stop errors, and Ctrl-C

- The selecting screen answers through `widgets.ConfirmAnswer`: `y` stops the ticked rows, `n` and Escape both cancel the deletion and stop nothing, Enter does nothing. `Outcome.Answer` keeps the answer so the caller can tell a decline from a skip; `Outcome.Confirmed()` is Accept. The finished screen still closes on any key (it is not a confirmation).
- A daemon-stop or SIGTERM that returns an error (the Python swallowed both as "not stopped") leaves the row running and is reported in `Outcome.Failed` with its error; the run goes on to the next row.
- The exit wait polls `sessions.IsLive` with the record's start token (the Python's identity-aware probe) in the package itself, because `profiles.WaitForExit` checks the pid alone and takes no context; it returns the cancellation cause when ctx is done.
- Ctrl-C (ctx cancelled) returns the cancellation cause; once stopping has begun it also returns the outcome so far, `StillHolding` probed. The Python returned "not confirmed" on Ctrl-C while selecting and ignored it on the finished screen.

## tui/wizard: entry points, the hook merge, and the screens

- Entry points: `RunCreate(ctx, fx, t, colors, ws, merge)` for `profile create` (and the bar's "+"), returning a `Summary` whose `Report()` is what the CLI prints; `RunAuthFlow(ctx, fx, t, colors, ws, profile, skipLabel)` for the bar's unauthenticated-profile intercept; `PickPlan` for the pre-launch plan prompt; `RunProfileForm`, `BuildSettings`, `CreateProfile`, and `DetectBrowsers` underneath.
- `internal/reconcile` did not exist when this was built, so the wizard's hook merge (Python `patch_profiles.merge_hooks`) is a parameter, `wizard.HookMerge func(existing, canonical *jsonfile.Object) error`; the CLI passes the reconcile package's merge, dropping its list of descriptions.
- Both logins (`claude auth login` and `setup-token`) run through `fx.RunPTY` with `ProxyTerminal`, inside `Terminal.Cooked`; the Python ran `auth login` with plain inherited stdio. The child then reaches the user through /dev/tty even with stdout redirected.
- The messages the Python printed during the flow are collected as `AuthResult.Notes` (never holding a token), shown on the final page under the summary, and printed by the CLI afterwards; an error on the way (a malformed or rejected token) is also the error line of the next entry page. Pasted and recovery tokens are entered on a fullscreen masked entry page in the wizard itself (the widgets have no masked field), redrawn on resize; Escape and an empty Enter both abort as the Python's None and "" did.
- Hard errors where the Python fell back silently: a corrupt shared-settings.json, a corrupt settings.json of the profile being cloned, a corrupt `.claude.json` when setting the onboarding flag, a `profileDefaults`, `hooks`, `permissions`, or `claudewheel` value that is not an object, and an unreadable state.json for the remembered browser. A missing shared-settings.json is still the canonical shared settings (what the workspace setup writes there), and a cloned profile without settings.json still clones empty settings.
- The Claude Code binary is the managed `~/.local/bin/claude` link's target when the link exists, and `claude` on PATH only when it does not: a link that leads to no regular file fails the auth attempt with a note instead of falling through to PATH.
- The form's charset and reserved-name messages come from `profiles.CheckNewName`, so they read as the CLI's do. A failed token save is still a failed auth with a note (the token redacted), as in the Python; other workspace errors end the flow with an error.

## tui/wizard: the hook merge comes from reconcile

`internal/reconcile` was committed while the wizard was being built, so `BuildSettings`, `CreateProfile`, and `RunCreate` call `reconcile.MergeHooks` directly and the `HookMerge` parameter is gone: `RunCreate(ctx, fx, t, colors, ws)`. This overrides the parameter recorded in the wizard entry above.

## cli: the command foundation, opening the workspace, and exit statuses

- Commands are registered through `readOnlyCommand` and `mutatingCommand` (internal/cli/call.go), which fix the effect and hand the handler a `*call` (context, FX for that effect, workspace). A handler returns an error: exit 1 with the message, `exitStatus(n)` after the handler reported itself, and an error wrapping `terminal.ErrInterrupted` exits 130 with no message.
- A command acting on the workspace opens the app config first (`Load` read-only, `Ensure` mutating), so an unconverted or missing workspace is refused before anything else. `versions`, `install`, and `uninstall` act only outside the workspace and do not open it; `config` does not either, so a workspace that needs converting can still be edited by hand; `reset-options` does not, because replacing options.json is how a corrupt one is repaired (it refuses when the workspace root does not exist).
- strictcli handles SIGINT and SIGTERM during a handler and replaces the exit status with 128 + the signal's number. `probe run-service` therefore ends with 143 on systemd's SIGTERM (the unit's `SuccessExitStatus=143`) and 130 on SIGINT, although the runner itself returns cleanly; only SIGHUP, which `terminal.WithSignals` adds, ends it with 0.
- Answers go to stdout through `ctx.Out` (kept under `--quiet`); install's "Downloading" line is an info line, and its progress is redrawn in place on stderr and hidden under `--quiet` (the Python wrote both to stdout).
- `migrate` resolves its two profile names against the profile store's names and refuses an unknown one, listing the profiles; the Python joined any name under `profiles/`.
- `probe create` followed by `-- <command>` is refused by strictcli's own "unexpected argument" parse error; the Python's message listing the probe kinds needed the deleted `_passthrough` global.
- `stats` counts each top-level entry of the shared store as the Python did (a directory by its regular files, links not followed; a file or a link to one as one file); an entry that cannot be read is an error.
- `deploy-hooks` takes the unit's executable from `os.Executable()` with symbolic links resolved.

## reconcile: the selection, errors, and malformed permission lists

- `Run(fx, ws, sel)` takes a `Selection` built by `OneProfile(name)` or `AllProfiles()`; the zero value is refused. Only `AllProfiles` adds shared-settings.json. `OneProfile("default")` is refused naming why, and an unknown name is refused listing the managed profiles; both are checked before any hook script is deployed (the Python deployed first, then reported "not found").
- A failed write is an error that stops the run, the report holding what was done before it (the Python recorded a `write-error` skip and went on). A missing, unreadable, or malformed file is still a per-target skip; `Report.Skipped()` lists them so the launch can show them.
- `permissions.deny` or `permissions.ask` holding null, a non-array, or a non-string entry is malformed and the target skipped (the Python crashed on the first two and on an array or object entry, and removed number entries). `permissions.allow` is only read, as before.
- A wrong-type message says "an object" where the Python said "a dict".
- `merge_hooks` is `MergeHooks` here, beside `ReferencedScripts` and `ScriptBasename`, which the default profile's opt-in wiring and health share. The `deploy_hook_scripts` parameter is not ported: every caller passed true.

## health: read-only, failed checks, and the fix commands

- Health writes nothing. The Python pruned stale entries from `shared/inodes.json` during the inode check; that write is dropped, and the entries naming directories that no longer exist are counted in the check's OK detail. Nothing else prunes them.
- A check that cannot be carried out is not OK, detail "check failed: <error>": df failing, exiting nonzero, or printing no percentage (the Python reported OK "check failed" or "unknown"), a stat error other than a missing path, an unreadable options.json in the orphan check, and a failed profile enumeration (such as a leftover rename breadcrumb), which fails every check that needs the profiles. Profiles are enumerated once per run, an unreadable token file read as no token.
- The fix the drift checks name is `claudewheel patch-profiles --all-profiles` in the aggregated details and `claudewheel patch-profiles --profile <name>` in the per-profile ones, since the bare command is now refused.
- `Run(Inputs{FX, Workspace, Executable, Today})`: the probe runner's expected unit is built for `Executable` (the CLI passes `os.Executable()`), and its detail no longer names the workspace root (the unit has no `Environment` line). A token date that does not parse makes the expiry check report that profile's entry as unreadable (see the tokens entry).
- A settings container of the wrong type (`"permissions": []`, `"claudewheel": null`) reads as empty, where the Python crashed. Python `repr` spellings in details (`None`, `'text'`, `[...]`) are reproduced; numbers keep their text.
- The token file mode 0600 is a local constant: `tokens` exports none.

## tui/bar: entry point, terminal, and the flows above it

- `bar.Run(ctx, fx, t, colors, store, bar.Input)` takes a terminal that is open and not yet in cbreak mode: it asks about mode 2031, enters cbreak mode on the alternate screen, and leaves it (`ExitRaw`) when the user launches or quits. On any error, Ctrl-C's `terminal.ErrInterrupted` among them, it returns at once and the opener's deferred `Close` restores the terminal. It returns `bar.Outcome` (launch or quit, the selections with a value, the client, per-segment value metadata for the model id lookup). The session choice and print mode stay the launch command's; the bar never produces either.
- The screens that live above the bar (the create-profile wizard with its authentication, the pre-launch authentication offer, the deletion checklist, and adding or removing the guardrails on ~/.claude) are `bar.Flows` callbacks the launch fills; every field is required. The client registry arrives as `bar.Clients` (each `bar.Client` with its hidden segments and rejected values), so the bar does not import `launch`.
- A rejected value is drawn in the unavailable color and Enter refuses it with "<label>: <value> does not apply to <client>". A command-line value for a segment the chosen client hides, or one it rejects, is an error when the client is chosen.
- The install offer, the delete confirmation, and the saferm install offer use `widgets.Confirm` (y accepts; Enter does nothing). Every install error is shown on the "Install failed" page (the Python showed OSError and crashed on the rest).
- The "finish the deletion" instruction names `claudewheel profile delete <name> --no-force-delete --no-force-delete-data`: both flags are required, so the bare command the Python printed was refused.

## tui/bar: no silent fallbacks

- A command-line value a segment that is not freeform does not offer is an error naming the options (the Python ignored it, so the last launch's value stayed selected).
- config.json's `minimap` must be `auto` or `always` (the Python treated anything else as auto); a segment with no colors in the theme is an error.
- A background discovery error ends the bar with that error (the Python's thread died and the bar went on without its results). A discovery's `RefreshError` is shown as the flash ("Refresh failed: …") on the draw after the results arrive.
- Background results are taken after each key and each resize, as the Python took them after each key; the bar does not redraw on its own when they arrive.

## cli: profile delete at a terminal

- At a terminal (`/dev/tty` opens, as `terminal.HasControllingTerminal` defines it) and outside `--dry-run`, `profile delete` shows the deletion checklist over the profile's holders before anything is removed, then, when saferm is missing, offers its install through `widgets.Confirm` on the same alternate-screen session (the Python asked a typed `[y/N]` line on stdin and stopped nothing from the CLI). Checklist first, offer second, as the bar orders them.
- The live-interactive-session refusal without `--force-delete` applies to what still holds the profile after the checklist, so ticking that session stops it and the deletion proceeds. Without a terminal or under `--dry-run` it applies to every live holder, read before anything else, as in the Python.
- Cancelling the checklist, declining the offer, a failed install, and an installed saferm still lacking a feature each end with exit 1 and nothing deleted; stop failures are printed once the screen closes.
- The offer's text is wrapped by a copy of the bar's `wrapText` (in `internal/cli/profiledelete.go`); both should become one exported widgets helper.

## cli: answers, reports, and the workspace

- `permission list`'s human lines are the command's answer (`ctx.Out`, kept under `--quiet`); the Python wrote them as info lines. The `--json` payload is unchanged.
- `patch-profiles` prints the report of what was done before a failed write, then the error; a selection refused before anything ran prints no report.
- `profile rename` rerun after an interruption reports that it finished the interrupted rename. It is the one profile command that does not refuse a leftover breadcrumb up front.
- `profile exec` opens the app config (`Ensure`) like every mutating command, so an unconverted workspace is refused before the exec.

## launch: the miniclaude adapter always passes model and permission mode (orchestrating session)

The miniclaude client adapter always passes `--model <id>` and `--permission-mode <mode>` to `miniclaude repl`; the Go miniclaude requires both. When the model or permissions segment has no value for a miniclaude launch, the launch is refused with an error naming the segment, instead of omitting the flag.

## launch: the adapter record and the preflight's clients

- `launch.Adapters()` is the client registry: each `Adapter` carries `HiddenSegments` (miniclaude: `version`), `RejectedValues` (miniclaude: `mcp=strict`), its availability check, and its argv builder. The bar's `bar.Clients` (`BarClients`), the command line's check of `-s` values (`CheckExplicit`), and the dropping of remembered values that do not apply (`DropInapplicable`, hidden segments and rejected values alike) all read it; `--client`'s choices are `AdapterNames()`.
- The required segments that decide whether the bar is skipped, and print mode's required segments, leave out the segments the planned client (the `--client` value, else `default_client`) hides, so a miniclaude launch never needs a version it would refuse.
- Preflight steps declare their clients: vanilla-choice, reconcile-guardrails, model-version-guard, release-notes-seen, and plan-declaration apply to claude only; approved-hooks and scratchpad-cleanup to every client.
- The session choice is a typed `launch.Session` (new, continue, resume, picker, print) that each adapter turns into its own flags, instead of the Python's claude-form flag list that the miniclaude adapter parsed back. An empty `--resume` value is the picker, as before.
- The claude adapter refuses an `mcp` value other than default or strict and a `permissions` value other than bypass, default, plan, or auto (the Python passed nothing for them, silently). A missing version's message names `claudewheel install <v>` (the Python's named `python3 -m claudewheel --install`).

## launch: prompts, errors, and the launch sequence

- Every prompt is a `widgets.Confirm` page or a selection list on its own alternate-screen session: the vanilla choice (y enables the guardrails, n stays vanilla and is remembered, Escape stays vanilla for this launch and asks again), the health warnings (y launches anyway; n and Escape abort), the hook approval (y approves; n and Escape abort), the scratchpad cleanup (one page per stale directory: y deletes, n dismisses it for good, Escape keeps it for now), and the session-move offers of `--resume` and `--cont` (the multiple-candidate number entry is a selection list). Without a terminal, `--resume` of a session whose directory was renamed is refused naming `claudewheel mv <old> <new> --post-hoc`; `--cont`'s offer is skipped, as before.
- Hard errors where the Python went on: a pre-launch hook's failure, a reconcile error, a scratchpad deletion error, a `.claude.json` that cannot be read, parsed, or written while marking release notes seen (the Python printed one line and launched), a failing `systemctl` list, show, or stop while sweeping ended sessions (the Python ignored the stop's exit status), and a `~/.claude/settings.json` that is not valid JSON when the guardrails are added or removed (the Python treated it as empty and overwrote it, or skipped it).
- The bar's authentication intercept shows the auth flow's notes on a page before returning to the bar, since nothing prints while the bar is on screen.
- The directory recorded in `shared/inodes.json` is the launch directory with `~` expanded (the Python passed the selection unexpanded, so a `~` directory was never recorded).

## cli: the launch command

- `launch` declares the session choice as the member selector `session` (`--cont`/`-c`, `--resume`/`-r`, `--print-prompt`/`-p`, `--picker`, `--new-session`, the default), the repeatable `-s/--set`, `--client` with its choices and their help from `launch.Adapters()`, and the optional variadic positional `client-args`. It declares the grants `exec-client` and `auth-login`.
- strictcli puts every positional token in `client-args`, before `--` as well as after it, so the handler refuses client arguments that did not come after the `--` in the command line: `claudewheel typo` is an "unexpected argument" error saying the word is not a command, never a word handed to Claude Code. main's launch insertion needed no change: a bare `claudewheel`, `claudewheel -c`, and `claudewheel -- --foo` become `launch`, `launch -c`, and `launch -- --foo`.

## tui/bar: a command-line value a segment does not offer is taken for the launch (orchestrating session)

- A `-s KEY=VALUE` whose value a segment that is not freeform does not offer (a GitHub account the slow background discovery has not produced yet, say) is taken as a launch-only option and selected, as a launch that skips the bar takes it, instead of the hard error recorded under "tui/bar: no silent fallbacks". It is drawn with the ephemeral mark.
- Launching pins a selected launch-only option into options.json only on a freeform segment (a directory typed on the bar, as before); on any other segment it came from the command line and is not remembered as an option. Once discovery offers the value, it is an ordinary option.
- An empty `-s KEY=` value is refused when the bar opens: a launch that skips the bar reads it as no value, but the bar would select the last launch's value again when discovery arrives.

## realpath: one resolver for Python's non-strict resolve

`internal/realpath` (base layer) holds the one port of Python's non-strict `os.path.realpath`, which pathlib's non-strict `Path.resolve` wraps: `Resolve(p)` and `Join` (`posixpath.join`). It replaces sessionmove's private copy and the two lenient resolvers in `profiles` (`ClassifySharedDirs`) and `install` (`Locator.SymlinkTarget`), which could not share code because `profiles` may not import `install`. Behavior those two callers gain: `..` applies to the path resolved so far, as in Python, and a link loop leaves the rest of the path unresolved instead of failing (install's `SymlinkTarget` reported false, profiles returned an error). A link that cannot be read is still an error, which `SymlinkTarget` reports as false.

## pyrepr: one rendering of Python's repr

`internal/pyrepr` (base layer) holds the one rendering of decoded JSON values as Python's `repr` and `str` (`Repr`, `Str`, `StringList`), used by health's details and projecthooks' approval listing; each package had its own copy. The projecthooks listing now escapes control characters in a non-string matcher or command as Python's `repr` does (its copy did not).

## reconcile: MergeHooks refuses malformed canonical hooks

`MergeHooks` checks the canonical hooks before changing anything: an event value or an entry's `hooks` that is not an array, or an entry or hook that is not an object, is a `*MalformedSettingsError` (the wizard passes `shared-settings.json`'s hand-editable hooks as canonical; such a value panicked). The signature is unchanged.

## health: the token file mode comes from tokens

The file-permissions check reads the token file's expected mode from `tokens.TokenFileMode` (which is `effects.SecretFileMode`, the mode `WriteSecretAtomic` leaves), replacing health's local 0600 constant. This overrides the health entry saying `tokens` exports none.

## jsonfile, effects, lifecycle, workspace: single authorities from the read pass

- `effects.SecretFileMode` (0600) is the mode `WriteSecretAtomic` leaves; `tokens.TokenFileMode` is defined from it. This overrides the health entry saying `tokens` exports no token file mode.
- `jsonfile.Describe` names a tree value's JSON type and `jsonfile.StringArray` turns strings into a JSON array; appconfig and guardrail use them instead of their own copies.
- `lifecycle.SplitLines` and `lifecycle.JoinDiagnostics` are shared with the probe store, which split its JSONL files and joined validator diagnostics in second copies.
- `workspace.ProjectsDirName` names the `projects` store directory for the shared store and for `sessions`.
- The jsonfile entry saying claudewheel's own files hold no floats is wrong, as the appconfig entry records: state.json's cache fetch times are floats, written in encoding/json's shortest form and read back by `DecodeStrict` into float64 fields.

## terminal: Cooked keeps the mode 2031 subscription

`Terminal.Cooked` re-subscribes to mode 2031 notifications after re-entering cbreak mode when the terminal was subscribed before (ExitRaw ends the subscription). Callers no longer re-subscribe by hand: the bar did after its install download, but not after the create wizard's logins, which left theme switching dead after a profile was created from the bar.

## tui/widgets: the text helpers are shared

The character-counting helpers the bar, the sessions view, and the wizard each declared (`runeLen`, `runePrefix`, `runeSuffixFrom`, `centeredCol`, the 60-column entry area) and the page prose wrapping the bar and the CLI each declared (`wrapText` at width 56) are `widgets.RuneCount`, `RunePrefix`, `RuneSuffix`, `CenteredColumn`, `FieldAreaWidth`, `WrapText`, and `PageTextWidth`. `sessionsview.CurrentIdentity` checks the pid with `sessions.AllDigits`.

## pathstat: one home for the path-existence helpers that agree on absence

`internal/pathstat` (base layer) holds `Exists` (stat, following links), `Lexists` (lstat), and `IsDir` (stat), each counting only `fs.ErrNotExist` as absent and returning every other stat error. It replaces the identical private copies in `hookscripts` (`exists`), `profiles` (`exists`, `lexists`, `isDir`), and `appconfig` (`fileExists`, an lstat). It is a separate package rather than part of `realpath`, which only resolves links. The helpers that also treat a non-directory parent (ENOTDIR) as absent stay private where they are: `discover` (`absent`, `isDir`, `isFile`), `proberunner` (`exists`), and `health` (`pathState`); so do `sessionmove`'s, which also treat a link loop (ELOOP) as absent. Whether all of these should agree on one definition of absence is a behavior question, not settled here.

## jsonfile, lifecycle, and sessions: shared helpers replace private copies

`health` uses `jsonfile.StringArray`; `reconcile` and `install` use `jsonfile.Describe` (the copies differed from it only in the text for a non-JSON Go value, which a decoded tree never holds); `sessionmove` uses `lifecycle.JoinDiagnostics` and `workspace.ProjectsDirName`; `profiles` uses `sessions.AllDigits`; `proberunner` splits journalctl output with `lifecycle.SplitLines`, which differs from its copy only for empty output (one empty line, which the JSON decode skips). `probe`'s cgroup line splitter stays: it also folds "\r\n". `sessions`' byte splitter stays: it keeps the empty text after a final newline.

## terminal: who is at a terminal (orchestrating session)

`terminal.HasControllingTerminal` now requires both that stdin is a terminal and that `/dev/tty` opens: a process an agent started can keep a controlling terminal while its stdin is a pipe, and a prompt there would wait for keys nobody presses. Every interactive surface asks it before opening the terminal: profile create (before the workspace is opened), profile delete's checklist and saferm offer, the launch bar, and every launch prompt. Nobody at a terminal is an error naming the screen; the launch bar's says it is skipped when -s presets every required segment or --print-prompt runs one prompt. This overrides the "/dev/tty opens" definition in the "cli: profile delete at a terminal" entry.

## archiver: the install commands method is Fix

`Unavailable.Remedy()` is renamed `Unavailable.Fix()` (the old word is banned in this project's files). This overrides the name in the "archiver: detection, the delegation, and the install" entry.

## deletion and appconfig: single authorities for the saferm offer and the segment keys

- The saferm install confirmation and its explanation lines are built once, by `deletion.SafermInstallOffer` and `deletion.SafermExplanation` (internal/tui/deletion/saferm.go); the bar and profile delete both use them.
- The default segments' keys are named once, as `appconfig.SegmentKey*` (internal/appconfig/segment_keys.go). The launch reads them; `profiles.Segment`, `discover.VersionSegmentKey`, the bar's `keyModel`/`keyDirectory`, and the key literals of `appconfig.DefaultSegments` still spell them separately.

## cli: the launch command's grants and its client arguments

- `launch` also declares the grants `download` (NetMutate, the bar's version install) and `archive-delegation` (ProcMutate, the bar's profile delete): under `--dry-run` an undeclared grant is an error.
- strictcli has no declaration for "the tokens after `--` only" (`WithPassthrough` skips parsing altogether), so the handler still refuses client arguments that did not follow `--` by reading `os.Args`. It splits the command line as strictcli's tokenizer does: the framework switches before the first `--` are dropped (`anywhereSwitches`), and a `--` that is the value of a value-taking flag (`-p --`) is that flag's value, the spellings read off the launch command's own declarations. A programmatic door (`App.Test`, the MCP server) carries no command line in `os.Args`, so there the check reads the wrong argv; a strictcli rest-after-separator option would remove it.
- `cli.FrameworkSwitch` is the one list of the framework flags main steps over when it inserts `launch`; main's own copy is gone.
