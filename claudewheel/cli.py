"""main() function with argparse for claudewheel CLI."""

from __future__ import annotations

import argparse
import os
import sys

from .app import App
from .config import ConfigManager
from .constants import LAUNCHER_DIR, OPTIONS_FILE, VERSIONS_DIR, CLAUDE_SYMLINK
from .health import run_health_check, print_health_report
from .hooks import run_hooks
from .launch import resolve_launch_config, do_launch
from .segment import version_sort_key
from .state import save_launch_state


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


def _do_show(cfg: ConfigManager) -> int:
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
    cfg: ConfigManager, selections: dict, extra_flags: list[str] | None = None
) -> None:
    """Run health check, hooks, save state, resolve, and exec. Does not return on success."""
    if cfg.config.get("health_check_on_launch", True):
        results = run_health_check()
        warnings = [r for r in results if not r.ok]
        if warnings:
            print("Health warnings:")
            print_health_report(warnings)
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


def main() -> None:
    # Load config first so we can build dynamic --<segment> CLI args
    cfg = ConfigManager()
    enabled = cfg.config.get("enabled_segments", [])
    segment_keys = [s["key"] for s in cfg.segments_def if s["key"] in enabled]

    parser = argparse.ArgumentParser(prog="c", description="claudewheel - TUI launcher for Claude Code")
    parser.add_argument("--health", action="store_true", help="run health check and exit")
    parser.add_argument("--config", action="store_true", help="open ~/.claudelauncher/ in $EDITOR")
    parser.add_argument("--versions", action="store_true", help="list available versions and exit")
    parser.add_argument("--install", metavar="VERSION", default=None,
                        help="download and install a specific Claude Code version, then exit")
    parser.add_argument("--uninstall", metavar="VERSION", default=None,
                        help="delete an installed Claude Code version, then exit")
    parser.add_argument("--reset-options", action="store_true",
                        help="delete options.json so it regenerates from defaults on next run")
    parser.add_argument("--new-profile", action="store_true",
                        help="run the profile creation wizard")
    parser.add_argument("--delete-profile", metavar="NAME", default=None,
                        help="delete a registered profile and all associated data")
    parser.add_argument("--force", action="store_true",
                        help="force deletion even if sessions appear active (for --delete-profile)")
    parser.add_argument("--show", action="store_true",
                        help="print current selections and exit")
    parser.add_argument("--migrate", nargs="+", metavar="ARG",
                        help="migrate sessions: SRC DST [UUID_SUBSTR]")
    parser.add_argument("--gc", action="store_true",
                        help="garbage-collect stale sentinels, compact origins, report stats")
    parser.add_argument("--redir", nargs=2, metavar=("OLD", "NEW"),
                        help="redirect session data after a project directory rename")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview changes without writing (for --migrate, --gc, --redir)")

    # Mutually exclusive session/print passthrough flags
    _RESUME_NO_ID = object()  # sentinel for "-r with no value"
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("-c", "--continue", dest="cont", action="store_true",
                               help="continue the most recent Claude conversation")
    session_group.add_argument("-r", "--resume", nargs="?", default=None, const=_RESUME_NO_ID,
                               metavar="SESSION_ID",
                               help="resume a Claude session (with optional ID, or open picker)")
    session_group.add_argument("-p", "--print", dest="print_prompt", default=None,
                               metavar="PROMPT",
                               help="run Claude in non-interactive print mode with the given prompt")

    # Dynamic --<segment_key> args, one per enabled segment
    seg_group = parser.add_argument_group("segment values")
    for sdef in cfg.segments_def:
        key = sdef["key"]
        if key in enabled:
            seg_group.add_argument(
                f"--{key}", default=None, metavar="VALUE",
                help=f"preset value for the {sdef.get('label', key)} segment",
            )

    args, _remaining = parser.parse_known_args()

    # --versions: list installed versions and exit
    if args.versions:
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
        return

    # --config: open config dir in editor
    if args.config:
        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
        os.execlp(editor, editor, str(LAUNCHER_DIR))

    # --health: run health checks and exit
    if args.health:
        results = run_health_check()
        print_health_report(results)
        sys.exit(0 if all(r.ok for r in results) else 1)

    # --install <version>: download and install a version, then exit
    if args.install:
        from .install import install_version

        def on_progress(downloaded: int, total: int) -> None:
            if total > 0:
                mb_done = downloaded / (1024 * 1024)
                mb_total = total / (1024 * 1024)
                pct = downloaded * 100 // total
                print(f"\r  {mb_done:.0f}/{mb_total:.0f} MB ({pct}%)", end="", flush=True)

        print(f"Downloading Claude Code {args.install}...")
        try:
            dest = install_version(args.install, progress_callback=on_progress)
            print(f"\nInstalled to {dest}")
        except OSError as e:
            print(f"\nInstallation failed: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # --uninstall <version>: delete an installed version, then exit
    if args.uninstall:
        sys.exit(_do_uninstall(args.uninstall))

    # --reset-options: delete options.json so it regenerates from defaults
    if args.reset_options:
        sys.exit(_do_reset_options())

    # --new-profile: run the profile creation wizard
    if args.new_profile:
        from .wizard import run_profile_wizard, create_profile
        from .health import _discover_profiles
        existing = [name for name, _ in _discover_profiles()]
        result = run_profile_wizard(existing)
        if result.cancelled:
            print("Cancelled.")
            return
        create_profile(result, cfg)
        return

    # --delete-profile <name>: delete a profile and all associated data
    if args.delete_profile:
        from .profile_ops import do_delete_profile
        sys.exit(do_delete_profile(args.delete_profile, force=args.force))

    # --show: print last_config + segment summary, then exit
    if args.show:
        sys.exit(_do_show(cfg))

    # --migrate SRC DST [UUID_SUBSTR] [--dry-run]
    if args.migrate:
        from .migrate import migrate_sessions
        migrate_args = list(args.migrate)
        dry_run = args.dry_run
        if len(migrate_args) < 2:
            print("Usage: c --migrate [--dry-run] SRC_PROFILE DST_PROFILE [UUID_SUBSTR]", file=sys.stderr)
            sys.exit(1)
        src, dst = migrate_args[0], migrate_args[1]
        uuid_filter = migrate_args[2] if len(migrate_args) > 2 else None
        try:
            migrate_sessions(src, dst, uuid_filter=uuid_filter, dry_run=dry_run)
        except (FileNotFoundError, OSError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # --gc: garbage-collect stale data
    if args.gc:
        from .gc import run_gc
        run_gc(dry_run=args.dry_run)
        return

    # --redir OLD NEW: redirect session data after a directory rename
    if args.redir:
        from .redir import run_redir
        old, new = args.redir
        try:
            run_redir(old, new, dry_run=args.dry_run)
        except (FileNotFoundError, FileExistsError, OSError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Collect segment value overrides from CLI args
    segment_overrides: dict[str, str] = {}
    for key in segment_keys:
        val = getattr(args, key, None)
        if val is not None:
            segment_overrides[key] = val

    # Default directory to cwd if not explicitly set
    if "directory" in segment_keys and "directory" not in segment_overrides:
        segment_overrides["directory"] = os.getcwd()

    # Build extra Claude Code flags from passthrough args
    extra_flags: list[str] = []
    if args.cont:
        extra_flags.append("--continue")
    elif args.resume is not None:
        extra_flags.append("--resume")
        if args.resume is not _RESUME_NO_ID:
            extra_flags.append(args.resume)
    elif args.print_prompt is not None:
        extra_flags.extend(["--print", args.print_prompt])

    # Append anything after "--" as raw Claude Code flags
    if "--" in sys.argv:
        passthrough_start = sys.argv.index("--") + 1
        extra_flags.extend(sys.argv[passthrough_start:])

    # Skip TUI when args cover every required segment, or when print mode is active.
    required_keys = {s["key"] for s in cfg.segments_def
                     if s["key"] in enabled and s.get("required", False)}
    skip_tui = args.print_prompt is not None or (
        required_keys and all(k in segment_overrides for k in required_keys)
    )
    if skip_tui:
        merged = dict(cfg.state.get("last_config", {}))
        merged.update(segment_overrides)
        _do_launch_sequence(cfg, merged, extra_flags=extra_flags)
        return

    # Otherwise show the TUI (pre-filled from last_config + arg overrides)
    app = App(cfg=cfg, overrides=segment_overrides)
    selections = app.run_tui()
    if selections is None:
        return

    _do_launch_sequence(app.cfg, selections, extra_flags=extra_flags)
