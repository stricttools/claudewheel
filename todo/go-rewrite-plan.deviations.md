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
