"""CLI argument parsing, subcommand routing, and launch orchestration."""

from __future__ import annotations

import os
import sys

import strictcli
from strictcli import App, Arg, CoRequired, Flag, FlagSet, MutexGroup

from . import __version__
from .constants import CONFIG_DIR, OPTIONS_FILE, PROFILES_DIR, SCRIPTS_DIR, SKILLS_DIR, STATE_FILE, VERSIONS_DIR, CLAUDE_SYMLINK, SHARED_DIR, TOKENS_FILE
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


def _write_tier_stub(profile: str | None, config_dir: str | None) -> None:
    """Write a rateLimitTier stub into .credentials.json if tokens.json has tier data.

    This lets downstream tools (e.g. howmuchleft) read the tier from
    .credentials.json even when auth is via CLAUDE_CODE_OAUTH_TOKEN.
    Short-circuits if .credentials.json already has the same tier value.
    A corrupt tokens.json raises TokenStoreError (surfaced cleanly by the
    launch handler); the .credentials.json write remains best-effort.
    """
    import json
    from pathlib import Path
    from .constants import TOKENS_FILE
    from .fsutil import write_json_atomic_secret
    from .tokens import TokenStore

    if not profile or not config_dir:
        return
    tokens = TokenStore(TOKENS_FILE).load()
    entry = tokens.get(profile)
    if not isinstance(entry, dict):
        return
    tier = entry.get("rateLimitTier")
    if not tier:
        return
    subscription = entry.get("subscriptionType")

    creds_path = Path(config_dir) / ".credentials.json"
    # Short-circuit: skip write if existing file already has matching tier
    try:
        existing = json.loads(creds_path.read_text())
        existing_oauth = existing.get("claudeAiOauth")
        if isinstance(existing_oauth, dict):
            if existing_oauth.get("rateLimitTier") == tier:
                return
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = {}

    # Merge tier fields into existing credentials (preserve other keys)
    oauth = existing.get("claudeAiOauth", {})
    if not isinstance(oauth, dict):
        oauth = {}
    oauth["rateLimitTier"] = tier
    if subscription:
        oauth["subscriptionType"] = subscription
    existing["claudeAiOauth"] = oauth
    try:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        write_json_atomic_secret(creds_path, existing)
    except OSError:
        pass


def _shared_store():
    """Build a SharedStore from cli-module path constants (call-time so tests
    patching SHARED_DIR/SKILLS_DIR redirect it at a sandbox)."""
    from .shared_store import SharedStore
    return SharedStore(SHARED_DIR, SKILLS_DIR)


def _profile_store():
    """Build a fully-wired ProfileStore from cli-module path constants.

    Read APIs work without the write stores, but delete/rename need them, so
    every store this helper hands out is wired for writes. Tests patch the cli
    module constants (and Path.home) to redirect it at a sandbox.
    """
    from pathlib import Path
    from .appdata import OptionsFile, StateFile
    from .profile_store import ProfileStore
    from .shared_store import SharedStore
    from .tokens import TokenStore

    return ProfileStore(
        PROFILES_DIR, Path.home() / ".claude", TokenStore(TOKENS_FILE),
        shared=SharedStore(SHARED_DIR, SKILLS_DIR),
        options=OptionsFile(OPTIONS_FILE),
        state=StateFile(STATE_FILE),
    )


def _do_launch_sequence(
    cfg: object, selections: dict, extra_flags: list[str] | None = None,
    interactive: bool = True,
    metadata: dict[str, dict[str, dict]] | None = None,
) -> None:
    """Run health check, hooks, save state, resolve, and exec. Does not return on success."""
    from pathlib import Path
    from .binaries import BinaryLocator
    from .health import run_health_check, print_health_report
    from .hooks import run_hooks
    from .launch import resolve_launch_config, do_launch
    from .profile_store import ProfileStore
    from .state import record_inode, save_launch_state
    from .tokens import TokenStore

    if interactive and cfg.config.get("health_check_on_launch", True):
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
        print("Pre-launch hook failed. Aborting.", file=sys.stderr)
        sys.exit(1)
    # Save state only after hooks succeed, so launch_count isn't inflated by aborts
    if interactive:
        save_launch_state(cfg, selections)
        record_inode(selections.get("directory", os.getcwd()))
    # Read-side ProfileStore is enough: env() supplies both config dir and
    # token. A stale/unknown profile name raises ValueError (the hard-error
    # contract); a corrupt tokens.json raises TokenStoreError. Both are caught
    # here so the user sees a clean message, never a traceback.
    profiles = ProfileStore(PROFILES_DIR, Path.home() / ".claude",
                            TokenStore(TOKENS_FILE))
    try:
        cwd, argv, env = resolve_launch_config(
            selections, cfg.options_def, cfg.config.get("default_flags", []),
            locator=BinaryLocator.default(),
            profiles=profiles,
            extra_flags=extra_flags,
            metadata=metadata,
        )
        _write_tier_stub(selections.get("profile"), env.get("CLAUDE_CONFIG_DIR"))
        do_launch(cwd, argv, env)
    except ValueError as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------
# Each handler's signature must exactly match the flags/args declared for its
# command.  Handlers that need ConfigManager instantiate it lazily (only the
# ones that actually need it), keeping the one-shot commands fast.

def _handle_health() -> int:
    from .health import run_health_check, print_health_report
    results = run_health_check()
    print_health_report(results)
    if not all(r.ok for r in results):
        sys.exit(1)
    return 0


def _handle_config() -> int:
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    os.execlp(editor, editor, str(CONFIG_DIR))
    return 0


def _handle_versions() -> int:
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
    return 0


