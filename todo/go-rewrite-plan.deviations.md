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
