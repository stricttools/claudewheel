"""main() function with strictcli for claudewheel CLI."""

from __future__ import annotations

import os
import sys

import strictcli
from strictcli import App, Arg, Flag, MutexGroup, Tag

from . import __version__
from .constants import LAUNCHER_DIR, OPTIONS_FILE, VERSIONS_DIR, CLAUDE_SYMLINK
from .segment import version_sort_key

# Passthrough args after "--" are stashed here by main() before strictcli sees argv.
_passthrough: list[str] = []


def _do_uninstall(version: str) -> int:
    """Delete an installed Claude Code version binary.

    Refuses to delete the version the `claude` symlink currently points to,
    since that would break the default `claude` command. Returns a process
    exit code.
    """
    target = VERSIONS_DIR / version
    if not target.exists():
        print(f"Version {version} is not installed at {target}", file=sys.stderr)
        return 1

    # Refuse to remove the version the symlink currently resolves to.
    try:
        if CLAUDE_SYMLINK.is_symlink() or CLAUDE_SYMLINK.exists():
            current = CLAUDE_SYMLINK.resolve().name
            if current == version:
                print(
                    f"Refusing to uninstall {version}: it is the current "
                    f"`claude` symlink target ({CLAUDE_SYMLINK}). "
                    "Switch to another version first.",
                    file=sys.stderr,
                )
                return 1
    except OSError:
        # If we can't resolve the symlink, fall through and uninstall anyway --
        # a broken symlink isn't a reason to block cleanup.
        pass

    try:
        target.unlink()
    except OSError as e:
        print(f"Failed to delete {target}: {e}", file=sys.stderr)
        return 1
    print(f"Uninstalled {version} ({target})")
    return 0


def _do_reset_options() -> int:
    """Delete OPTIONS_FILE so it regenerates from defaults on next run.

    Does NOT instantiate ConfigManager -- the next normal run will recreate
    options.json via `_ensure_dir`. Idempotent: missing file is not an error.
    """
    if OPTIONS_FILE.exists():
        try:
            OPTIONS_FILE.unlink()
        except OSError as e:
            print(f"Failed to delete {OPTIONS_FILE}: {e}", file=sys.stderr)
            return 1
        print(f"Deleted {OPTIONS_FILE}; defaults will regenerate on next run.")
    else:
        print(f"{OPTIONS_FILE} does not exist; nothing to reset.")
    return 0


def _do_show(cfg: object) -> int:
    """Print a git-status-like summary of last_config, segments, theme, and recent dirs."""
    enabled = cfg.config.get("enabled_segments", [])
    last_config = cfg.state.get("last_config", {})

    print("claudewheel state:")
    # Compute label width for nice alignment across enabled segments
    enabled_segs = [s for s in cfg.segments_def if s["key"] in enabled]
    label_width = max((len(s.get("label", s["key"])) for s in enabled_segs), default=0)
    for sdef in enabled_segs:
        key = sdef["key"]
        label = sdef.get("label", key)
        value = last_config.get(key, "<unset>")
        # +1 for the colon, padded to label_width+1 then a space
        print(f"  {label + ':':<{label_width + 1}} {value}")

    print()
    print(f"Theme: {cfg.config.get('theme', 'dark')}")
    default_flags = cfg.config.get("default_flags", [])
    print(f"Default flags: {' '.join(default_flags) if default_flags else '<none>'}")
    print(f"Health check on launch: {cfg.config.get('health_check_on_launch', True)}")

    recent_dirs = cfg.state.get("recent_dirs", [])
    if recent_dirs:
        shown = recent_dirs[:5]
        print(f"Recent dirs ({len(shown)} of {len(recent_dirs)}):")
        for d in shown:
            print(f"  {d}")
    else:
        print("Recent dirs: <none>")

    print(f"Launch count: {cfg.state.get('launch_count', 0)}")
    return 0