def _handle_install(version: str) -> int:
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
    return 0


def _handle_uninstall(version: str) -> int:
    rc = _do_uninstall(version)
    if rc != 0:
        sys.exit(rc)
    return 0


def _handle_reset_options() -> int:
    rc = _do_reset_options()
    if rc != 0:
        sys.exit(rc)
    return 0


def _handle_new_profile() -> int:
    """Run the create-profile flow as one continuous alt-screen session.

    Mirrors the TUI path: wizard form, auth forms, and summary page all
    render borrowed in a single alt-screen raw session on a CLI-owned
    terminal. After the session ends, the summary and auth outcome are
    printed to stdout as a persistent record.
    """
    from .config import ConfigManager
    from .terminal import Terminal
    from .theme import parse_theme
    from .ui import show_page
    from .wizard import run_profile_wizard, create_profile, run_auth_flow
    from .discovery import discover_profiles

    cfg = ConfigManager()
    theme = parse_theme(cfg.theme)
    # Requires a real TTY; a headless environment fails here, loudly.
    terminal = Terminal()
    existing = [p.name for p in discover_profiles()]

    cancelled = False
    summary: list[str] = []
    outcome = ""
    try:
        terminal.enter_raw(alt_screen=True)
        try:
            result = run_profile_wizard(existing, theme, terminal)
            if result.cancelled:
                cancelled = True
            else:
                summary = create_profile(result, cfg)
                outcome = run_auth_flow(result.config_dir, result.name,
                                        theme, terminal)
                show_page("Profile created", summary, theme, terminal)
        finally:
            terminal.exit_raw()
    finally:
        terminal.close()

    if cancelled:
        print("Cancelled.")
        return 0
    for line in summary:
        print(line)
    if outcome == "authenticated":
        print("Profile authenticated.")
    elif outcome == "unverified":
        print("Token saved without validation (API unreachable).")
    elif outcome == "cancel":
        print("Auth setup cancelled -- you can authenticate later by launching the profile.")
    elif outcome == "failed":
        print("Auth setup failed -- you can retry by launching the profile.")
    return 0


@strictcli.flag("force-delete", type=bool, help="force deletion even if sessions appear active; skips the safety check")
@strictcli.flag("force-delete-data", type=bool, help="delete even when shared-dir names hold REAL data instead of symlinks; this DESTROYS that data (e.g. conversation history)")
def _handle_delete_profile(name: str, force_delete: bool, force_delete_data: bool) -> int:
    """Delete a profile via ProfileStore. The running check is CLI policy."""
    from .profile_ops import _is_profile_running

    # Running check is CLI policy (ProfileStore.delete does not enforce it).
    if not force_delete and _is_profile_running(name):
        print(
            f"Profile '{name}' appears to have active sessions. "
            "Use --force-delete to delete anyway.",
            file=sys.stderr,
        )
        sys.exit(1)

    store = _profile_store()
    try:
        result = store.delete(name, allow_data_destruction=force_delete_data)
    except ValueError as e:
        # Covers default / not-found / data-destruction refusals.
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(f"Deleting profile '{name}'...")
    print(f"  Removed dir: {result.removed_symlinks} symlinks unlinked, "
          f"{result.removed_real} real entries removed")
    if result.removed_from_options:
        print("  Removed from options.json")
    else:
        print("  Not found in options.json (already clean)")
    if result.removed_from_tokens:
        print("  Removed from tokens.json")
    else:
        print("  Not found in tokens.json (already clean)")
    if result.last_config_purged:
        print("  Cleared last_config profile reference in state.json")
    print(f"Profile '{name}' deleted.")
    return 0


def _handle_show_profile(name: str) -> int:
    from .profile_info import format_report, gather_profile_info

    report = gather_profile_info(name)
    # Unknown = no dir on disk, not registered/pinned, and no token entry.
    # "default" (~/.claude) is inspectable like any other profile.
    if not (report.exists or report.registered or report.pinned
            or report.has_token):
        print(f"Profile '{name}' not found: no profile directory, "
              "no options.json registration, no token.", file=sys.stderr)
        sys.exit(1)
    for line in format_report(report):
        print(line)
    return 0


def _handle_rename_profile(old: str, new: str) -> int:
    """Rename a profile: validate inputs, then delegate to ProfileStore.rename.

    The charset, name-collision (options + tokens), and running checks stay
    here as CLI policy -- they produce clean, targeted messages. The store
    enforces dir-existence and the 'default' reservation as a backstop; its
    ValueErrors are mapped to the same error-print + exit-1 style.
    """
    import re
    from .appdata import OptionsFile
    from .constants import PROFILES_DIR, TOKENS_FILE
    from .profile_ops import _is_profile_running
    from .tokens import TokenStore, TokenStoreError

    # Validate old exists
    old_dir = PROFILES_DIR / old
    options = OptionsFile(OPTIONS_FILE).load({})
    profile_sec = options.get("profile", {})
    registered = old in profile_sec.get("values", []) or old in profile_sec.get("pinned", [])
    if not registered and not old_dir.is_dir():
        print(f"Profile '{old}' not found.", file=sys.stderr)
        sys.exit(1)

    # Validate new name charset
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', new):
        print("Invalid name: use lowercase letters, digits, hyphens only "
              "(must start with letter or digit).", file=sys.stderr)
        sys.exit(1)

    # Validate not reserved
    if new == "default":
        print("Cannot rename to 'default': reserved name.", file=sys.stderr)
        sys.exit(1)

    # Validate not already taken
    new_dir = PROFILES_DIR / new
    if new_dir.exists():
        print(f"Profile '{new}' already exists (directory).", file=sys.stderr)
        sys.exit(1)
    new_in_values = new in profile_sec.get("values", [])
    new_in_pinned = new in profile_sec.get("pinned", [])
    if new_in_values or new_in_pinned:
        print(f"Profile '{new}' already registered in options.", file=sys.stderr)
        sys.exit(1)
    try:
        tokens = TokenStore(TOKENS_FILE).load()
    except TokenStoreError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if new in tokens:
        print(f"Profile '{new}' already has a token entry.", file=sys.stderr)
        sys.exit(1)

    # Check not running
    if _is_profile_running(old):
        print(f"Profile '{old}' has active sessions. "
              "Stop them before renaming.", file=sys.stderr)
        sys.exit(1)

    # Perform rename
    try:
        _profile_store().rename(old, new)
    except (ValueError, OSError) as e:
        print(f"Rename failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Renamed profile '{old}' -> '{new}'.")
    return 0


