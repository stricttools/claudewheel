"""CLI argument parsing, subcommand routing, and launch orchestration."""

from __future__ import annotations

import inspect
import os
import re
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import strictcli
from strictcli import AllOrNone, App, Arg, Choice, Flag, FlagSet, Grant, Member

from . import __version__
from . import effects
from .clients import CLIENT_NAMES, DEFAULT_CLIENT, resolve_default_client

if TYPE_CHECKING:
    from .archiver import Saferm, Unavailable
    from .binaries import BinaryLocator
    from .config import AppConfigStore
    from .workspace import Workspace

# Passthrough args after "--" are stashed here by main() before strictcli sees argv.
_passthrough: list[str] = []


def _absent(value: Any, fallback: Any) -> Any:
    """Resolve an optional flag's absence to the fallback its own help declares.

    strictcli forbids ``default=`` on any flag or arg of a ``mutating``
    command: a value the framework picks is a value the framework writes. The
    opt-in switches of claudewheel's mutating commands (``--all``,
    ``--force-overwrite``, ``--reid``, ``--post-hoc``) therefore declare
    ``presence="optional"`` and name their fallback in their help text, and
    this is the single place where absence becomes that fallback -- so no
    downstream branch ever sees a ``None`` it would read as a value.
    """
    return fallback if value is None else value


def _do_uninstall(locator: "BinaryLocator", version: str) -> int:
    """Delete an installed Claude Code version binary.

    Refuses to delete the version the `claude` symlink currently points to,
    since that would break the default `claude` command. Returns a process
    exit code.
    """
    target = locator.binary_for(version)
    if not target.exists():
        print(f"Version {version} is not installed at {target}", file=sys.stderr)
        return 1

    # Refuse to remove the version the symlink currently resolves to.
    resolved = locator.symlink_target()
    if resolved is not None and resolved.name == version:
        print(
            f"Refusing to uninstall {version}: it is the current "
            f"`claude` symlink target ({locator.claude_symlink}). "
            "Switch to another version first.",
            file=sys.stderr,
        )
        return 1

    try:
        effects.remove(target)
    except OSError as e:
        print(f"Failed to delete {target}: {e}", file=sys.stderr)
        return 1
    verb = "Would uninstall" if effects.previewing() else "Uninstalled"
    print(f"{verb} {version} ({target})")
    return 0


def _do_reset_options(ws: "Workspace") -> int:
    """Delete options.json so it regenerates from defaults on next run.

    Does NOT instantiate AppConfigStore -- the next normal run will recreate
    options.json via `_ensure_dir`. Idempotent: missing file is not an error.
    """
    options_file = ws.options_file
    if options_file.exists():
        try:
            effects.remove(options_file)
        except OSError as e:
            print(f"Failed to delete {options_file}: {e}", file=sys.stderr)
            return 1
        verb = "Would delete" if effects.previewing() else "Deleted"
        print(f"{verb} {options_file}; defaults will regenerate on next run.")
    else:
        print(f"{options_file} does not exist; nothing to reset.")
    return 0


def _do_show(cfg: "AppConfigStore") -> int:
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


def _launch_is_interactive(print_prompt: str | None) -> bool:
    """Whether this launch may prompt: is there a terminal, and a human's session?

    Two conditions, and the terminal one is the substantive half. A launch
    whose segments all came from flags skips the TUI but is otherwise an
    ordinary interactive run -- it may prompt when a person is there and must
    not when nobody is. Deriving that from print mode alone made a headless,
    flag-driven launch believe it was interactive, so every prompting step went
    ahead and tried to open a terminal that does not exist.

    Print mode stays non-interactive regardless: ``--print`` is a machine
    invocation whose stdout is the answer, so a prompt would corrupt it even
    with a terminal attached.
    """
    from .terminal import has_controlling_terminal

    return print_prompt is None and has_controlling_terminal()


def _do_launch_sequence(
    ws: "Workspace",
    locator: "BinaryLocator",
    cfg: "AppConfigStore",
    selections: dict[str, str | None],
    extra_flags: list[str] | None = None,
    interactive: bool = True,
    metadata: dict[str, dict[str, dict[str, Any]]] | None = None,
    client: str = DEFAULT_CLIENT,
    passthrough: list[str] | None = None,
) -> None:
    """Run health check, hooks, save state, resolve, and exec. Does not return on success."""
    from .health import run_health_check, print_health_report
    from .hooks import run_hooks
    from .launch import resolve_launch_config, do_launch
    from .state import record_inode, save_launch_state

    if interactive and cfg.config.get("health_check_on_launch", True):
        results = run_health_check(ws)
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
    if not run_hooks(ws.hooks_dir, "pre-launch", selections):
        print("Pre-launch hook failed. Aborting.", file=sys.stderr)
        sys.exit(1)
    # Save state only after hooks succeed, so launch_count isn't inflated by aborts
    if interactive:
        save_launch_state(cfg, selections)
        record_inode(ws.shared, selections.get("directory") or os.getcwd())
    # Preflight steps run here, independent of the health_check_on_launch gate,
    # after state is saved and before the launch config is resolved. The terminal
    # is in cooked mode; UI-rendering steps manage their own raw mode. An ABORT
    # prints its actionable message to stderr and exits nonzero.
    from .preflight import PreflightContext, run_preflight

    preflight_result = run_preflight(
        PreflightContext(
            selections=selections,
            workspace=ws,
            locator=locator,
            cfg=cfg,
            interactive=interactive,
        )
    )
    if preflight_result is not None and preflight_result.is_abort:
        print(preflight_result.message, file=sys.stderr)
        sys.exit(1)
    # The workspace ProfileStore supplies both config dir and token via env(). A
    # stale/unknown profile name raises ValueError (the hard-error contract); a
    # corrupt token entry raises TokenStoreError. Both are caught here so the
    # user sees a clean message, never a traceback.
    try:
        cwd, argv, env = resolve_launch_config(
            selections,
            cfg.options_def,
            cfg.config.get("default_flags", []),
            locator=locator,
            profiles=ws.profiles,
            extra_flags=extra_flags,
            metadata=metadata,
            client=client,
            clients_config=cfg.config.get("clients", {}),
            passthrough=passthrough,
        )
        # Nothing is written into the profile's config dir here. The plan-tier
        # fields reach Claude Code through the launch environment (see
        # ProfileStore.env); a launch never touches its .credentials.json.
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
# command.  Handlers that need an AppConfigStore instantiate it lazily (only
# the ones that actually need it), keeping the one-shot commands fast.


def _handle_health(ws: "Workspace") -> int:
    """Run diagnostic health checks and print results."""
    from .health import run_health_check, print_health_report

    results = run_health_check(ws)
    print_health_report(results)
    if not all(r.ok for r in results):
        sys.exit(1)
    return 0


def _handle_config(ws: "Workspace") -> int:
    """Open the config directory in the user's preferred editor."""
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    os.execlp(editor, editor, str(ws.root))
    return 0


def _handle_versions(locator: "BinaryLocator") -> int:
    """List installed Claude Code versions, marking the current symlink target."""
    versions = locator.installed_versions()

    # Determine which version the symlink points to
    target = locator.symlink_target()
    current_version = target.name if target is not None else None

    if not versions:
        print("No versions found in", locator.versions_dir)
    else:
        for v in versions:
            suffix = " (current)" if v == current_version else ""
            print(f"  {v}{suffix}")
    return 0


def _handle_install(locator: "BinaryLocator", version: str) -> int:
    """Download and install a specific Claude Code version."""
    from .install import install_version

    def on_progress(downloaded: int, total: int) -> None:
        if total > 0:
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            pct = downloaded * 100 // total
            print(f"\r  {mb_done:.0f}/{mb_total:.0f} MB ({pct}%)", end="", flush=True)

    previewing = effects.previewing()
    verb = "Would download" if previewing else "Downloading"
    print(f"{verb} Claude Code {version}...")
    try:
        dest = install_version(locator, version, progress_callback=on_progress)
        outcome = "Would install to" if previewing else "\nInstalled to"
        print(f"{outcome} {dest}")
    except OSError as e:
        print(f"\nInstallation failed: {e}", file=sys.stderr)
        sys.exit(1)
    return 0


def _handle_uninstall(locator: "BinaryLocator", version: str) -> int:
    """Uninstall a specific Claude Code version binary."""
    rc = _do_uninstall(locator, version)
    if rc != 0:
        sys.exit(rc)
    return 0


def _handle_reset_options(ws: "Workspace") -> int:
    """Delete options.json so defaults regenerate on next run."""
    rc = _do_reset_options(ws)
    if rc != 0:
        sys.exit(rc)
    return 0


def _handle_new_profile(ws: "Workspace", locator: "BinaryLocator") -> int:
    """Run the create-profile flow as one continuous alt-screen session.

    Mirrors the TUI path: wizard form, auth forms, and summary page all
    render borrowed in a single alt-screen raw session on a CLI-owned
    terminal. After the session ends, the summary and auth outcome are
    printed to stdout as a persistent record.
    """
    from .config import resolve_theme_name
    from .terminal import Terminal
    from .theme import parse_theme
    from .ui import show_page
    from .wizard import run_profile_wizard, create_profile, run_auth_flow

    cfg = ws.appconfig()
    theme_name = resolve_theme_name(cfg.config.get("theme", "auto"))
    theme = parse_theme(cfg.load_theme(theme_name))
    # Requires a real TTY; a headless environment fails here, loudly.
    terminal = Terminal()
    existing = [p.name for p in ws.profiles.enumerate()]

    cancelled = False
    summary: list[str] = []
    outcome = ""
    try:
        terminal.enter_raw(alt_screen=True)
        try:
            result = run_profile_wizard(ws, existing, theme, terminal)
            if result.cancelled:
                cancelled = True
            else:
                summary = create_profile(ws, result, previewing=effects.previewing())
                outcome = run_auth_flow(
                    ws, locator, result.config_dir, result.name, theme, terminal
                )
                title = (
                    "Profile would be created"
                    if effects.previewing()
                    else "Profile created"
                )
                show_page(title, summary, theme, terminal)
        finally:
            terminal.exit_raw()
    finally:
        terminal.close()

    if cancelled:
        print("Cancelled.")
        return 0
    previewing = effects.previewing()
    for line in summary:
        print(line)
    if outcome == "authenticated":
        print(
            "Profile would be authenticated."
            if previewing
            else "Profile authenticated."
        )
    elif outcome == "unverified":
        print(
            "Token would be saved without validation (API unreachable)."
            if previewing
            else "Token saved without validation (API unreachable)."
        )
    elif outcome == "cancel":
        print(
            "Auth setup cancelled -- you can authenticate later by launching the profile."
        )
    elif outcome == "failed":
        print("Auth setup failed -- you can retry by launching the profile.")
    return 0