def _do_launch_sequence(
    cfg: object, selections: dict, extra_flags: list[str] | None = None,
    interactive: bool = True,
) -> None:
    """Run health check, hooks, save state, resolve, and exec. Does not return on success."""
    from .health import run_health_check, print_health_report
    from .hooks import run_hooks
    from .launch import resolve_launch_config, do_launch
    from .state import save_launch_state

    if cfg.config.get("health_check_on_launch", True):
        results = run_health_check()
        warnings = [r for r in results if not r.ok]
        if warnings:
            # In non-interactive mode (e.g. print mode), write to stderr and skip input()
            dest = None if interactive else sys.stderr
            print("Health warnings:", file=dest)
            print_health_report(warnings, file=dest)
            if interactive:
                print("Press Enter to continue or Ctrl-C to abort...")
                try:
                    input()
                except KeyboardInterrupt:
                    print()
                    sys.exit(1)
    if not run_hooks("pre-launch", selections):
        print("Pre-launch hook failed. Aborting.")
        sys.exit(1)
    # Save state only after hooks succeed, so launch_count isn't inflated by aborts
    save_launch_state(cfg, selections)
    try:
        cwd, argv, env = resolve_launch_config(
            selections, cfg.options_def, cfg.config.get("default_flags", []),
            extra_flags=extra_flags,
        )
        do_launch(cwd, argv, env)
    except OSError as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------
# Each handler's signature must exactly match the flags/args declared for its
# command.  Handlers that need ConfigManager instantiate it lazily (only the
# ones that actually need it), keeping the one-shot commands fast.

def _handle_health() -> None:
    from .health import run_health_check, print_health_report
    results = run_health_check()
    print_health_report(results)
    if not all(r.ok for r in results):
        sys.exit(1)


def _handle_config() -> None:
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    os.execlp(editor, editor, str(LAUNCHER_DIR))


def _handle_versions() -> None:
    if VERSIONS_DIR.is_dir():
        versions = sorted(
            [e.name for e in VERSIONS_DIR.iterdir() if e.is_file()],
            key=version_sort_key,
            reverse=True,
        )
    else:
        versions = []

    # Determine which version the symlink points to
    current_version = None
    try:
        if CLAUDE_SYMLINK.is_symlink() or CLAUDE_SYMLINK.exists():
            target = CLAUDE_SYMLINK.resolve()
            current_version = target.name
    except OSError:
        pass

    if not versions:
        print("No versions found in", VERSIONS_DIR)
    else:
        for v in versions:
            suffix = " (current)" if v == current_version else ""
            print(f"  {v}{suffix}")


def _handle_install(version: str) -> None:
    from .install import install_version

    def on_progress(downloaded: int, total: int) -> None:
        if total > 0:
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            pct = downloaded * 100 // total
            print(f"\r  {mb_done:.0f}/{mb_total:.0f} MB ({pct}%)", end="", flush=True)

    print(f"Downloading Claude Code {version}...")
    try:
        dest = install_version(version, progress_callback=on_progress)
        print(f"\nInstalled to {dest}")
    except OSError as e:
        print(f"\nInstallation failed: {e}", file=sys.stderr)
        sys.exit(1)


def _handle_uninstall(version: str) -> None:
    rc = _do_uninstall(version)
    if rc != 0:
        sys.exit(rc)


def _handle_reset_options() -> None:
    rc = _do_reset_options()
    if rc != 0:
        sys.exit(rc)


def _handle_new_profile() -> None:
    from .config import ConfigManager
    from .wizard import run_profile_wizard, create_profile
    from .health import _discover_profiles

    cfg = ConfigManager()
    existing = [name for name, _ in _discover_profiles()]
    result = run_profile_wizard(existing)
    if result.cancelled:
        print("Cancelled.")
        return
    create_profile(result, cfg)


@strictcli.flag("force", type=bool, help="force deletion even if sessions appear active")
def _handle_delete_profile(name: str, force: bool) -> None:
    from .profile_ops import do_delete_profile
    rc = do_delete_profile(name, force=force)
    if rc != 0:
        sys.exit(rc)


def _handle_show() -> None:
    from .config import ConfigManager
    cfg = ConfigManager()
    rc = _do_show(cfg)
    if rc != 0:
        sys.exit(rc)