def _handle_check_tokens() -> int:
    """Validate stored tokens for all discovered profiles against the Anthropic API."""
    from .constants import TOKENS_FILE
    from .discovery import discover_profiles
    from .tokens import TokenStore, TokenStoreError, parse_entry
    from .auth import validate_token, VALID, INVALID, UNREACHABLE, INDETERMINATE

    # Load tokens.json via TokenStore. A corrupt/unreadable file raises
    # TokenStoreError -- catch it narrowly here so the user sees the actionable
    # message and a nonzero exit, never a traceback (mirrors the launch path).
    try:
        tokens = TokenStore(TOKENS_FILE).load()
    except TokenStoreError as e:
        print(str(e), file=sys.stderr)
        return 1

    profiles = discover_profiles()
    if not profiles:
        print("No profiles found.")
        return 0

    # Collect results: (name, status, token_display)
    results: list[tuple[str, str, str]] = []
    for p in profiles:
        entry = tokens.get(p.name)
        token = parse_entry(entry)
        if token is None:
            results.append((p.name, "no token", "-"))
            continue
        status = validate_token(token)
        # Truncate token for display: first 20 chars + "..."
        token_display = token[:20] + "..."
        results.append((p.name, status, token_display))

    # Print tabular output
    col_name = max(len("Profile"), max(len(r[0]) for r in results))
    col_status = max(len("Status"), max(len(r[1]) for r in results))
    col_token = max(len("Token"), max(len(r[2]) for r in results))

    header = f"{'Profile':<{col_name}}  {'Status':<{col_status}}  {'Token':<{col_token}}"
    print(header)
    for name, status, token_display in results:
        print(f"{name:<{col_name}}  {status:<{col_status}}  {token_display:<{col_token}}")

    # Exit 1 if any profile has invalid, unreachable, or indeterminate status
    any_bad = any(
        s in (INVALID, UNREACHABLE, INDETERMINATE)
        for _, s, _ in results
    )
    return 1 if any_bad else 0


def _handle_fix_auth(name: str) -> int:
    """Remove session credentials that shadow a long-lived token."""
    from .profile_ops import fix_auth_shadow

    result = fix_auth_shadow(name)

    if not result.ok:
        if result.reason == "no-token":
            print(f"No long-lived token for '{name}', nothing to fix.", file=sys.stderr)
            sys.exit(1)
        elif result.reason == "unreadable-creds":
            print(f"Cannot read credentials for '{name}'.", file=sys.stderr)
            sys.exit(1)
        else:
            # "no-shadow"
            print(f"No auth shadow detected for '{name}'.")
            return 0

    print(f"Removed session credentials from {name}. Long-lived token will now be used.")
    if result.tier_saved:
        print(f"Saved rate-limit tier: {result.tier_saved}")
    return 0


def _handle_show() -> int:
    from .config import ConfigManager
    cfg = ConfigManager()
    rc = _do_show(cfg)
    if rc != 0:
        sys.exit(rc)
    return 0