def _resolve_archiver(ws: "Workspace", name: str) -> "Saferm | Unavailable":
    """The archiving tool this deletion will use, or why there is none.

    Deletion delegates to saferm so it can be undone, which makes "is saferm
    here, and does it ship what the delegation uses" a precondition of the
    operation rather than a detail of it. This is where that is decided, for
    the scripted door.

    A run with no terminal gets no second consent to ask for -- the framework's
    confirmation already happened, or was answered in advance with
    ``--approve-consequential`` -- so it does not get an install offer either.
    The caller turns the answer into a hard error, which is the input a machine
    caller can act on: nothing happened, and the profile is still there.

    At a terminal there IS someone to ask, so the missing tool becomes an offer
    to install it. Declining aborts the deletion; a failed install aborts it
    too. Neither ever falls through to removing the directory some other way.
    """
    from .archiver import Unavailable, detect

    found = detect(ws.root)
    if not isinstance(found, Unavailable):
        return found
    if effects.previewing() or not sys.stdin.isatty():
        # A preview is the one case where a terminal is not enough: installing
        # a program for real is exactly what a run promising to change nothing
        # must not do.
        return found
    return _offer_saferm_install(ws, name, found)


def _offer_saferm_install(
    ws: "Workspace", name: str, unavailable: "Unavailable"
) -> "Saferm | Unavailable":
    """Ask whether to install saferm, and install it if the answer is yes.

    Shaped on the Claude Code install: the release's published checksum
    manifest is fetched first, this platform's asset is downloaded, and its
    SHA-256 is checked against the manifest before anything is unpacked or put
    on disk. A mismatch installs nothing.

    The answer to a declined offer, a failed download and a fresh binary that
    STILL does not answer the probe is the same one: hand the caller back an
    unavailable tool and let the deletion be refused. There is deliberately no
    branch here that gives up on the archive and deletes the profile anyway.
    """
    from .archiver import INSTALL_COMMANDS, InstallError, Unavailable, detect, install

    verb = "Upgrade" if unavailable.upgrade else "Install"
    print(unavailable.diagnosis())
    print(unavailable.stakes(name))
    print(f"{verb} it now from its published release? [y/N] ", end="", flush=True)
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        print()
        return unavailable
    if not answer.strip().lower().startswith("y"):
        print(f"Declined. Profile '{name}' was not deleted.")
        print("Install it yourself with one of:")
        for command in INSTALL_COMMANDS:
            print(f"  {command}")
        return unavailable

    try:
        binary = install(ws.root)
    except InstallError as e:
        # A hard error, never a quiet fall back to destroying the directory.
        print(f"error: {e}", file=sys.stderr)
        return unavailable
    print(f"Installed saferm at {binary}.")

    # Re-run detection over what was just installed rather than assuming it:
    # the offer's own promise is that the deletion proceeds only against a tool
    # that has answered the probe.
    found = detect(ws.root)
    if isinstance(found, Unavailable):
        print(
            "error: the saferm just installed still does not ship what "
            "claudewheel needs.",
            file=sys.stderr,
        )
    return found


@strictcli.flag(
    "force-delete",
    type=bool,
    presence="required",
    help="delete anyway when the profile holds a live interactive Claude Code session (background jobs and daemons never block deletion)",
)
@strictcli.flag(
    "force-delete-data",
    type=bool,
    presence="required",
    help="delete even when shared-dir names hold REAL data instead of symlinks; this DESTROYS that data (e.g. conversation history)",
)
def _handle_delete_profile(
    ws: "Workspace", name: str, force_delete: bool, force_delete_data: bool
) -> int:
    """Delete a profile via ProfileStore. The running check is CLI policy.

    This is the scripted door, so it stops nothing: the interactive checklist
    that offers to stop the processes holding a profile belongs to the TUI,
    where there is a person to tick the boxes. What this path does instead is
    refuse to be silent about them -- every live holder is read BEFORE the
    removal (afterwards the registry is gone with the directory) and named in
    the summary, because a surviving process still carries CLAUDE_CONFIG_DIR
    and recreates the directory on its next write.

    The archiving tool is resolved HERE, not in the store, and not by the
    framework's confirmation prompt: that prompt fires before dispatch with a
    string pinned verbatim by tests, so the handler is the first place that can
    say anything about saferm at all. What it says depends on whether there is
    anyone to say it to -- an offer where there is a terminal, a hard error
    where there is not.
    """
    from . import session_registry
    from .archiver import ArchiveError, ArchiveUnreadable, Unavailable
    from .profile_store import DeletionBookkeepingError

    # The reserved-name query comes first, before anything is read or printed:
    # a name claudewheel does not own has no holders worth listing and no
    # deletion to describe.
    reserved = ws.profiles.reserved_reason(name)
    if reserved is not None:
        print(reserved, file=sys.stderr)
        sys.exit(1)

    # Read the holders while the registry still exists -- it lives inside the
    # directory this command is about to remove.
    holders = session_registry.live_records(ws.profiles.path_for(name))

    # Running check is CLI policy (ProfileStore.delete does not enforce it).
    if not force_delete and any(record.interactive for record in holders):
        print(
            f"Profile '{name}' has a live interactive session. "
            "Use --force-delete to delete anyway.",
            file=sys.stderr,
        )
        sys.exit(1)

    archiver = _resolve_archiver(ws, name)
    if isinstance(archiver, Unavailable):
        print(
            archiver.refusal_error(name, previewing=effects.previewing()),
            file=sys.stderr,
        )
        sys.exit(1)

    store = ws.profiles
    try:
        result = store.delete(
            name, archiver=archiver, allow_data_destruction=force_delete_data
        )
    except ValueError as e:
        # Covers default / not-found / data-destruction refusals.
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except ArchiveUnreadable as e:
        # Caught ahead of ArchiveError because it is the one member of that
        # family where the archival DID run: the directory is gone, and only
        # claudewheel's reading of the answer failed. The registration is
        # therefore stale too, and the same deletion finishes that cleanup --
        # it finds no directory to archive the second time.
        print(f"error: {e}", file=sys.stderr)
        print(
            f"claudewheel's own registration was not updated: run "
            f"`claudewheel profile delete {name}` again to finish it, which "
            f"archives nothing because the directory is already gone.",
            file=sys.stderr,
        )
        sys.exit(1)
    except ArchiveError as e:
        # The archival is the first destructive step, so a refusal here means
        # nothing happened at all: the profile is on disk and every store still
        # names it. There is deliberately no branch that removes it anyway.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except DeletionBookkeepingError as e:
        # Past the archival: the profile is archived and gone, and only
        # claudewheel's own registration is stale. The message carries the
        # handle, which is why this is not allowed to surface as a bare OSError.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    previewing = effects.previewing()
    print(f"{'Would delete' if previewing else 'Deleting'} profile '{name}'...")
    print(
        f"  {'Would remove' if previewing else 'Removed'} dir: "
        f"{result.removed_symlinks} symlinks unlinked, "
        f"{result.removed_real} real entries removed"
    )
    removed = "Would remove" if previewing else "Removed"
    if result.removed_from_options:
        print(f"  {removed} from options.json")
    else:
        print("  Not found in options.json (already clean)")
    if result.last_config_purged:
        print(
            f"  {'Would clear' if previewing else 'Cleared'} last_config "
            "profile reference in state.json"
        )
    if holders:
        pids = ", ".join(f"{r.name or r.kind} (pid {r.pid})" for r in holders)
        print(
            f"  {len(holders)} process(es) still hold this profile: {pids}. "
            "They carry CLAUDE_CONFIG_DIR and will recreate the directory on "
            "their next write -- stop them, or delete the profile from the TUI, "
            "which offers to stop them for you."
        )
    if previewing:
        print(f"Would delete profile '{name}'.")
    elif holders:
        print(
            f"Profile '{name}' deleted, but {len(holders)} process(es) still hold it."
        )
    else:
        print(f"Profile '{name}' deleted.")
    if result.archive is not None:
        # Reported, never recorded: saferm's archive is the authority on what
        # was deleted and keeps its own audit trail, so the handle is printed
        # where the person who just deleted a profile can act on it, and after
        # that it lives in `saferm list` like every other deletion.
        print(f"  Archived as {result.archive.uuid}")
        print(f"  Restore it with: {result.archive.restore_command}")
    return 0


def _handle_show_profile(ws: "Workspace", name: str) -> int:
    from .profile_info import format_report, gather_profile_info
    from .tokens import TokenStoreError

    # A corrupt token entry surfaces as a TokenStoreError from gather_profile_info
    # (it reads token state). Catch it narrowly here so the user sees a clean,
    # actionable message and a nonzero exit, never a Python traceback.
    try:
        report = gather_profile_info(ws, name)
    except TokenStoreError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    # Unknown = no dir on disk, not registered/pinned, and no token entry.
    # "default" (~/.claude) is inspectable like any other profile.
    if not (report.exists or report.registered or report.pinned or report.has_token):
        print(
            f"Profile '{name}' not found: no profile directory, "
            "no options.json registration, no token.",
            file=sys.stderr,
        )
        sys.exit(1)
    for line in format_report(report):
        print(line)
    return 0