def _handle_migrate(src: str, dst: str, uuid: str) -> None:
    from .migrate import migrate_sessions
    uuid_filter = uuid if uuid else None
    try:
        migrate_sessions(src, dst, uuid_filter=uuid_filter, dry_run=False)
    except (FileNotFoundError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


@strictcli.flag("dry-run", type=bool, help="preview changes without writing")
def _handle_gc(dry_run: bool) -> None:
    from .gc import run_gc
    run_gc(dry_run=dry_run)


@strictcli.flag("dry-run", type=bool, help="preview changes without writing")
def _handle_redir(old: str, new: str, dry_run: bool) -> None:
    from .redir import run_redir
    try:
        run_redir(old, new, dry_run=dry_run)
    except (FileNotFoundError, FileExistsError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# "continue" and "print" are Python keywords, so we use "cont" / "print-prompt"
# as flag names. Short forms -c and -p remain the same for user convenience.
def _handle_launch(
    # Mutex group flags
    cont: bool, resume: str | None, print_prompt: str | None,
    # Segment flags (via tag); empty string means "not provided"
    profile: str, github: str, model: str,
    directory: str, mcp: str, permissions: str,
    # Repeatable set flag (via tag)
    set: list[str],
) -> None:
    from .app import App as TuiApp
    from .config import ConfigManager

    cfg = ConfigManager()
    enabled = cfg.config.get("enabled_segments", [])
    segment_keys = [s["key"] for s in cfg.segments_def if s["key"] in enabled]

    # Collect segment value overrides from individual flags.
    # Empty string means "not provided" (strictcli default for optional str flags).
    segment_overrides: dict[str, str] = {}
    flag_values = {
        "profile": profile, "github": github, "model": model,
        "directory": directory, "mcp": mcp, "permissions": permissions,
    }
    for key in segment_keys:
        val = flag_values.get(key)
        if val:
            segment_overrides[key] = val

    # Merge -s key=value overrides (these take precedence over individual flags)
    for item in set:
        if "=" not in item:
            print(f"Invalid -s format: {item!r} (expected KEY=VALUE)", file=sys.stderr)
            sys.exit(1)
        key, _, value = item.partition("=")
        if key not in segment_keys:
            print(f"Unknown segment: {key!r} (available: {', '.join(segment_keys)})", file=sys.stderr)
            sys.exit(1)
        segment_overrides[key] = value

    # Default directory to cwd if not explicitly set
    if "directory" in segment_keys and "directory" not in segment_overrides:
        segment_overrides["directory"] = os.getcwd()

    # Build extra Claude Code flags from session/print flags
    extra_flags: list[str] = []
    if cont:
        extra_flags.append("--continue")
    elif resume is not None:
        extra_flags.append("--resume")
        if resume:
            extra_flags.append(resume)
    elif print_prompt is not None:
        extra_flags.extend(["--print", print_prompt])

    # Append passthrough args (everything after "--" in original argv)
    extra_flags.extend(_passthrough)

    # Skip TUI when args cover every required segment, or when print mode is active.
    required_keys = {s["key"] for s in cfg.segments_def
                     if s["key"] in enabled and s.get("required", False)}
    skip_tui = print_prompt is not None or (
        required_keys and all(k in segment_overrides for k in required_keys)
    )
    if skip_tui:
        merged = dict(cfg.state.get("last_config", {}))
        merged.update(segment_overrides)
        if print_prompt is not None:
            print_keys = {s["key"] for s in cfg.segments_def
                          if s["key"] in enabled and s.get("print_mode", True)}
            merged = {k: v for k, v in merged.items() if k in print_keys}
            missing = [k for k in required_keys & print_keys if not merged.get(k)]
            if missing:
                print(
                    f"Warning: required segments not set: {', '.join(sorted(missing))}; "
                    "using fallback defaults. Use --<segment> flags or run the TUI first "
                    "to populate last_config.",
                    file=sys.stderr,
                )
        _do_launch_sequence(cfg, merged, extra_flags=extra_flags,
                            interactive=print_prompt is None)
        return

    # Otherwise show the TUI (pre-filled from last_config + arg overrides)
    app = TuiApp(cfg=cfg, overrides=segment_overrides)
    selections = app.run_tui()
    if selections is None:
        return

    _do_launch_sequence(app.cfg, selections, extra_flags=extra_flags)


# ---------------------------------------------------------------------------
# Subcommand names for routing
# ---------------------------------------------------------------------------
_SUBCOMMANDS = frozenset({
    "health", "config", "versions", "install", "uninstall",
    "reset-options", "new-profile", "delete-profile", "show",
    "migrate", "gc", "redir", "launch",
})


def _build_app() -> App:
    """Build the strictcli App with all subcommands registered."""
    app = App(name="c", version=__version__, help="claudewheel - TUI launcher for Claude Code")

    # -- One-shot commands --

    app.command("health", help="run health check and exit")(
        _handle_health
    )

    app.command("config", help="open config dir in editor")(
        _handle_config
    )

    app.command("versions", help="list available versions and exit")(
        _handle_versions
    )

    app.command("install", help="download and install a specific Claude Code version",
                args=[Arg(name="version", help="version to install")])(
        _handle_install
    )

    app.command("uninstall", help="delete an installed Claude Code version",
                args=[Arg(name="version", help="version to uninstall")])(
        _handle_uninstall
    )

    app.command("reset-options", help="delete options.json so it regenerates from defaults")(
        _handle_reset_options
    )

    app.command("new-profile", help="run the profile creation wizard")(
        _handle_new_profile
    )

    app.command("delete-profile", help="delete a registered profile and all associated data",
                args=[Arg(name="name", help="profile name to delete")])(
        _handle_delete_profile
    )

    app.command("show", help="print current selections and exit")(
        _handle_show
    )

    app.command("migrate", help="migrate sessions between profiles",
                args=[
                    Arg(name="src", help="source profile"),
                    Arg(name="dst", help="destination profile"),
                    Arg(name="uuid", help="UUID substring filter", required=False, default=""),
                ])(
        _handle_migrate
    )

    app.command("gc", help="garbage-collect stale sentinels, compact origins, report stats")(
        _handle_gc
    )

    app.command("redir", help="redirect session data after a project directory rename",
                args=[
                    Arg(name="old", help="old directory path"),
                    Arg(name="new", help="new directory path"),
                ])(
        _handle_redir
    )

    # -- Launch command (default when no subcommand given) --
    _session_mutex = MutexGroup(flags=[
        Flag(name="cont", short="c", type=bool,
             help="continue the most recent conversation"),
        Flag(name="resume", short="r", type=str,
             help="resume a session (ID, or empty for picker)"),
        Flag(name="print-prompt", short="p", type=str,
             help="run in non-interactive print mode with the given prompt"),
    ])

    _segment_tag = Tag(name="segments", flags=[
        Flag(name="profile", type=str, default="",
             help="preset value for the Profile segment"),
        Flag(name="github", type=str, default="",
             help="preset value for the GH segment"),
        Flag(name="model", type=str, default="",
             help="preset value for the Model segment"),
        Flag(name="directory", type=str, default="",
             help="preset value for the Dir segment"),
        Flag(name="mcp", type=str, default="",
             help="preset value for the MCP segment"),
        Flag(name="permissions", type=str, default="",
             help="preset value for the Perms segment"),
        Flag(name="set", short="s", type=str, repeatable=True,
             help="set a segment value (e.g. -s version=2.1.119)"),
    ])

    app.command("launch", help="start the interactive TUI launcher",
                mutex=[_session_mutex], tags=[_segment_tag])(
        _handle_launch
    )

    return app


def main() -> None:
    global _passthrough

    # Pre-process sys.argv: extract passthrough args after "--"
    argv = list(sys.argv)
    if "--" in argv:
        idx = argv.index("--")
        _passthrough = argv[idx + 1:]
        sys.argv = argv[:idx]
    else:
        _passthrough = []

    # If no subcommand given, inject "launch" so the TUI starts.
    # Exception: --help/-h/--version/-v should be handled at the app level
    # to show all commands, not the launch command's help.
    rest = sys.argv[1:]
    _APP_LEVEL_FLAGS = {"--help", "-h", "--version", "-v"}
    if not rest or (rest[0] not in _SUBCOMMANDS and rest[0] not in _APP_LEVEL_FLAGS):
        sys.argv = [sys.argv[0], "launch"] + rest

    _build_app().run()