def _handle_migrate(src: str, dst: str, uuid: str) -> int:
    from .migrate import migrate_sessions
    uuid_filter = uuid if uuid else None
    try:
        migrate_sessions(src, dst, uuid_filter=uuid_filter, dry_run=False)
    except (FileNotFoundError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    return 0


@strictcli.flag("dry-run", type=bool, default=False, help="preview cleanup changes without writing anything to disk")
def _handle_stats(dry_run: bool) -> int:
    from .stats import run_stats
    run_stats(dry_run=dry_run)
    return 0


@strictcli.flag("dry-run", type=bool, default=False, help="preview the rename and session migration without writing anything to disk")
@strictcli.flag("post-hoc", type=bool, default=False, help="skip filesystem rename, migrate sessions only (directory already renamed)")
def _handle_mv(old: str, new: str, dry_run: bool, post_hoc: bool) -> int:
    from .mv import run_mv
    try:
        run_mv(old, new, dry_run=dry_run, post_hoc=post_hoc)
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    return 0


@strictcli.flag("dry-run", type=bool, default=False, help="preview the import operation without writing any session data to disk")
@strictcli.flag("reid", type=bool, default=False, help="assign new UUIDs to sessions that collide with existing local sessions")
def _handle_import(source: str, from_: list[str], to: list[str], dry_run: bool, reid: bool) -> int:
    from pathlib import Path
    from .import_ import run_import

    if len(from_) != len(to):
        print(
            f"Error: --from and --to must appear the same number of times "
            f"(got {len(from_)} --from and {len(to)} --to)",
            file=sys.stderr,
        )
        return 1

    mappings: list[tuple[str, str]] = []
    for f, t in zip(from_, to):
        resolved = Path(t).expanduser().resolve()
        if not resolved.is_dir():
            print(f"Error: --to path does not exist or is not a directory: {t}", file=sys.stderr)
            return 1
        mappings.append((f, str(resolved)))

    try:
        result = run_import(source, mappings, reid=reid, dry_run=dry_run)
    except (ValueError, FileNotFoundError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result.collisions and not reid:
        print("Collisions detected (use --reid to assign new UUIDs):")
        for c in result.collisions:
            print(f"  {c}")
        return 1

    return 0


@strictcli.flag("all", type=bool, default=False, help="deploy every known hook script from the built-in registry at once")
@strictcli.flag("force-overwrite", type=bool, default=False, help="overwrite existing hook scripts on disk instead of skipping them")
def _handle_deploy_hooks(name: str, all: bool, force_overwrite: bool) -> int:
    from .hook_scripts import HOOK_SCRIPTS, deploy_scripts

    if not name and not all:
        print("Error: provide a script name or --all", file=sys.stderr)
        sys.exit(1)
    if name and all:
        print("Error: --all and a positional name are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if name and name not in HOOK_SCRIPTS:
        known = ", ".join(sorted(HOOK_SCRIPTS))
        print(f"Error: unknown hook script: {name!r} (known: {known})", file=sys.stderr)
        sys.exit(1)

    targets = sorted(HOOK_SCRIPTS) if all else [name]
    for script_name, action in deploy_scripts(targets, SCRIPTS_DIR, force_overwrite):
        dest = SCRIPTS_DIR / script_name
        if action == "exists":
            print(f"already exists: {dest}")
        else:
            print(f"{action}: {dest}")

    return 0


@strictcli.flag("dry-run", type=bool, default=False,
                help="preview the changes without writing anything to disk")
def _handle_patch_profiles(dry_run: bool) -> int:
    from .patch_profiles import run_patch_profiles
    return run_patch_profiles(dry_run=dry_run)


@strictcli.flag("dry-run", type=bool, default=False,
                help="print the per-target permissions diff and change NOTHING (mutually exclusive with --apply; you MUST pass exactly one of --dry-run or --apply)")
@strictcli.flag("apply", type=bool, default=False,
                help="perform the reconciliation, writing each target atomically (mutually exclusive with --dry-run; you MUST pass exactly one of --dry-run or --apply)")
@strictcli.flag("profile", type=str, default="",
                help="reconcile only this single profile; when given, shared-settings.json profileDefaults is left untouched (omit to reconcile every profile AND shared-settings profileDefaults)")
def _handle_reconcile_permissions(dry_run: bool, apply: bool, profile: str) -> int:
    from .reconcile import run_reconcile

    if dry_run == apply:
        # Neither (both False) or both (both True) is a hard error: the caller
        # must declare intent explicitly.
        print(
            "Error: pass exactly one of --dry-run or --apply "
            "(--dry-run previews, --apply writes)",
            file=sys.stderr,
        )
        sys.exit(2)

    return run_reconcile(dry_run=dry_run, profile=profile or None)


def _handle_permission_add(category: str, rule: str,
                           profile: str, all_profiles: bool) -> int:
    from .permission import validate_rule, resolve_profiles, load_settings, add_rule, save_settings

    valid_categories = ("allow", "deny", "ask")
    if category not in valid_categories:
        print(f"Error: category must be one of {', '.join(valid_categories)}, got {category!r}",
              file=sys.stderr)
        sys.exit(1)

    try:
        validate_rule(rule)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    targets = resolve_profiles(profile if profile else None, all_profiles)
    for name, settings_path in targets:
        data = load_settings(settings_path)
        result = add_rule(data, category, rule)
        save_settings(settings_path, data)
        if result == "added":
            print(f"{name}: added {rule} to {category}")
        else:
            print(f"{name}: already in {category}")
    return 0


def _handle_permission_remove(category: str, rule: str,
                              profile: str, all_profiles: bool) -> int:
    from .permission import resolve_profiles, load_settings, remove_rule, save_settings

    valid_categories = ("allow", "deny", "ask")
    if category not in valid_categories:
        print(f"Error: category must be one of {', '.join(valid_categories)}, got {category!r}",
              file=sys.stderr)
        sys.exit(1)

    if not rule.strip():
        print("Error: rule must not be empty", file=sys.stderr)
        sys.exit(1)

    targets = resolve_profiles(profile if profile else None, all_profiles)
    for name, settings_path in targets:
        data = load_settings(settings_path)
        result = remove_rule(data, category, rule)
        if result == "removed":
            save_settings(settings_path, data)
            print(f"{name}: removed {rule} from {category}")
        else:
            print(f"{name}: not found in {category}")
    return 0


@strictcli.flag("format", type=str, help="output format: grouped (indented tree), flat (tsv), or json",
                choices=["grouped", "flat", "json"])
@strictcli.flag("category", type=str, help="restrict output to a single permission category (allow, deny, or ask)",
                default="")
def _handle_permission_list(profile: str, all_profiles: bool,
                            format: str, category: str) -> int:
    import json as json_mod
    from .permission import resolve_profiles, load_settings

    valid_categories = ("allow", "deny", "ask")
    if category and category not in valid_categories:
        print(f"Error: category must be one of {', '.join(valid_categories)}, got {category!r}",
              file=sys.stderr)
        sys.exit(1)

    targets = resolve_profiles(profile if profile else None, all_profiles)
    multi = len(targets) > 1

    for i, (name, settings_path) in enumerate(targets):
        data = load_settings(settings_path)
        perms = data.get("permissions", {})

        if category:
            subset = {category: perms.get(category, [])}
        else:
            subset = {c: perms.get(c, []) for c in ("allow", "deny", "ask")}

        if multi:
            if i > 0:
                print()
            print(f"[{name}]")

        if format == "grouped":
            for cat, rules in subset.items():
                print(f"  {cat}:")
                if rules:
                    for r in rules:
                        print(f"    {r}")
                else:
                    print("    (none)")
        elif format == "flat":
            for cat, rules in subset.items():
                for r in rules:
                    print(f"{cat}\t{r}")
        elif format == "json":
            print(json_mod.dumps(subset, indent=2))

    return 0


def _check_resume_session(session_id: str, directory: str) -> None:
    """Intercept --resume to detect and offer to fix directory renames.

    When a session exists under an old encoded path (because the project
    directory was renamed), this function detects the mismatch and offers
    to move all sessions to the new path via ``run_mv``.

    Returns normally when no interception is needed (session found under
    current directory, or sessions successfully moved).  Calls ``sys.exit(1)``
    when the session cannot be resumed from here.
    """
    from .session import find_session

    store = _shared_store()

    # Step 1: Check if session exists under the current directory
    encoded_cwd = store.encode_path(os.path.abspath(directory))
    expected_path = store.projects_dir / encoded_cwd / f"{session_id}.jsonl"
    if expected_path.exists():
        return  # Claude Code will find it

    # Step 2: Search the entire shared store
    info = find_session(session_id)
    if info is None:
        print(
            f"Session {session_id} not found in any project directory.\n"
            "Try --picker to browse available sessions.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 3: Session found elsewhere -- check if it's a rename or wrong directory
    old_cwd = info.cwd
    if old_cwd is None:
        # Can't extract cwd from JSONL; fall through to let Claude Code handle it
        return

    if os.path.isdir(old_cwd):
        print(
            f"Session {session_id} belongs to {old_cwd} which still exists.\n"
            f"Run from that directory instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 4: Confirmed rename -- old path gone, session found under old encoded dir
    current_dir = os.path.abspath(directory)
    old_project_dir = store.projects_dir / info.encoded_cwd
    jsonl_files = list(old_project_dir.glob("*.jsonl")) if old_project_dir.is_dir() else []
    n = len(jsonl_files)
    size_bytes = sum(f.stat().st_size for f in jsonl_files)
    size_mb = size_bytes / (1024 * 1024)

    print(
        f"Session {session_id} was created in {old_cwd}\n"
        f"which no longer exists. You are now in {current_dir}.\n"
        f"\n"
        f"Found {n} sessions ({size_mb:.1f} MB) under the old path.\n"
        f"Move all sessions from {old_cwd} to {current_dir}? [y/N] ",
        end="",
        flush=True,
    )
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    if not answer.strip().lower().startswith("y"):
        print("Aborted. Sessions remain under the old path.")
        sys.exit(1)

    # Step 5: Dry-run first (quiet -- no per-file log spam)
    from .mv import run_mv

    result = run_mv(old_cwd, current_dir, dry_run=True, quiet=True, post_hoc=True)
    print(
        f"\nWill move {result.files_rewritten} session files, "
        f"rewrite {result.lines_replaced} path references, "
        f"update {result.project_keys_updated} profile keys."
        f"\nProceed? [y/N] ",
        end="",
        flush=True,
    )
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    if not answer.strip().lower().startswith("y"):
        print("Aborted.")
        sys.exit(1)

    # Step 6: Execute for real
    result = run_mv(old_cwd, current_dir, dry_run=False, quiet=True, post_hoc=True)
    print("Done. Resuming session...")


def _check_cont_session(directory: str) -> None:
    """Intercept --cont to detect and offer to fix directory renames.

    When the current directory has no sessions but an orphaned project
    directory exists under the same parent (original cwd no longer on
    disk), this function offers to move those sessions to the current
    directory via ``run_mv``.
    """
    from .session import find_orphaned_project_dirs

    store = _shared_store()
    current_dir = os.path.abspath(directory)

    # Step 1: Check if sessions exist under the current directory
    encoded_cwd = store.encode_path(current_dir)
    project_dir = store.projects_dir / encoded_cwd
    if project_dir.is_dir() and list(project_dir.glob("*.jsonl")):
        return  # Claude Code will find sessions

    # Step 2: Scan all project dirs for orphans (cwd no longer on disk)
    candidates = find_orphaned_project_dirs()

    # Step 3: No candidates
    if not candidates:
        return  # let Claude Code handle it

    # Step 4/5: Present candidates and offer to move
    if len(candidates) == 1:
        orphan = candidates[0]
        size_mb = orphan.total_size_bytes / (1024 * 1024)
        print(
            f"No sessions found under {current_dir}.\n"
            f"Found {orphan.session_count} sessions ({size_mb:.1f} MB) "
            f"under {orphan.cwd} which no longer exists.\n"
            f"Move all sessions from {orphan.cwd} to {current_dir}? [y/N] ",
            end="",
            flush=True,
        )
        try:
            answer = input()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not answer.strip().lower().startswith("y"):
            return

        old_cwd = orphan.cwd
    else:
        # Multiple candidates
        print(f"No sessions found under {current_dir}.")
        print("Found sessions under multiple directories that no longer exist:")
        for i, orphan in enumerate(candidates, 1):
            size_mb = orphan.total_size_bytes / (1024 * 1024)
            print(f"  {i}. {orphan.cwd} ({orphan.session_count} sessions, {size_mb:.1f} MB)")
        print(f"Move sessions from which directory? [1-{len(candidates)}/n to skip] ", end="", flush=True)
        try:
            answer = input()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        answer = answer.strip().lower()
        if answer == "n" or not answer:
            return
        try:
            idx = int(answer) - 1
            if idx < 0 or idx >= len(candidates):
                return
        except ValueError:
            return
        old_cwd = candidates[idx].cwd

    # Two-prompt flow: dry run, then confirm and execute
    from .mv import run_mv

    result = run_mv(old_cwd, current_dir, dry_run=True, quiet=True, post_hoc=True)
    print(
        f"\nWill move {result.files_rewritten} session files, "
        f"rewrite {result.lines_replaced} path references, "
        f"update {result.project_keys_updated} profile keys."
        f"\nProceed? [y/N] ",
        end="",
        flush=True,
    )
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if not answer.strip().lower().startswith("y"):
        return

    result = run_mv(old_cwd, current_dir, dry_run=False, quiet=True, post_hoc=True)
    print("Done. Resuming session...")


# "continue" and "print" are Python keywords, so we use "cont" / "print-prompt"
# as flag names. Short forms -c and -p remain the same for user convenience.
def _handle_launch(
    # Session flags (via tag); mutually exclusive, all optional
    cont: bool, resume: str, print_prompt: str, picker: bool,
    # Segment flags (via tag); empty string means "not provided"
    profile: str, github: str, model: str,
    directory: str, mcp: str, permissions: str,
    # Repeatable set flag (via tag)
    set: list[str],
) -> int:
    # Normalize sentinel defaults to None for cleaner downstream logic
    _UNSET = "\x00__unset__"
    resume_val: str | None = None if resume == _UNSET else resume
    print_prompt_val: str | None = None if print_prompt == _UNSET else print_prompt

    provided = sum([cont, resume_val is not None, print_prompt_val is not None, picker])
    if provided > 1:
        print("Error: --cont, --resume, --print-prompt, and --picker are mutually exclusive",
              file=sys.stderr)
        sys.exit(1)

    from .app import App as TuiApp
    from .config import ConfigManager

    cfg = ConfigManager()
    enabled = cfg.config.get("enabled_segments", [])
    segment_keys = [s["key"] for s in cfg.segments_def if s["key"] in enabled]

    # Collect segment value overrides from individual flags.
    # Empty string means "not provided" (strictcli default for optional str flags).
    segment_overrides: dict[str, str] = {}
    segment_sources: dict[str, str] = {}
    flag_values = {
        "profile": profile, "github": github, "model": model,
        "directory": directory, "mcp": mcp, "permissions": permissions,
    }
    for key in segment_keys:
        val = flag_values.get(key)
        if val:
            segment_overrides[key] = val
            segment_sources[key] = f"--{key}"

    # Merge -s key=value overrides; duplicates from ANY source are rejected.
    for item in set:
        if "=" not in item:
            print(f"Invalid -s format: {item!r} (expected KEY=VALUE)", file=sys.stderr)
            sys.exit(1)
        key, _, value = item.partition("=")
        if key not in segment_keys:
            print(f"Unknown segment: {key!r} (available: {', '.join(segment_keys)})", file=sys.stderr)
            sys.exit(1)
        if key in segment_overrides:
            prior_value = segment_overrides[key]
            prior_source = segment_sources[key]
            print(
                f"Duplicate segment override for {key!r}: "
                f"{prior_value!r} (from {prior_source}) and {value!r} (from -s)",
                file=sys.stderr,
            )
            sys.exit(1)
        segment_overrides[key] = value
        segment_sources[key] = "-s"

    # Default directory to cwd if not explicitly set
    if "directory" in segment_keys and "directory" not in segment_overrides:
        segment_overrides["directory"] = os.getcwd()

    # Build extra Claude Code flags from session/print flags
    extra_flags: list[str] = []
    if cont:
        extra_flags.append("--continue")
    elif resume_val is not None:
        extra_flags.append("--resume")
        if resume_val:
            extra_flags.append(resume_val)
    elif picker:
        extra_flags.append("--resume")
    elif print_prompt_val is not None:
        extra_flags.extend(["--print", print_prompt_val])

    # Append passthrough args (everything after "--" in original argv)
    extra_flags.extend(_passthrough)

    # Intercept --resume/--cont to detect directory renames and offer to
    # move sessions before Claude Code tries to find them.
    if resume_val:
        target_dir = segment_overrides.get("directory", os.getcwd())
        _check_resume_session(resume_val, target_dir)
    if cont:
        _check_cont_session(segment_overrides.get("directory", os.getcwd()))

    # Skip TUI when args cover every required segment, or when print mode is active.
    required_keys = {s["key"] for s in cfg.segments_def
                     if s["key"] in enabled and s.get("required", False)}
    skip_tui = print_prompt_val is not None or (
        required_keys and all(k in segment_overrides for k in required_keys)
    )

    # A corrupt tokens.json surfaces as a TokenStoreError from the launch
    # sequence. Catch it narrowly at this handler boundary so the user sees a
    # clean, actionable message instead of a Python traceback.
    from .tokens import TokenStoreError
    try:
        if skip_tui:
            merged = dict(cfg.state.get("last_config", {}))
            merged.update(segment_overrides)
            if print_prompt_val is not None:
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
                                interactive=print_prompt_val is None)
            return 0

        # Otherwise show the TUI (pre-filled from last_config + arg overrides)
        app = TuiApp(cfg=cfg, overrides=segment_overrides)
        selections = app.run_tui()
        if selections is None:
            return 0

        # Extract per-segment metadata from the bar for resolve_launch_config
        bar_metadata: dict[str, dict[str, dict]] = {}
        for seg in app.bar.segments:
            if seg.state.metadata:
                bar_metadata[seg.key] = seg.state.metadata

        _do_launch_sequence(
            app.cfg, selections, extra_flags=extra_flags,
            metadata=bar_metadata or None,
        )
        return 0
    except TokenStoreError as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand names for routing
# ---------------------------------------------------------------------------
_SUBCOMMANDS = frozenset({
    "health", "config", "versions", "install", "uninstall",
    "reset-options", "show",
    "migrate", "stats", "mv", "import", "deploy-hooks", "patch-profiles",
    "reconcile-permissions", "launch",
    "permission", "profile",
    # Deprecated top-level names kept here so main() doesn't rewrite
    # e.g. "c new-profile" to "c launch new-profile" before the
    # deprecation handler can fire.
    "new-profile", "delete-profile", "show-profile",
})

# Flags that must be handled at the app level rather than routed to the
# "launch" subcommand. --help/--version show the app-wide help/version, and
# --dump-schema is a strictcli reserved flag that dumps the CLI schema.
_APP_LEVEL_FLAGS = frozenset({"--help", "-h", "--version", "-v", "--dump-schema"})


def _inject_launch(argv: list[str]) -> list[str]:
    """Return argv with the "launch" subcommand injected when appropriate.

    argv includes argv[0] (the program name). When no subcommand is given, or
    the leading token is neither a known subcommand nor an app-level flag, the
    "launch" subcommand is injected so the interactive TUI starts. App-level
    flags (see _APP_LEVEL_FLAGS) and known subcommands are left untouched.
    """
    rest = argv[1:]
    if not rest or (rest[0] not in _SUBCOMMANDS and rest[0] not in _APP_LEVEL_FLAGS):
        return [argv[0], "launch"] + rest
    return list(argv)


def _build_app() -> App:
    """Build the strictcli App with all subcommands registered."""
    app = App(name="c", version=__version__, help="claudewheel - TUI launcher for Claude Code with profile, model, and directory selection")

    # -- One-shot commands --

    app.command("health", help="run diagnostic health checks on profiles, tokens, and hooks, then exit")(
        _handle_health
    )

    app.command("config", help="open the ~/.claudewheel/ config directory in your $EDITOR")(
        _handle_config
    )

    app.command("versions", help="list all installed Claude Code versions, marking the current symlink target")(
        _handle_versions
    )

    app.command("install", help="download and install a specific Claude Code version",
                args=[Arg(name="version", help="semver version string to download and install (e.g. 2.1.119)")])(
        _handle_install
    )

    app.command("uninstall", help="delete an installed Claude Code version binary from the versions directory",
                args=[Arg(name="version", help="semver version string to remove (refuses if it is the current symlink target)")])(
        _handle_uninstall
    )

    app.command("reset-options", help="delete options.json so it regenerates from defaults")(
        _handle_reset_options
    )

    # -- Profile group --
    profile_grp = app.group("profile", help="create, inspect, rename, delete, and manage Claude Code profiles and their stored tokens")

    profile_grp.command("create", help="create a new profile interactively through a guided wizard, then set up its authentication")(
        _handle_new_profile
    )

    profile_grp.command("delete", help="delete a registered profile and clean up its directory, tokens, and options entries",
                        args=[Arg(name="name", help="name of the profile to delete (e.g. work, personal, lisa)")])(
        _handle_delete_profile
    )

    profile_grp.command("show", help="inspect a profile's configuration, authentication status, and session data in a detailed report",
                        args=[Arg(name="name", help="name of the profile to inspect (e.g. work, personal, default)")])(
        _handle_show_profile
    )

    profile_grp.command("rename", help="rename a profile, moving its directory, tokens, and session data to the new name",
                        args=[Arg(name="old", help="current name of the profile to rename (must be an existing, non-running profile)"),
                              Arg(name="new", help="new name for the profile (lowercase letters, digits, and hyphens; must be unused)")])(
        _handle_rename_profile
    )

    profile_grp.command("fix-auth", help="remove session credentials that shadow a long-lived token",
                        args=[Arg(name="name", help="name of the profile whose shadowing session credentials should be removed")])(
        _handle_fix_auth
    )

    profile_grp.command("check-tokens", help="validate every discovered profile's stored OAuth token against the Anthropic API")(
        _handle_check_tokens
    )

    # Hard-break old top-level names so they fail loudly with migration guidance
    app.deprecate("new-profile", message="Renamed: use 'claudewheel profile create' instead.")
    app.deprecate("delete-profile", message="Renamed: use 'claudewheel profile delete <name>' instead.")
    app.deprecate("show-profile", message="Renamed: use 'claudewheel profile show <name>' instead.")

    app.command("show", help="print a summary of current segment selections, theme, and recent directories")(
        _handle_show
    )

    app.command("migrate", help="move session data files from one profile to another, optionally filtered by UUID",
                args=[
                    Arg(name="src", help="source profile name whose sessions will be moved (e.g. work)"),
                    Arg(name="dst", help="destination profile name to receive the migrated sessions (e.g. personal)"),
                    Arg(name="uuid", help="optional UUID substring to migrate only matching sessions", required=False, default=""),
                ])(
        _handle_migrate
    )

    app.command("stats", help="report shared-store stats and clean up legacy data")(
        _handle_stats
    )

    app.command("mv", help="rename a project directory and migrate session data",
                args=[
                    Arg(name="old", help="current path of the project directory to rename (absolute or relative)"),
                    Arg(name="new", help="target path for the renamed project directory (absolute or relative)"),
                ])(
        _handle_mv
    )

    app.command("import", help="import session data from an external Claude Code directory",
                args=[
                    Arg(name="source", help="path to the source directory (e.g., /path/to/backup/.claude)"),
                ],
                flag_sets=[
                    FlagSet(name="mapping", flags=[
                        Flag(name="from", type=str, repeatable=True, unique=False,
                             help="original project path as recorded in the source session data (repeatable)"),
                        Flag(name="to", type=str, repeatable=True, unique=False,
                             help="local directory path that corresponds to the --from path on this machine (repeatable)"),
                    ]),
                ],
                dependencies=[
                    CoRequired(flags=["from", "to"]),
                ])(
        _handle_import
    )

    app.command("deploy-hooks", help="deploy built-in hook scripts to the ~/.claudewheel/scripts/ directory",
                args=[Arg(name="name", help="name of the specific hook script to deploy (omit to use --all)", required=False, default="")])(
        _handle_deploy_hooks
    )

    app.command("patch-profiles", help="sync existing profiles and shared-settings.json to canonical hook and disallowedTools defaults")(
        _handle_patch_profiles
    )

    app.command("reconcile-permissions", help="reconcile profile and shared-settings permissions (deny/ask/allow) to the canonical guardrail model; requires exactly one of --dry-run or --apply")(
        _handle_reconcile_permissions
    )

    # -- Permission group --
    _profile_mutex = MutexGroup(flags=[
        Flag(name="profile", type=str, help="target a specific profile by name (mutually exclusive with --all-profiles)"),
        Flag(name="all-profiles", type=bool, default=False, help="apply the operation to every registered profile at once"),
    ])

    perm_grp = app.group("permission", help="add, remove, and list permission rules across Claude profiles")

    perm_grp.command("add", help=(
                         "Add a permission rule to a profile's settings.json. Takes a category"
                         " (allow, deny, or ask) and a rule string such as Bash or Read(//home/**)."
                         " Writes the rule into the specified category array. Use --profile to target"
                         " a single profile or --all-profiles to apply the rule across every registered"
                         " profile. Skips duplicates if the rule already exists in the category."
                     ),
                     args=[
                         Arg(name="category", help="permission category to add the rule to: allow, deny, or ask"),
                         Arg(name="rule", help="permission rule string to add (e.g. Bash, Read(//home/**), Edit)")
                     ],
                     mutex=[_profile_mutex])(
        _handle_permission_add
    )

    perm_grp.command("remove", help=(
                         "Remove a permission rule from a profile's settings.json. Takes a category"
                         " (allow, deny, or ask) and the exact rule string to delete. The rule is"
                         " removed from the specified category array and the file is saved. Use"
                         " --profile to target a single profile or --all-profiles to remove the rule"
                         " from every registered profile. Reports whether the rule was found."
                     ),
                     args=[
                         Arg(name="category", help="permission category to remove the rule from: allow, deny, or ask"),
                         Arg(name="rule", help="exact permission rule string to remove (must match an existing entry)"),
                     ],
                     mutex=[_profile_mutex])(
        _handle_permission_remove
    )

    perm_grp.command("list", help=(
                         "List permission rules from a profile's settings.json. Displays rules in"
                         " grouped, flat, or JSON format controlled by --format. Use --category to"
                         " filter output to a single category (allow, deny, or ask). Use --profile"
                         " to inspect a single profile or --all-profiles to show rules from every"
                         " registered profile, with each profile's rules displayed under a header."
                     ),
                     mutex=[_profile_mutex])(
        _handle_permission_list
    )

    # -- Launch command (default when no subcommand given) --
    _UNSET = "\x00__unset__"  # sentinel to distinguish "not passed" from ""
    _session_flag_set = FlagSet(name="session", flags=[
        Flag(name="cont", short="c", type=bool, default=False,
             help="continue the most recent conversation in the current directory"),
        Flag(name="resume", short="r", type=str, default=_UNSET,
             help="resume a specific session by its UUID, or pass empty string to open the picker"),
        Flag(name="print-prompt", short="p", type=str, default=_UNSET,
             help="run in non-interactive print mode with the given prompt"),
        Flag(name="picker", type=bool, default=False,
             help="open the interactive session resume picker to browse and select a session"),
    ])

    _segment_flag_set = FlagSet(name="segments", flags=[
        Flag(name="profile", type=str, default="",
             help="preset the Profile segment to this value, skipping TUI selection for it"),
        Flag(name="github", type=str, default="",
             help="preset the GitHub account segment to this value, skipping TUI selection for it"),
        Flag(name="model", type=str, default="",
             help="preset the Model segment to this value (e.g. opus, sonnet), skipping TUI selection"),
        Flag(name="directory", type=str, default="",
             help="preset the Directory segment to this path, skipping TUI selection for it"),
        Flag(name="mcp", type=str, default="",
             help="preset the MCP mode segment to this value, skipping TUI selection for it"),
        Flag(name="permissions", type=str, default="",
             help="preset the Permissions segment to this value, skipping TUI selection for it"),
        Flag(name="set", short="s", type=str, repeatable=True, unique=False,
             help="set any segment value as KEY=VALUE (e.g. -s version=2.1.119); repeatable"),
    ])

    app.command("launch", help="start the interactive TUI launcher to select a profile, model, and directory",
                flag_sets=[_session_flag_set, _segment_flag_set])(
        _handle_launch
    )

    return app


def main() -> None:
    """CLI entry point that parses arguments and dispatches to subcommands or the TUI."""
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
    # Exception: app-level flags (--help/-h/--version/-v/--dump-schema) are
    # handled at the app level, not routed to the launch command.
    sys.argv = _inject_launch(sys.argv)

    _build_app().run()