def _handle_rename_profile(ws: "Workspace", old: str, new: str) -> int:
    """Rename a profile: validate inputs, then delegate to ProfileStore.rename.

    The charset, name-collision (options + directory), and running checks stay
    here as CLI policy -- they produce clean, targeted messages. The store
    enforces dir-existence and the 'default' reservation as a backstop; its
    ValueErrors are mapped to the same error-print + exit-1 style.
    """
    import re
    from .appdata import OptionsFile
    from .profile_ops import _is_profile_running
    from .profile_store import RESERVED_PROFILE_NAMES

    # Validate old exists
    old_dir = ws.profiles.path_for(old)
    options = OptionsFile(ws.options_file).load({})
    profile_sec = options.get("profile", {})
    registered = old in profile_sec.get("values", []) or old in profile_sec.get(
        "pinned", []
    )
    if not registered and not old_dir.is_dir():
        print(f"Profile '{old}' not found.", file=sys.stderr)
        sys.exit(1)

    # Validate new name charset
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", new):
        print(
            "Invalid name: use lowercase letters, digits, hyphens only "
            "(must start with letter or digit).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate not reserved -- the store owns the set of names it will not take.
    if new in RESERVED_PROFILE_NAMES:
        print(f"Cannot rename to '{new}': reserved name.", file=sys.stderr)
        sys.exit(1)

    # Validate not already taken
    new_dir = ws.profiles.path_for(new)
    if new_dir.exists():
        print(f"Profile '{new}' already exists (directory).", file=sys.stderr)
        sys.exit(1)
    new_in_values = new in profile_sec.get("values", [])
    new_in_pinned = new in profile_sec.get("pinned", [])
    if new_in_values or new_in_pinned:
        print(f"Profile '{new}' already registered in options.", file=sys.stderr)
        sys.exit(1)
    # Check not running
    if _is_profile_running(ws, old):
        print(
            f"Profile '{old}' has a live interactive session. Stop it before renaming.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Perform rename
    try:
        ws.profiles.rename(old, new)  # effects: exempt -- store method, not Path.rename
    except (ValueError, OSError) as e:
        print(f"Rename failed: {e}", file=sys.stderr)
        sys.exit(1)

    verb = "Would rename" if effects.previewing() else "Renamed"
    print(f"{verb} profile '{old}' -> '{new}'.")
    return 0


def _handle_check_tokens(ws: "Workspace") -> int:
    """Validate stored tokens for all discovered profiles against the Anthropic API."""
    from .tokens import TokenStoreError

    from .auth import validate_token, INVALID, UNREACHABLE, INDETERMINATE

    # Each profile carries its own token entry. A corrupt/unreadable one raises
    # TokenStoreError -- catch it narrowly here so the user sees the actionable
    # message and a nonzero exit, never a traceback (mirrors the launch path).
    try:
        profiles = ws.profiles.enumerate()
    except TokenStoreError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not profiles:
        print("No profiles found.")
        return 0

    # Collect results: (name, status, token_display)
    results: list[tuple[str, str, str]] = []
    for p in profiles:
        try:
            token = ws.profiles.data_for(p.name).token()
        except TokenStoreError as e:
            print(str(e), file=sys.stderr)
            return 1
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

    header = (
        f"{'Profile':<{col_name}}  {'Status':<{col_status}}  {'Token':<{col_token}}"
    )
    print(header)
    for name, status, token_display in results:
        print(
            f"{name:<{col_name}}  {status:<{col_status}}  {token_display:<{col_token}}"
        )

    # Exit 1 if any profile has invalid, unreachable, or indeterminate status
    any_bad = any(s in (INVALID, UNREACHABLE, INDETERMINATE) for _, s, _ in results)
    return 1 if any_bad else 0


def _handle_fix_auth(ws: "Workspace", name: str) -> int:
    """Strip session credentials that shadow a profile's long-lived token."""
    from .profile_ops import fix_auth_shadow

    if not ws.profiles.path_for(name).is_dir():
        print(f"No profile '{name}'.", file=sys.stderr)
        sys.exit(1)

    result = fix_auth_shadow(ws, name)

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

    previewing = effects.previewing()
    print(
        f"{'Would remove' if previewing else 'Removed'} session credentials "
        f"from {name}. Long-lived token will now be used."
    )
    return 0


def _handle_set_plan(ws: "Workspace", name: str, plan: str) -> int:
    """Declare which plan a profile's account is on, without prompting.

    The scripted writer of the three: the same closed list the interactive
    picker offers, resolved by key, stored through the same door. It is what a
    headless launch is told to run when the profile declares no plan.
    """
    from .tokens import plan_by_key

    if not ws.profiles.path_for(name).is_dir():
        print(f"No profile '{name}'.", file=sys.stderr)
        sys.exit(1)

    try:
        selected = plan_by_key(plan)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    ws.profiles.data_for(name).set_plan(selected)
    verb = "Would declare" if effects.previewing() else "Declared"
    fields = ", ".join(f"{k}={v}" for k, v in selected.fields().items())
    print(f"{verb} plan {selected.label} for '{name}' ({fields}).")
    return 0


def _handle_show(ws: "Workspace") -> int:
    """Print a summary of current selections, theme, and recent directories."""
    cfg = ws.appconfig()
    rc = _do_show(cfg)
    if rc != 0:
        sys.exit(rc)
    return 0


def _handle_migrate(ws: "Workspace", src: str, dst: str, uuid: str) -> int:
    """Move session data files between profiles, optionally filtered by UUID."""
    from .migrate import migrate_sessions

    uuid_filter = uuid if uuid else None
    try:
        migrate_sessions(
            ws, src, dst, uuid_filter=uuid_filter, dry_run=effects.previewing()
        )
    except (FileNotFoundError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    return 0


def _handle_stats(ws: "Workspace") -> int:
    """Report shared-store statistics and optionally clean up legacy data."""
    from .stats import run_stats

    run_stats(ws.shared, dry_run=effects.previewing())
    return 0


@strictcli.flag(
    "post-hoc",
    type=bool,
    presence="optional",
    help="skip filesystem rename, migrate sessions only (directory already renamed); when omitted, the directory is renamed too",
)
def _handle_mv(ws: "Workspace", old: str, new: str, post_hoc: bool | None) -> int:
    """Rename a project directory and migrate its session data."""
    from .mv import run_mv

    try:
        run_mv(
            ws,
            old,
            new,
            dry_run=effects.previewing(),
            post_hoc=_absent(post_hoc, False),
        )
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    return 0


@strictcli.flag(
    "reid",
    type=bool,
    presence="optional",
    help="assign new UUIDs to sessions that collide with existing local sessions; when omitted, a collision is reported instead",
)
def _handle_import(
    ws: "Workspace",
    source: str,
    from_: list[str],
    to: list[str],
    reid: bool | None,
) -> int:
    """Import session data from an external Claude Code directory."""
    from pathlib import Path
    from .import_ import run_import

    reid = _absent(reid, False)

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
            print(
                f"Error: --to path does not exist or is not a directory: {t}",
                file=sys.stderr,
            )
            return 1
        mappings.append((f, str(resolved)))

    try:
        result = run_import(
            ws.shared, source, mappings, reid=reid, dry_run=effects.previewing()
        )
    except (ValueError, FileNotFoundError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result.collisions and not reid:
        print("Collisions detected (use --reid to assign new UUIDs):")
        for c in result.collisions:
            print(f"  {c}")
        return 1

    return 0


# deploy_scripts reports what it issued in the past tense; a preview only
# recorded those writes, so the narration switches to the conditional form.
_WOULD_DEPLOY = {"created": "would create", "overwritten": "would overwrite"}


@strictcli.flag(
    "all",
    type=bool,
    presence="optional",
    help="deploy every known hook script from the built-in registry at once; when omitted, the positional name selects one script",
)
@strictcli.flag(
    "force-overwrite",
    type=bool,
    presence="optional",
    help="overwrite existing hook scripts on disk instead of skipping them; when omitted, an existing script is left alone",
)
def _handle_deploy_hooks(
    ws: "Workspace", name: str | None, all: bool | None, force_overwrite: bool | None
) -> int:
    """Deploy built-in hook scripts to the scripts directory.

    "Name one script or pass --all" is half a declaration and half a handler
    rule, and the split is the framework's own boundary: the at-least-one half
    is the ``deploy-target`` constraint on the command, while the exclusivity
    half stays here because exactly-one selection is a choice flag and a
    positional arg cannot be a member of one (nor be declared inside a
    choice's scope). Moving it would mean spelling the script name as
    ``--script <name>``, which is not the argv this command has.
    """
    from .hook_scripts import HOOK_SCRIPTS, deploy_scripts

    all = _absent(all, False)
    force_overwrite = _absent(force_overwrite, False)

    if name and all:
        print(
            "Error: --all and a positional name are mutually exclusive", file=sys.stderr
        )
        sys.exit(1)

    if name and name not in HOOK_SCRIPTS:
        known = ", ".join(sorted(HOOK_SCRIPTS))
        print(f"Error: unknown hook script: {name!r} (known: {known})", file=sys.stderr)
        sys.exit(1)

    scripts_dir = ws.scripts_dir
    targets = sorted(HOOK_SCRIPTS) if all else [name or ""]
    previewing = effects.previewing()
    for script_name, action in deploy_scripts(targets, scripts_dir, force_overwrite):
        dest = scripts_dir / script_name
        if action == "exists":
            print(f"already exists: {dest}")
        elif previewing:
            # deploy_scripts reports the past-tense action it issued; under a
            # preview the effects chokepoint only recorded it.
            print(f"{_WOULD_DEPLOY[action]}: {dest}")
        else:
            print(f"{action}: {dest}")

    return 0


def _handle_patch_profiles(ws: "Workspace") -> int:
    """Reconcile every managed profile and shared-settings.json to exact canonical.

    Delegates to the unified reconcile core. This PRUNES each target's guardrail
    sections (the entire hooks structure, the disallowedTools list, and
    permissions deny/ask) to EXACTLY the canonical model, removing drift and any
    user-added extras -- the old additive, extras-preserving semantics are gone.
    Also deploys any missing guardrail hook scripts. The 'default' profile
    (~/.claude) is never read from or written to.

    Declared ``consequential``, like ``reconcile-permissions`` it delegates to:
    the pruning is unrecoverable, so the framework confirms before dispatch and
    refuses outright without a terminal unless ``--approve-consequential`` is
    passed. ``--dry-run`` previews and is never gated.
    """
    from .patch_profiles import run_patch_profiles

    return run_patch_profiles(ws, dry_run=effects.previewing())


@strictcli.flag(
    "profile",
    type=str,
    presence="optional",
    help="reconcile only this single profile; when given, shared-settings.json is left untouched (omit to reconcile every profile AND shared-settings.json)",
)
def _handle_reconcile_permissions(ws: "Workspace", profile: str | None) -> int:
    """Reconcile every managed target to EXACTLY the canonical guardrail model.

    Delegates to the unified reconcile core. Makes each target's hooks, the
    disallowedTools list, and permissions deny/ask EXACTLY canonical (allow keeps
    only its non-conflicting entries), pruning all drift and user-added extras --
    the old additive, extras-preserving behavior is gone. The 'default' profile
    (~/.claude) is never read from or written to.

    The hand-rolled ``--dry-run``/``--apply`` pair this command used to require
    is gone: ``--dry-run`` is now the framework's, and it is the only mode
    flag. The explicit-intent half of that pair is not gone, though -- the
    command declares itself ``consequential``, so the framework confirms before
    dispatch and refuses a bare run on a non-interactive stdin with "pass
    --approve-consequential to confirm". The pruning is exact and nothing
    reconstructs a removed entry, which is what earns the interruption; the
    informative preview is still the per-target diff ``--dry-run`` prints, and
    ``--dry-run`` is never gated.
    """
    from .reconcile import run_reconcile

    return run_reconcile(ws, dry_run=effects.previewing(), profile=profile or None)


# ---------------------------------------------------------------------------
# The profile-target selector
# ---------------------------------------------------------------------------
#
# "One named profile, or every registered profile" is an EXACTLY-ONE selection,
# so it is a member-spelled choice flag rather than the mutex group it used to
# be. The argv the operator types is unchanged (``--profile work`` /
# ``--all-profiles``), and what changes is who owns the rule: the framework
# elects, refuses a double election, refuses a decline that chooses nothing,
# and refuses an invocation that names neither -- the last of which used to be
# a handler check every one of these four commands had to remember to run.
#
# The member flag's own presence is `required`, read as "required once this
# member is elected", and in Python a frozen dataclass field says that by
# construction.


@strictcli.choice("profile", help="target one profile, by name")
class OneProfile:
    """The ``--profile <name>`` member: a single named profile."""

    value: str = strictcli.member_value(
        help="name of the profile to target (e.g. work, personal, research)"
    )


@strictcli.choice("all-profiles", help="target every registered profile at once")
class AllProfiles:
    """The ``--all-profiles`` member: the whole registered fleet."""


#: The selector shared by the four commands that act on profiles. Declared once
#: and applied as a decorator, so the four cannot drift apart.
_profile_target = strictcli.choice_flag(
    "target",
    help="which profiles the operation applies to",
    presence="required",
    elect_by="member-flags",
    choices=[OneProfile, AllProfiles],
)


def one_profile(name: str) -> OneProfile:
    """Construct the named-profile member.

    ``@strictcli.choice`` builds the frozen dataclass at runtime; the decorator
    is not spelled as a ``dataclass_transform``, so a type checker cannot see
    the generated ``__init__``. One typed door here beats an ignore comment at
    every construction site.
    """
    return OneProfile(value=name)  # type: ignore[call-arg]


def _target_selection(target: "OneProfile | AllProfiles") -> tuple[str | None, bool]:
    """Read an elected profile target as the ``(profile, all_profiles)`` pair.

    ``--profile ''`` elects the named-profile member with an empty name --
    electing says WHICH member was named and never that its value is usable --
    so the empty name arrives here as "no profile named", and the callers
    refuse it rather than reading it as "every profile".
    """
    match target:
        case OneProfile(value=name):
            return (name or None, False)
        case AllProfiles():
            return (None, True)
    raise AssertionError(f"unreachable target member: {target!r}")


@_profile_target
def _handle_purge_plugins(ws: "Workspace", target: OneProfile | AllProfiles) -> int:
    """Remove the Claude Code plugin trees from the selected profiles.

    Opt-in and separate from the canonical reconciliation on purpose: that one
    is exact and runs over every managed target, so folding a plugin purge into
    it would delete plugin state on every reconcile, including state somebody
    installed deliberately.

    The vanilla ``default`` profile is never touched -- ``~/.claude`` is Claude
    Code's own directory and claudewheel is read-only to it.

    Naming NEITHER target is now the framework's refusal -- the selector
    declares ``required``, so ``one of --profile, --all-profiles is required``
    comes from the parser. What still belongs here is the empty NAME:
    ``--profile ''`` elects the named-profile member with a name that names no
    profile, and purging every profile off a flag that named none of them is
    exactly the outcome this refusal exists to prevent.
    """
    from .plugins import inventory, purge
    from .profile_info import _format_size

    profile, all_profiles = _target_selection(target)

    if profile:
        reserved = ws.profiles.reserved_reason(profile)
        if reserved is not None:
            print(reserved, file=sys.stderr)
            sys.exit(1)
        targets = [p for p in ws.profiles.enumerate() if p.name == profile]
        if not targets:
            print(f"Error: profile {profile!r} not found", file=sys.stderr)
            sys.exit(1)
    elif not all_profiles:
        print("Error: one of --profile or --all-profiles is required", file=sys.stderr)
        sys.exit(1)
    else:
        targets = [
            p
            for p in ws.profiles.enumerate()
            if ws.profiles.reserved_reason(p.name) is None
        ]
        if not targets:
            print("Error: no profiles found", file=sys.stderr)
            sys.exit(1)

    previewing = effects.previewing()
    purged = 0
    freed = 0
    for entry in targets:
        found = inventory(entry.path)
        if not found.exists:
            print(f"{entry.name}: no plugin tree")
            continue
        detail = []
        if found.marketplaces:
            detail.append(f"marketplaces: {', '.join(found.marketplaces)}")
        if found.plugins:
            detail.append(f"plugins: {', '.join(found.plugins)}")
        verb = "would remove" if previewing else "removed"
        print(f"{entry.name}: {verb} {_format_size(found.size_bytes)}")
        for line in detail:
            print(f"  {line}")
        purge(entry.path)
        purged += 1
        freed += found.size_bytes

    if purged:
        total = _format_size(freed)
        print(
            f"{'Would free' if previewing else 'Freed'} {total} "
            f"across {purged} profile(s)."
        )
    else:
        print("Nothing to purge.")
    return 0


@_profile_target
def _handle_permission_add(
    ws: "Workspace", category: str, rule: str, target: OneProfile | AllProfiles
) -> int:
    """Add a permission rule to the specified category for one or all profiles."""
    from .permission import (
        validate_rule,
        resolve_profiles,
        load_settings,
        add_rule,
        save_settings,
    )

    valid_categories = ("allow", "deny", "ask")
    if category not in valid_categories:
        print(
            f"Error: category must be one of {', '.join(valid_categories)}, got {category!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        validate_rule(rule)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    previewing = effects.previewing()
    targets = resolve_profiles(ws, *_target_selection(target))
    for name, settings_path in targets:
        data = load_settings(settings_path)
        result = add_rule(data, category, rule)
        save_settings(settings_path, data)
        if result == "added":
            verb = "would add" if previewing else "added"
            print(f"{name}: {verb} {rule} to {category}")
        else:
            print(f"{name}: already in {category}")
    return 0


@_profile_target
def _handle_permission_remove(
    ws: "Workspace", category: str, rule: str, target: OneProfile | AllProfiles
) -> int:
    """Remove a permission rule from the specified category for one or all profiles."""
    from .permission import resolve_profiles, load_settings, remove_rule, save_settings

    valid_categories = ("allow", "deny", "ask")
    if category not in valid_categories:
        print(
            f"Error: category must be one of {', '.join(valid_categories)}, got {category!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not rule.strip():
        print("Error: rule must not be empty", file=sys.stderr)
        sys.exit(1)

    previewing = effects.previewing()
    targets = resolve_profiles(ws, *_target_selection(target))
    for name, settings_path in targets:
        data = load_settings(settings_path)
        result = remove_rule(data, category, rule)
        if result == "removed":
            save_settings(settings_path, data)
            verb = "would remove" if previewing else "removed"
            print(f"{name}: {verb} {rule} from {category}")
        else:
            print(f"{name}: not found in {category}")
    return 0


#: The shape ``permission list`` answers a machine with.  Closed at every
#: level: an object of profiles, each a name and its permission categories.
#: ``permissions`` names no required member because ``--category`` narrows it
#: to the single category asked for -- the closure is what says an unexpected
#: key never appears, not that all three always do.
_PERMISSION_LIST_PAYLOAD_SCHEMA = strictcli.schema_object(
    properties={
        "profiles": strictcli.schema_array(
            strictcli.schema_object(
                properties={
                    "name": strictcli.schema_type("string"),
                    "permissions": strictcli.schema_object(
                        properties={
                            "allow": strictcli.schema_array(
                                strictcli.schema_type("string")
                            ),
                            "deny": strictcli.schema_array(
                                strictcli.schema_type("string")
                            ),
                            "ask": strictcli.schema_array(
                                strictcli.schema_type("string")
                            ),
                        },
                        additional_properties=False,
                    ),
                },
                required=["name", "permissions"],
                additional_properties=False,
            )
        ),
    },
    required=["profiles"],
    additional_properties=False,
)


@strictcli.flag(
    "format",
    type=str,
    presence="required",
    help="output format: grouped (indented tree) or flat (tsv)",
    choices=[
        Choice("grouped", help="one indented block per category, one rule per line"),
        Choice("flat", help="one tab-separated category-and-rule pair per line"),
    ],
)
@strictcli.flag(
    "category",
    type=str,
    presence="optional",
    help="restrict output to a single permission category (allow, deny, or ask)",
)
@_profile_target
def _handle_permission_list(
    ws: "Workspace",
    target: OneProfile | AllProfiles,
    format: str,
    category: str | None,
) -> int:
    """List permission rules for one or all profiles in the chosen format.

    ``--format`` chooses between the two HUMAN renderings and nothing else.
    The machine form is the framework-owned ``--json``: the whole answer --
    every target, not one document per target -- goes into the single payload
    slot, validated against :data:`_PERMISSION_LIST_PAYLOAD_SCHEMA`.  The
    payload is built on every run and the framework decides whether it becomes
    a document, so there is no mode branch here.

    The human lines go through :func:`claudewheel.effects.info` rather than
    ``print``, which is what keeps the envelope the sole document on stdout
    when a machine is reading.
    """
    from .permission import resolve_profiles, load_settings

    valid_categories = ("allow", "deny", "ask")
    if category and category not in valid_categories:
        print(
            f"Error: category must be one of {', '.join(valid_categories)}, got {category!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    targets = resolve_profiles(ws, *_target_selection(target))
    multi = len(targets) > 1
    answer: list[dict[str, Any]] = []

    for i, (name, settings_path) in enumerate(targets):
        data = load_settings(settings_path)
        perms = data.get("permissions", {})

        if category:
            subset = {category: perms.get(category, [])}
        else:
            subset = {c: perms.get(c, []) for c in ("allow", "deny", "ask")}
        answer.append({"name": name, "permissions": subset})

        if multi:
            if i > 0:
                effects.info("")
            effects.info(f"[{name}]")

        if format == "grouped":
            for cat, rules in subset.items():
                effects.info(f"  {cat}:")
                if rules:
                    for r in rules:
                        effects.info(f"    {r}")
                else:
                    effects.info("    (none)")
        elif format == "flat":
            for cat, rules in subset.items():
                for r in rules:
                    effects.info(f"{cat}\t{r}")

    effects.payload({"profiles": answer})
    return 0


# A canonical UUID (Claude Code session id). Anything matching this is used
# verbatim as a --resume value; anything else is treated as a session title.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _resolve_resume_title(ws: "Workspace", resume_val: str, directory: str) -> str:
    """Resolve a ``--resume`` argument to a session UUID.

    If *resume_val* is UUID-shaped it is returned unchanged. Otherwise it is
    treated as a session title (Claude Code accepts either). Titles are resolved
    by scanning the current directory's project dir first, then all project
    dirs. Exactly one match rewrites the value to that session's UUID and the
    caller proceeds through the normal UUID machinery. Zero or multiple matches
    print guidance and exit nonzero.
    """
    if _UUID_RE.match(resume_val):
        return resume_val

    from datetime import datetime

    from .session import find_sessions_by_title

    store = ws.shared
    projects_dir = store.projects_dir
    encoded_cwd = store.encode_path(os.path.abspath(directory))
    target_project_dir = projects_dir / encoded_cwd

    matches = find_sessions_by_title(resume_val, [target_project_dir])
    if not matches:
        all_dirs = (
            sorted(p for p in projects_dir.iterdir() if p.is_dir())
            if projects_dir.is_dir()
            else []
        )
        matches = find_sessions_by_title(resume_val, all_dirs)

    if len(matches) == 1:
        return matches[0].session_id

    if not matches:
        print(
            f"No session titled {resume_val!r} was found in any project "
            "directory.\nTry --picker to browse available sessions.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Multiple sessions match the title {resume_val!r}:",
        file=sys.stderr,
    )
    for m in sorted(matches, key=lambda x: x.mtime, reverse=True):
        ts = datetime.fromtimestamp(m.mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {m.session_id}  {m.project_dir.name}  {ts}", file=sys.stderr)
    print(
        "Resume by UUID (the first column above) to disambiguate.",
        file=sys.stderr,
    )
    sys.exit(1)


def _check_resume_session(ws: "Workspace", session_id: str, directory: str) -> None:
    """Intercept --resume to detect and offer to fix directory renames.

    When a session exists under an old encoded path (because the project
    directory was renamed), this function detects the mismatch and offers
    to move all sessions to the new path via ``run_mv``.

    Returns normally when no interception is needed (session found under
    current directory, or sessions successfully moved).  Calls ``sys.exit(1)``
    when the session cannot be resumed from here.
    """
    from .session import find_session

    store = ws.shared

    # Step 1: Check if session exists under the current directory
    encoded_cwd = store.encode_path(os.path.abspath(directory))
    expected_path = store.projects_dir / encoded_cwd / f"{session_id}.jsonl"
    if expected_path.exists():
        return  # Claude Code will find it

    # Step 2: Search the entire shared store
    info = find_session(session_id, store.projects_dir)
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
    jsonl_files = (
        list(old_project_dir.glob("*.jsonl")) if old_project_dir.is_dir() else []
    )
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

    result = run_mv(ws, old_cwd, current_dir, dry_run=True, quiet=True, post_hoc=True)
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
    result = run_mv(ws, old_cwd, current_dir, dry_run=False, quiet=True, post_hoc=True)
    print("Done. Resuming session...")


def _check_cont_session(ws: "Workspace", directory: str) -> None:
    """Intercept --cont to detect and offer to fix directory renames.

    When the current directory has no sessions but an orphaned project
    directory exists under the same parent (original cwd no longer on
    disk), this function offers to move those sessions to the current
    directory via ``run_mv``.
    """
    from .session import find_orphaned_project_dirs

    store = ws.shared
    current_dir = os.path.abspath(directory)

    # Step 1: Check if sessions exist under the current directory
    encoded_cwd = store.encode_path(current_dir)
    project_dir = store.projects_dir / encoded_cwd
    if project_dir.is_dir() and list(project_dir.glob("*.jsonl")):
        return  # Claude Code will find sessions

    # Step 2: Scan all project dirs for orphans (cwd no longer on disk)
    candidates = find_orphaned_project_dirs(store.projects_dir)

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
            print(
                f"  {i}. {orphan.cwd} ({orphan.session_count} sessions, {size_mb:.1f} MB)"
            )
        print(
            f"Move sessions from which directory? [1-{len(candidates)}/n to skip] ",
            end="",
            flush=True,
        )
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

    result = run_mv(ws, old_cwd, current_dir, dry_run=True, quiet=True, post_hoc=True)
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

    result = run_mv(ws, old_cwd, current_dir, dry_run=False, quiet=True, post_hoc=True)
    print("Done. Resuming session...")


# "continue" and "print" are Python keywords, so we use "cont" / "print-prompt"
# as flag names. Short forms -c and -p remain the same for user convenience.
# Explicit segment overrides that contradict a non-claude client. Maps segment
# key -> predicate over the override value: True means the value is claude-only.
# version: any value (it names a claudewheel-managed claude binary).
# mcp: only "strict" (it maps to claude's --strict-mcp-config; "default" is a no-op).
_CLAUDE_ONLY_OVERRIDES: dict[str, Callable[[str], bool]] = {
    "version": lambda v: True,
    "mcp": lambda v: v == "strict",
}


def _reject_claude_only_overrides(
    client_val: str, segment_overrides: dict[str, Any]
) -> None:
    """Hard-error on explicit claude-only overrides combined with a non-claude client.

    ``version`` and ``mcp=strict`` are claude-client-only inputs. An *ambient*
    value (remembered in last_config or a config default) is silently ignored
    for non-claude clients; but an explicit, same-invocation override (a
    segment flag or ``-s key=value``) alongside a non-claude ``--client`` is
    contradictory intent and is rejected here, where the selection's provenance
    (an explicit override) is known -- the adapter downstream cannot tell
    explicit from ambient.
    """
    if client_val == "claude":
        return
    for key, is_claude_only in _CLAUDE_ONLY_OVERRIDES.items():
        if key in segment_overrides and is_claude_only(segment_overrides[key]):
            print(
                f"Error: explicit {key} selection {segment_overrides[key]!r} "
                f"is claude-client-only and cannot be combined with "
                f"--client {client_val}",
                file=sys.stderr,
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# The session selector
# ---------------------------------------------------------------------------
#
# A launch either continues, resumes, prints, picks, or starts a new session --
# exactly one of five, so it is a member-spelled choice flag. The five used to
# be four independent flags plus a counted "mutually exclusive" refusal in the
# handler, with two of them carrying a private "\x00__unset__" string so an
# absent flag could be told from an explicitly empty one.
#
# The fifth member is the one the old shape could not express. An at-most-one
# rule over four flags is an exactly-one rule whose fifth alternative -- plain
# launch, the thing that happens when nothing is typed -- has no name. Naming
# it makes `--new-session` a real spelling and makes the default an election
# like any other.


@strictcli.choice(
    "cont", help="continue the most recent conversation in the current directory"
)
class ContinueSession:
    """The ``--cont`` member: Claude Code's ``--continue``."""


@strictcli.choice("resume", help="resume one specific session")
class ResumeSession:
    """The ``--resume <session>`` member: Claude Code's ``--resume <id>``."""

    value: str = strictcli.member_value(
        help=(
            "session to resume, by UUID or by title; an empty string opens "
            "Claude Code's own picker"
        )
    )


@strictcli.choice(
    "print-prompt", help="run one prompt in non-interactive print mode and exit"
)
class PrintPrompt:
    """The ``--print-prompt <prompt>`` member: Claude Code's ``--print``."""

    value: str = strictcli.member_value(help="the prompt to run non-interactively")


@strictcli.choice(
    "picker", help="browse this profile's sessions and pick one to resume"
)
class SessionPicker:
    """The ``--picker`` member: claudewheel's own session picker screen."""


@strictcli.choice("new-session", help="start a new session (what a bare launch does)")
class NewSession:
    """The default member: no session is continued, resumed or printed."""


#: The launch command's session selection. Declaring a default makes plain
#: launch a named member rather than the absence of four flags.
_launch_session = strictcli.choice_flag(
    "session",
    help="which session this launch starts in",
    default=NewSession(),
    elect_by="member-flags",
    choices=[ContinueSession, ResumeSession, PrintPrompt, SessionPicker, NewSession],
)


@_launch_session
def _handle_launch(
    ws: "Workspace",
    locator: "BinaryLocator",
    # The session selection: exactly one of the five members, elected by its
    # own flag. Nothing elected is `NewSession`, the selector's declared
    # default -- the launch this command has always performed when no session
    # flag was typed, now named instead of inferred from four absences.
    session: ContinueSession | ResumeSession | PrintPrompt | SessionPicker | NewSession,
    # Segment flags (via tag); absent means "not provided"
    profile: str | None,
    github: str | None,
    model: str | None,
    directory: str | None,
    mcp: str | None,
    permissions: str | None,
    # Repeatable set flag (via tag)
    set: list[str],
    # Launch target adapter (via tag). None means "--client not passed": the
    # interactive launcher prompts (Client step) and non-interactive launches
    # fall back to config default_client. A value means explicit -> skip the step.
    client: str | None,
) -> int:
    """Handle the launch subcommand: run the TUI or skip it when args suffice."""
    # The elected member IS the session decision; these four locals are the
    # shape the rest of the handler reads it in. The exclusivity that used to
    # be counted here is the selector's, and unreachable states (a resume and
    # a print prompt at once) no longer exist to check for.
    cont = isinstance(session, ContinueSession)
    picker = isinstance(session, SessionPicker)
    resume_val: str | None = (
        session.value if isinstance(session, ResumeSession) else None
    )
    print_prompt_val: str | None = (
        session.value if isinstance(session, PrintPrompt) else None
    )

    # --client: absent means "not passed". Validate an explicit value against
    # the registry (the registry is built from the client adapters, so the
    # check lives here rather than in a `choices=` list that would have to
    # restate it).
    if client is not None and client not in CLIENT_NAMES:
        print(
            f"Error: unknown client {client!r}; known: {', '.join(CLIENT_NAMES)}",
            file=sys.stderr,
        )
        sys.exit(1)

    from .app import App as TuiApp

    cfg = ws.appconfig()
    enabled = cfg.config.get("enabled_segments", [])
    segment_keys = [s["key"] for s in cfg.segments_def if s["key"] in enabled]

    # Collect segment value overrides from individual flags.
    # Empty string means "not provided" (strictcli default for optional str flags).
    segment_overrides: dict[str, str] = {}
    segment_sources: dict[str, str] = {}
    flag_values = {
        "profile": profile,
        "github": github,
        "model": model,
        "directory": directory,
        "mcp": mcp,
        "permissions": permissions,
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
            print(
                f"Unknown segment: {key!r} (available: {', '.join(segment_keys)})",
                file=sys.stderr,
            )
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

    # Resolve a title-based --resume value to a session UUID before it is
    # baked into the launch flags. Claude Code's --resume accepts either a UUID
    # or a session title; resolving titles here keeps the downstream
    # rename-repair machinery (which is UUID-based) working uniformly.
    if resume_val:
        target_dir = segment_overrides.get("directory", os.getcwd())
        resume_val = _resolve_resume_title(ws, resume_val, target_dir)

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
        _check_resume_session(ws, resume_val, target_dir)
    if cont:
        _check_cont_session(ws, segment_overrides.get("directory", os.getcwd()))

    # Skip TUI when args cover every required segment, or when print mode is active.
    required_keys = {
        s["key"]
        for s in cfg.segments_def
        if s["key"] in enabled and s.get("required", False)
    }
    skip_tui = print_prompt_val is not None or (
        required_keys and all(k in segment_overrides for k in required_keys)
    )

    # Resolve the configured default client once, up front, so an unknown
    # default_client fails loudly (hard error) before any launch work -- never a
    # silent fallback. Explicit --client overrides it downstream.
    try:
        resolved_default_client = resolve_default_client(cfg.config)
    except ValueError as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)

    # A corrupt token entry surfaces as a TokenStoreError from the launch
    # sequence. Catch it narrowly at this handler boundary so the user sees a
    # clean, actionable message instead of a Python traceback.
    from .tokens import TokenStoreError

    try:
        if skip_tui:
            # Non-interactive: explicit --client wins, else the config default.
            # No prompting (mirrors how non-interactive segment values come from
            # last_config/flags without a TUI).
            client_val = client if client is not None else resolved_default_client
            _reject_claude_only_overrides(client_val, segment_overrides)
            merged = dict(cfg.state.get("last_config", {}))
            merged.update(segment_overrides)
            # Drop ambient (remembered/default) claude-only values for
            # non-claude clients: they must not reach the adapter. Explicit
            # contradictory overrides were already rejected above.
            if client_val != "claude":
                for _key in _CLAUDE_ONLY_OVERRIDES:
                    merged.pop(_key, None)
            if print_prompt_val is not None:
                print_keys = {
                    s["key"]
                    for s in cfg.segments_def
                    if s["key"] in enabled and s.get("print_mode", True)
                }
                merged = {k: v for k, v in merged.items() if k in print_keys}
                missing = [k for k in required_keys & print_keys if not merged.get(k)]
                if missing:
                    print(
                        f"Warning: required segments not set: {', '.join(sorted(missing))}; "
                        "using fallback defaults. Use --<segment> flags or run the TUI first "
                        "to populate last_config.",
                        file=sys.stderr,
                    )
            _do_launch_sequence(
                ws,
                locator,
                cfg,
                merged,
                extra_flags=extra_flags,
                interactive=_launch_is_interactive(print_prompt_val),
                client=client_val,
                passthrough=list(_passthrough),
            )
            return 0

        # Explicit --client is known up front; reject contradictory explicit
        # claude-only overrides before the TUI even opens.
        if client is not None:
            _reject_claude_only_overrides(client, segment_overrides)

        # Otherwise show the TUI (pre-filled from last_config + arg overrides).
        # The app runs the Client step first (unless --client was explicit),
        # then the segment bar; it drops the version segment for non-claude
        # clients.
        app = TuiApp(
            ws,
            cfg=cfg,
            overrides=segment_overrides,
            locator=locator,
            explicit_client=client,
            default_client=resolved_default_client,
            clients_config=cfg.config.get("clients", {}),
        )
        selections = app.run_tui()
        if selections is None:
            return 0

        client_val = app.selected_client
        # A client picked in the TUI can still collide with an explicit
        # claude-only override (e.g. -s version=...); reject that
        # contradictory intent.
        _reject_claude_only_overrides(client_val, segment_overrides)

        # Extract per-segment metadata from the bar for resolve_launch_config
        bar_metadata: dict[str, dict[str, dict[str, Any]]] = {}
        for seg in app.bar.segments:
            if seg.state.metadata:
                bar_metadata[seg.key] = seg.state.metadata

        _do_launch_sequence(
            ws,
            locator,
            app.cfg,
            selections,
            extra_flags=extra_flags,
            metadata=bar_metadata or None,
            client=client_val,
            passthrough=list(_passthrough),
        )
        return 0
    except TokenStoreError as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand names for routing
# ---------------------------------------------------------------------------
_SUBCOMMANDS = frozenset(
    {
        "health",
        "config",
        "versions",
        "install",
        "uninstall",
        "reset-options",
        "show",
        "migrate",
        "stats",
        "mv",
        "import",
        "deploy-hooks",
        "patch-profiles",
        "reconcile-permissions",
        "purge-plugins",
        "launch",
        "permission",
        "profile",
        # Deprecated top-level names kept here so main() doesn't rewrite
        # e.g. "c new-profile" to "c launch new-profile" before the
        # deprecation handler can fire.
        "new-profile",
        "delete-profile",
        "show-profile",
    }
)

# Flags that must be handled at the app level rather than routed to the
# "launch" subcommand. --help/--version show the app-wide help/version, and
# --dump-schema is a strictcli reserved flag that dumps the CLI schema.
_APP_LEVEL_FLAGS = frozenset({"--help", "-h", "--version", "-v", "--dump-schema"})

# strictcli owns these four names and strips them from argv before any command
# parsing (the effects contract, §7). They may appear anywhere, including in
# front of the command token, so the launch injection has to step over them --
# otherwise `c --dry-run stats` would be rewritten to `c launch --dry-run stats`
# and preview the TUI instead of the stats cleanup.
_RESERVED_QUARTET = frozenset(
    {"--dry-run", "--approve-consequential", "--quiet", "--verbose"}
)


def _inject_launch(argv: list[str]) -> list[str]:
    """Return argv with the "launch" subcommand injected when appropriate.

    argv includes argv[0] (the program name). When no subcommand is given, or
    the first token that is not a framework-reserved global flag is neither a
    known subcommand nor an app-level flag, the "launch" subcommand is injected
    at that position so the interactive TUI starts. App-level flags (see
    _APP_LEVEL_FLAGS) and known subcommands are left untouched.
    """
    rest = argv[1:]
    lead = 0
    while lead < len(rest) and rest[lead] in _RESERVED_QUARTET:
        lead += 1
    tail = rest[lead:]
    if not tail or (tail[0] not in _SUBCOMMANDS and tail[0] not in _APP_LEVEL_FLAGS):
        return [argv[0]] + rest[:lead] + ["launch"] + tail
    return list(argv)


def _plan_choices() -> list[Choice]:
    """The declarable plans, as `profile set-plan`'s argument choices.

    Constrained at parse time from the same closed list the interactive picker
    renders, so the scripted surface cannot accept a plan the picker has no
    entry for. Each entry carries the help strictcli requires a choice record
    to be able to carry, and that help is derived from the same ``PlanTier``
    the value comes from -- so a plan's label and the fields it stores cannot
    drift from the value that writes them.
    """
    from .tokens import RATE_LIMIT_FIELD, SUBSCRIPTION_FIELD, PLAN_TIERS

    entries: list[Choice] = []
    for plan in PLAN_TIERS:
        if plan.rate_limit_tier is None:
            stores = (
                f"{SUBSCRIPTION_FIELD}={plan.subscription_type}, no rate-limit tier"
            )
        else:
            stores = (
                f"{SUBSCRIPTION_FIELD}={plan.subscription_type}, "
                f"{RATE_LIMIT_FIELD}={plan.rate_limit_tier}"
            )
        entries.append(Choice(plan.key, help=f"{plan.label} ({stores})"))
    return entries


def _bind(handler: Callable[..., int], *pre: Any) -> Callable[..., int]:
    """Pre-bind leading positional dependencies (workspace/locator) to a handler.

    strictcli dispatches handlers with keyword arguments (``handler(ctx,
    **parsed)``) and builds the schema from the declared Flag/Arg objects --
    NOT from the handler signature. The signature is what strictcli's guard v2
    validates the declaration against. So the returned wrapper:

    - forwards the pre-bound deps plus parsed kwargs to the real handler,
    - binds the dispatch context to :mod:`claudewheel.effects` for the length
      of the call, which is what makes ``--dry-run`` record every mutation
      instead of performing it,
    - carries an explicit ``__signature__``: the real handler's parameters with
      the pre-bound positionals dropped and the framework's context slot put
      back in front.

    That ``__signature__`` is the point. The wrapper is physically ``(ctx,
    **kwargs)``, and a bare ``**kwargs`` handler is exactly the hole strictcli's
    guard v2 closes -- it would have to declare ``forwarding=`` and waive the
    signature cross-check for all 24 commands. Presenting the wrapped
    handler's real signature instead means every flag and arg is validated
    against a real parameter, which is the "declare everything" guarantee this
    wrapper used to opt out of.

    We deliberately do NOT use ``functools.wraps``: it would set
    ``__wrapped__``, and ``inspect.signature`` follows that chain back to the
    real (ws-bearing) signature, re-triggering validation against parameters
    the framework never supplies.
    """

    def wrapper(ctx: strictcli.Context, **kwargs: Any) -> int:
        with effects.bound(ctx):
            return handler(*pre, **kwargs)

    params = list(inspect.signature(handler).parameters.values())[len(pre) :]
    wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD)] + params
    )
    # The annotations of the same parameters, for the same reason. A choice
    # flag's handler parameter must be annotated with the union of its choice
    # classes, and strictcli reads that off `__annotations__` (resolving the
    # strings this module's `from __future__ import annotations` produces).
    # The pre-bound parameters are dropped here as well: `ws: "Workspace"` is
    # a TYPE_CHECKING-only name, and one unresolvable entry would make the
    # whole resolution fail.
    wrapper.__annotations__ = {
        p.name: p.annotation
        for p in params
        if p.annotation is not inspect.Parameter.empty
    }
    # strictcli reads these attributes off the callable to build the schema.
    setattr(wrapper, "_strictcli_flags", getattr(handler, "_strictcli_flags", []))
    setattr(wrapper, "_strictcli_args", getattr(handler, "_strictcli_args", []))
    # tests/test_effects_binding.py walks the registered commands for this.
    setattr(wrapper, "__claudewheel_effects_bound__", True)
    return wrapper


def _build_app(ws: "Workspace", locator: "BinaryLocator") -> App:
    """Build the strictcli App with all subcommands registered."""
    app = App(
        name="c",
        version=__version__,
        help="claudewheel - TUI launcher for Claude Code with profile, model, and directory selection",
    )

    # -- One-shot commands --

    app.command(
        "health",
        effect="read_only",
        help="run diagnostic health checks on profiles, tokens, and hooks, then exit",
    )(_bind(_handle_health, ws))

    app.command(
        "config",
        effect="mutating",
        help="open the ~/.claudewheel/ config directory in your $EDITOR",
    )(_bind(_handle_config, ws))

    app.command(
        "versions",
        effect="read_only",
        help="list all installed Claude Code versions, marking the current symlink target",
    )(_bind(_handle_versions, locator))

    app.command(
        "install",
        effect="mutating",
        grants=[
            Grant(
                "download",
                "installs an executable fetched from the Claude Code release bucket",
                strictcli.NET_MUTATE,
            )
        ],
        help="download and install a specific Claude Code version",
        args=[
            Arg(
                name="version",
                presence="required",
                help="semver version string to download and install (e.g. 2.1.119)",
            )
        ],
    )(_bind(_handle_install, locator))

    app.command(
        "uninstall",
        effect="mutating",
        help="delete an installed Claude Code version binary from the versions directory",
        args=[
            Arg(
                name="version",
                presence="required",
                help="semver version string to remove (refuses if it is the current symlink target)",
            )
        ],
    )(_bind(_handle_uninstall, locator))

    app.command(
        "reset-options",
        effect="mutating",
        help="delete options.json so it regenerates from defaults",
    )(_bind(_handle_reset_options, ws))

    # -- Profile group --
    profile_grp = app.group(
        "profile",
        help="create, inspect, rename, delete, and manage Claude Code profiles and their stored tokens",
    )

    profile_grp.command(
        "create",
        effect="mutating",
        grants=[
            Grant(
                "auth-login",
                "the wizard drives an interactive Claude Code OAuth login for the new profile",
                strictcli.PROC_MUTATE,
            )
        ],
        help="run the create-profile wizard in one continuous alt-screen session: prompt for the profile name, config directory and launch options, write the profile directory together with its symlinks into the shared store, then drive an interactive Claude Code OAuth login so the profile is authenticated before you leave. Requires a real terminal, and prints the summary and auth outcome afterwards",
    )(_bind(_handle_new_profile, ws, locator))

    profile_grp.command(
        "delete",
        effect="mutating",
        # One of claudewheel's three consequential commands (contract §8.1),
        # alongside reconcile-permissions and patch-profiles. The archival
        # makes it recoverable, not harmless: the profile stops existing, every
        # process holding it loses its configuration directory, and putting it
        # back is a second deliberate operation somebody has to know to run.
        # --force-delete-data widens what goes into the archive from
        # "credentials and settings" to "credentials, settings and conversation
        # history", so there is no quiet invocation to keep quiet about and the
        # command -- not the flag -- is the right granularity.
        consequential=True,
        grants=[
            Grant(
                "archive-delegation",
                "hands the whole profile directory, its stored OAuth token included, to saferm, which archives it and then removes it",
                strictcli.PROC_MUTATE,
            )
        ],
        help="remove a registered profile: hand its directory to saferm, which archives it (the stored token among it) and then removes it, unlink its shared-store symlinks, drop its options.json registration, and clear any last_config reference in state.json. Prints the archive handle that restores it. Refuses a profile holding a live interactive Claude Code session unless --force-delete (background jobs and daemons do not block it), and takes conversation history only with --force-delete-data. saferm must be installed: without it the deletion would be irreversible, so it is refused rather than performed",
        args=[
            Arg(
                name="name",
                presence="required",
                help="name of the profile to delete (e.g. work, personal, research)",
            )
        ],
    )(_bind(_handle_delete_profile, ws))

    profile_grp.command(
        "show",
        effect="read_only",
        help="print a detailed report for one profile: whether its directory exists on disk, whether it is registered or pinned in options.json, the state of its stored token, its resolved configuration and the session data it holds. Inspects default (~/.claude) like any other profile, and exits non-zero when the name matches no directory, registration or token",
        args=[
            Arg(
                name="name",
                presence="required",
                help="name of the profile to inspect (e.g. work, personal, default)",
            )
        ],
    )(_bind(_handle_show_profile, ws))

    profile_grp.command(
        "rename",
        effect="mutating",
        help="move a profile to a new name, taking its directory (with the token stored inside it), its options.json registration and its session data with it. Validates that the old name exists, that the new one is free in both the directory tree and the options file, and that it fits the lowercase-letters-digits-hyphens charset. Refuses a profile holding a live interactive Claude Code session, and the reserved name default",
        args=[
            Arg(
                name="old",
                presence="required",
                help="current name of the profile to rename (must be an existing, non-running profile)",
            ),
            Arg(
                name="new",
                presence="required",
                help="new name for the profile (lowercase letters, digits, and hyphens; must be unused)",
            ),
        ],
    )(_bind(_handle_rename_profile, ws))

    profile_grp.command(
        "fix-auth",
        effect="mutating",
        help="repair one profile's authentication: strip the session credentials that shadow its stored long-lived token so the token is used again. Says so plainly when there is nothing to repair, and refuses a name with no profile directory behind it",
        args=[
            Arg(
                name="name",
                presence="required",
                help="name of the profile to repair: its shadowing session credentials are removed",
            )
        ],
    )(_bind(_handle_fix_auth, ws))

    # Judged against strictcli's update-command construct and deliberately not
    # declared as one. `set-plan` does write one property of one instance --
    # the plan fields of a profile's token entry -- but an update command's
    # properties must be FLAGS declaring `optional`, where absence means
    # untouched. This command's plan is a required POSITIONAL, and the whole of
    # what it does is write it: converting it would respell `c profile set-plan
    # work pro` as `--plan pro`, and would turn a mandatory value into an
    # optional one that the framework's at-least-one-property rule then refuses
    # at parse time. The declaration would publish a write set nothing here
    # reads, at the cost of the argv and of a value that cannot be omitted.
    profile_grp.command(
        "set-plan",
        effect="mutating",
        help=(
            "declare which plan a profile's Claude account is on, without a "
            "prompt. Claude Code resolves its subscription tier from the launch "
            "environment and only from there when auth is a stored setup token, "
            "so an undeclared profile launches with the tier null and "
            "tier-dependent features failing closed. Writes both plan fields "
            "into the profile's token entry, leaving the token itself alone; "
            "the interactive picker in the create flow and the pre-launch "
            "prompt write exactly the same thing"
        ),
        args=[
            Arg(
                name="name",
                presence="required",
                help="name of the profile to declare a plan for (e.g. work, personal)",
            ),
            Arg(
                name="plan",
                presence="required",
                help=(
                    "the plan this profile's Claude account is on; each one "
                    "stores a subscription type and, where Claude Code has one, "
                    "a rate-limit tier"
                ),
                choices=_plan_choices(),
            ),
        ],
    )(_bind(_handle_set_plan, ws))

    profile_grp.command(
        "check-tokens",
        effect="read_only",
        help="read every discovered profile's own stored OAuth token and validate each one against the Anthropic API, then print a table of profile name, status and a truncated token preview. The status distinguishes a valid token from an invalid one, an unreachable API and an indeterminate answer, and profiles holding no token are listed too",
    )(_bind(_handle_check_tokens, ws))

    # Hard-break old top-level names so they fail loudly with migration guidance
    app.deprecate(
        "new-profile", message="Renamed: use 'claudewheel profile create' instead."
    )
    app.deprecate(
        "delete-profile",
        message="Renamed: use 'claudewheel profile delete <name>' instead.",
    )
    app.deprecate(
        "show-profile",
        message="Renamed: use 'claudewheel profile show <name>' instead.",
    )

    app.command(
        "show",
        effect="read_only",
        help="print a summary of current segment selections, theme, and recent directories",
    )(_bind(_handle_show, ws))

    app.command(
        "migrate",
        effect="mutating",
        help="move session data files from one profile to another, optionally filtered by UUID",
        args=[
            Arg(
                name="src",
                presence="required",
                help="source profile name whose sessions will be moved (e.g. work)",
            ),
            Arg(
                name="dst",
                presence="required",
                help="destination profile name to receive the migrated sessions (e.g. personal)",
            ),
            Arg(
                name="uuid",
                presence="optional",
                help="UUID substring to migrate only matching sessions; when omitted, every session moves",
            ),
        ],
    )(_bind(_handle_migrate, ws))

    app.command(
        "stats",
        effect="mutating",
        help="report shared-store stats and clean up legacy data",
    )(_bind(_handle_stats, ws))

    app.command(
        "mv",
        effect="mutating",
        help="rename a project directory and migrate session data",
        args=[
            Arg(
                name="old",
                presence="required",
                help="current path of the project directory to rename (absolute or relative)",
            ),
            Arg(
                name="new",
                presence="required",
                help="target path for the renamed project directory (absolute or relative)",
            ),
        ],
    )(_bind(_handle_mv, ws))

    app.command(
        "import",
        effect="mutating",
        help="import session data from an external Claude Code directory",
        args=[
            Arg(
                name="source",
                presence="required",
                help="path to the source directory (e.g., /path/to/backup/.claude)",
            ),
        ],
        flag_sets=[
            FlagSet(
                name="mapping",
                flags=[
                    # A declared empty default, which the mutating-default ban
                    # leaves legal: an empty collection declares NO elements,
                    # so nothing the invocation did not state can reach a write
                    # through it. The handler zips the two lists positionally.
                    Flag(
                        name="from",
                        type=str,
                        repeatable=True,
                        unique=False,
                        default=[],
                        help="original project path as recorded in the source session data (repeatable)",
                    ),
                    Flag(
                        name="to",
                        type=str,
                        repeatable=True,
                        unique=False,
                        default=[],
                        help="local directory path that corresponds to the --from path on this machine (repeatable)",
                    ),
                ],
            ),
        ],
        # A path mapping is a pair: neither half means anything alone.
        constraints=[
            AllOrNone(
                "path-mapping",
                (Member("from"), Member("to")),
            ),
        ],
    )(_bind(_handle_import, ws))

    app.command(
        "deploy-hooks",
        effect="mutating",
        help="deploy built-in hook scripts to the ~/.claudewheel/scripts/ directory",
        args=[
            Arg(
                name="name",
                presence="optional",
                help="name of the specific hook script to deploy (omit to use --all)",
            )
        ],
        # The at-least-one half of "name one script or pass --all". The
        # exclusivity half stays in the handler: exactly-one selection is a
        # choice flag, and a positional arg cannot be a member of one.
        constraints=[
            strictcli.AtLeastOne(
                "deploy-target",
                (
                    Member("name", when="non_empty"),
                    Member("all", when="true"),
                ),
            ),
        ],
    )(_bind(_handle_deploy_hooks, ws))

    # Both spellings of the exact reconciliation are consequential (contract
    # §8.1). One bare invocation rewrites every managed profile's settings.json
    # plus shared-settings.json, pruning hand-authored permission rules, hook
    # entries and disallowedTools drift across all of them at once, with
    # nothing backed up and nothing that reconstructs a pruned entry. These are
    # occasional maintenance commands rather than routine ones, so the prompt
    # costs nothing -- and the framework's non-TTY refusal is exactly the
    # second deliberate token the hand-rolled --dry-run/--apply pair used to
    # require before that pair was collapsed onto the framework's --dry-run.
    app.command(
        "patch-profiles",
        effect="mutating",
        consequential=True,
        help="reconcile every managed profile and shared-settings.json to EXACTLY the canonical guardrail model (hooks, disallowedTools, permissions deny/ask); prunes drift and user-added extras -- the old additive, extras-preserving behavior is gone. Deploys any missing guardrail hook scripts. The 'default' profile (~/.claude) is never touched. Preview with --dry-run; writing needs a terminal or --approve-consequential.",
    )(_bind(_handle_patch_profiles, ws))

    app.command(
        "reconcile-permissions",
        effect="mutating",
        consequential=True,
        help="reconcile every managed profile and shared-settings.json to EXACTLY the canonical guardrail model (hooks, disallowedTools, permissions deny/ask made exact; allow keeps only its non-conflicting entries); prunes all drift and user-added extras. The 'default' profile (~/.claude) is never touched. Pass --dry-run to preview the per-target diff without writing; writing needs a terminal to confirm at, or --approve-consequential.",
    )(_bind(_handle_reconcile_permissions, ws))

    # -- Permission group --

    app.command(
        "purge-plugins",
        effect="mutating",
        help=(
            "remove the Claude Code plugin tree from the selected profiles: the"
            " official-marketplace clone and every plugin installed from it, six"
            " to ten megabytes per profile. Opt-in and separate from the"
            " canonical reconciliation, which is exact and would otherwise"
            " delete plugin state on every run. Names the marketplaces and"
            " plugins it finds before removing them; --dry-run reports the"
            " inventory without touching anything. New launches do not collect"
            " a new tree -- the launch environment suppresses the auto-install,"
            " one-way per profile. The 'default' profile (~/.claude) is never"
            " touched."
        ),
    )(_bind(_handle_purge_plugins, ws))

    perm_grp = app.group(
        "permission",
        help="add, remove, and list permission rules across Claude profiles",
    )

    perm_grp.command(
        "add",
        effect="mutating",
        help=(
            "Add a permission rule to a profile's settings.json. Takes a category"
            " (allow, deny, or ask) and a rule string such as Bash or Read(//home/**)."
            " Writes the rule into the specified category array. Use --profile to target"
            " a single profile or --all-profiles to apply the rule across every registered"
            " profile. Skips duplicates if the rule already exists in the category."
        ),
        args=[
            Arg(
                name="category",
                presence="required",
                help="permission category to add the rule to: allow, deny, or ask",
            ),
            Arg(
                name="rule",
                presence="required",
                help="permission rule string to add (e.g. Bash, Read(//home/**), Edit)",
            ),
        ],
    )(_bind(_handle_permission_add, ws))

    perm_grp.command(
        "remove",
        effect="mutating",
        help=(
            "Remove a permission rule from a profile's settings.json. Takes a category"
            " (allow, deny, or ask) and the exact rule string to delete. The rule is"
            " removed from the specified category array and the file is saved. Use"
            " --profile to target a single profile or --all-profiles to remove the rule"
            " from every registered profile. Reports whether the rule was found."
        ),
        args=[
            Arg(
                name="category",
                presence="required",
                help="permission category to remove the rule from: allow, deny, or ask",
            ),
            Arg(
                name="rule",
                presence="required",
                help="exact permission rule string to remove (must match an existing entry)",
            ),
        ],
    )(_bind(_handle_permission_remove, ws))

    perm_grp.command(
        "list",
        effect="read_only",
        help=(
            "List permission rules from a profile's settings.json. Displays rules in"
            " grouped or flat format controlled by --format. Use --category to"
            " filter output to a single category (allow, deny, or ask). Use --profile"
            " to inspect a single profile or --all-profiles to show rules from every"
            " registered profile, with each profile's rules displayed under a header."
            " The framework-owned --json answers a machine instead: one envelope"
            " carrying every listed profile, whatever --format the human form would"
            " have used."
        ),
        payload_schema=_PERMISSION_LIST_PAYLOAD_SCHEMA,
    )(_bind(_handle_permission_list, ws))

    # -- Launch command (default when no subcommand given) --
    # The session selection is the `_launch_session` selector, declared on the
    # handler; what is left in a flag set here is everything that is not it.

    _segment_flag_set = FlagSet(
        name="segments",
        flags=[
            Flag(
                name="profile",
                type=str,
                presence="optional",
                help="preset the Profile segment to this value, skipping TUI selection for it",
            ),
            Flag(
                name="github",
                type=str,
                presence="optional",
                help="preset the GitHub account segment to this value, skipping TUI selection for it",
            ),
            Flag(
                name="model",
                type=str,
                presence="optional",
                help="preset the Model segment to this value (e.g. opus, sonnet), skipping TUI selection",
            ),
            Flag(
                name="directory",
                type=str,
                presence="optional",
                help="preset the Directory segment to this path, skipping TUI selection for it",
            ),
            Flag(
                name="mcp",
                type=str,
                presence="optional",
                help="preset the MCP mode segment to this value, skipping TUI selection for it",
            ),
            Flag(
                name="permissions",
                type=str,
                presence="optional",
                help="preset the Permissions segment to this value, skipping TUI selection for it",
            ),
            # A declared empty default: the mutating-default ban leaves an
            # empty collection legal, because no framework-chosen value can
            # reach a write through "no elements".
            Flag(
                name="set",
                short="s",
                type=str,
                repeatable=True,
                unique=False,
                default=[],
                help="set any segment value as KEY=VALUE (e.g. -s version=2.1.119); repeatable",
            ),
        ],
    )

    _client_flag_set = FlagSet(
        name="client",
        flags=[
            # Absence is delivered as absence, so the launcher can tell an
            # explicit choice from the lack of one. The value is validated in
            # the handler against CLIENT_NAMES, the registry the client
            # adapters build.
            Flag(
                name="client",
                type=str,
                presence="optional",
                help=(
                    "launch target client (one of: " + ", ".join(CLIENT_NAMES) + "). "
                    "When omitted, the interactive launcher prompts with a Client "
                    "step (cursor on config default_client, else claude) and "
                    "non-interactive launches use default_client. Passing it "
                    "explicitly skips that step. 'miniclaude' launches the "
                    "miniclaude REPL instead; version and strict-MCP selections, "
                    "config default_flags, and the disallowedTools list are "
                    "claude-client-only"
                ),
            ),
        ],
    )

    app.command(
        "launch",
        effect="mutating",
        grants=[
            Grant(
                "exec-client",
                "the launcher replaces this process with the selected client binary",
                strictcli.PROC_MUTATE,
            )
        ],
        help="start the interactive TUI launcher to select a profile, model, and directory",
        flag_sets=[_segment_flag_set, _client_flag_set],
    )(_bind(_handle_launch, ws, locator))

    return app


def main() -> None:
    """CLI entry point that parses arguments and dispatches to subcommands or the TUI."""
    global _passthrough

    # Pre-process sys.argv: extract passthrough args after "--"
    argv = list(sys.argv)
    if "--" in argv:
        idx = argv.index("--")
        _passthrough = argv[idx + 1 :]
        sys.argv = argv[:idx]
    else:
        _passthrough = []

    # If no subcommand given, inject "launch" so the TUI starts.
    # Exception: app-level flags (--help/-h/--version/-v/--dump-schema) are
    # handled at the app level, not routed to the launch command.
    sys.argv = _inject_launch(sys.argv)

    # Open the workspace ONCE at the dispatch boundary and thread it (plus the
    # binary locator, which is separate from the workspace by design) into every
    # handler via `_bind`. `Workspace.default()` is pure value construction (the
    # sole reader of the config-dir override env var); no filesystem or terminal
    # I/O happens here, so `--dump-schema` stays hermetic.
    from .workspace import Workspace
    from .binaries import BinaryLocator

    ws = Workspace.default()
    locator = BinaryLocator.default()

    _build_app(ws, locator).run()
