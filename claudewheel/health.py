"""Pre-launch diagnostics: symlinks, tokens, disk usage, and permission/hook drift against the canonical guardrail model."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import guardrail
from .appdata import OptionsFile
from .constants import INODES_FILE, OPTIONS_FILE, PROFILES_DIR, PROFILE_SHARED_DIRS, SCRIPTS_DIR, SHARED_SETTINGS_FILE, SKILLS_DIR, TOKENS_FILE
from .defaults import DISALLOWED_TOOLS
from .discovery import classify_shared_dirs
from .fsutil import write_json_atomic
from .hook_scripts import HOOK_SCRIPTS
from .profile_store import Profile, ProfileStore
from .tokens import TOKEN_TTL_DAYS, TokenStore, TokenStoreError, compute_expiry, parse_entry


@dataclass
class HealthResult:
    """Health check result with ok status, label, and detail message."""

    ok: bool
    label: str
    detail: str


def check_tmpfs_quota() -> HealthResult:
    """Check /tmp usage percentage via df."""
    try:
        result = subprocess.run(
            ["df", "--output=pcent", "/tmp"],
            capture_output=True, text=True, timeout=3
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            pct = int(lines[-1].strip().rstrip("%"))
            if pct > 80:
                return HealthResult(False, "tmpfs", f"{pct}% used (>80% threshold)")
            return HealthResult(True, "tmpfs", f"{pct}% used")
    except Exception as e:
        return HealthResult(True, "tmpfs", f"check failed: {e}")
    return HealthResult(True, "tmpfs", "unknown")


def _tmp_claude_dir() -> Path:
    """Return the per-user Claude scratch dir under /tmp."""
    return Path(f"/tmp/claude-{os.getuid()}")


def _real_disk_usage(root: Path) -> int:
    """Sum the real tmpfs block usage of regular files under root.

    Correctness requirements this satisfies:
    - Never follows symlinks. os.walk(followlinks=False) does not descend into
      symlinked directories, and lstat + S_ISREG skips symlinks to files. So
      symlink targets living outside /tmp (Claude session dirs link into home
      and project dirs) are never counted -- they consume zero /tmp space.
    - Counts REAL disk usage (st_blocks * 512), not apparent st_size. tmpfs
      charges by allocated blocks; apparent size overcounts sparse files.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_blocks * 512
    return total


def check_tmp_claude_size() -> HealthResult:
    """Check real tmpfs usage of /tmp/claude-$UID/ (excludes symlink targets)."""
    tmp_dir = _tmp_claude_dir()
    if not tmp_dir.exists():
        return HealthResult(True, "/tmp/claude", "not present")
    try:
        total = _real_disk_usage(tmp_dir)
        mb = total / (1024 * 1024)
        if mb > 1024:
            return HealthResult(False, "/tmp/claude", f"{mb:.0f} MB (>1 GB threshold)")
        return HealthResult(True, "/tmp/claude", f"{mb:.0f} MB")
    except Exception as e:
        return HealthResult(True, "/tmp/claude", f"check failed: {e}")


def _make_store() -> ProfileStore:
    """Build a read-only ProfileStore from health's module path constants.

    Constructed at call time (never module-import time) so the tests' patches of
    ``health.PROFILES_DIR`` / ``health.TOKENS_FILE`` and ``Path.home`` take
    effect. Read-only: the write-path stores stay ``None``.
    """
    return ProfileStore(PROFILES_DIR, Path.home() / ".claude", TokenStore(TOKENS_FILE))


def _discover_profiles(tokens: dict | None = None) -> list[Profile]:
    """Enumerate profiles via ProfileStore.

    *tokens* ``None`` loads token data via ``TokenStore`` (a corrupt tokens.json
    raises :class:`TokenStoreError`). Callers inside a health run pass the single
    token view loaded once by :func:`run_health_check` (``{}`` when corrupt) so
    enumeration never re-reads the file.
    """
    return _make_store().enumerate(tokens)


# -- Shared-store profile checks -------------------------------------------


def check_shared_symlinks(tokens: dict | None = None) -> HealthResult:
    """Verify each profile's shared dirs are symlinks to ~/.claudewheel/shared/."""
    profiles = _discover_profiles(tokens)
    if not profiles:
        return HealthResult(True, "shared-symlinks", "no profiles found")

    broken: list[str] = []
    for p in profiles:
        states = classify_shared_dirs(p.path)
        # Health checks completeness: anything not "intact" is broken,
        # including "missing" (unlike delete-safety, which only fears
        # "real-dir"). This preserves the original is_symlink() semantics.
        for d in PROFILE_SHARED_DIRS:
            if states[d] != "intact":
                broken.append(f"{p.name}/{d}")
        # skills -> ~/.claudewheel/skills (only checked if the store exists)
        if SKILLS_DIR.is_dir() and states["skills"] != "intact":
            broken.append(f"{p.name}/skills")

    if broken:
        return HealthResult(False, "shared-symlinks", f"broken: {', '.join(broken)}")
    return HealthResult(True, "shared-symlinks", f"all {len(profiles)} profiles OK")


def _hook_wired(hooks: object, event: str, matcher: str, script: str) -> bool:
    """Return True if *hooks* wires *script* under *event* with *matcher*.

    An entry matches when its ``matcher`` equals *matcher* (an absent matcher
    is treated as the empty string, which is how UserPromptSubmit entries are
    stored) and it carries a hook command mentioning *script*.
    """
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(event, [])
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("matcher", "") != matcher:
            continue
        for h in entry.get("hooks", []):
            cmd = h.get("command", "") if isinstance(h, dict) else ""
            if script in cmd:
                return True
    return False


def check_hooks_wired(tokens: dict | None = None) -> HealthResult:
    """Verify each profile wires every expected hook in settings.json.

    The canonical wirings are the (event, matcher, script-name) triples in
    ``guardrail.EXPECTED_HOOK_WIRINGS``. A profile passes only when every
    triple is present: an entry under the given event whose matcher equals the
    given matcher, containing a hook command that references the given script.
    """
    profiles = _discover_profiles(tokens)
    if not profiles:
        return HealthResult(True, "hooks-wired", "no profiles found")

    missing: list[str] = []
    for p in profiles:
        settings_file = p.path / "settings.json"
        if not settings_file.exists():
            missing.append(f"{p.name}: no settings.json")
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            missing.append(f"{p.name}: unreadable settings.json")
            continue

        hooks = settings.get("hooks", {})
        for event, matcher, script in guardrail.EXPECTED_HOOK_WIRINGS:
            if not _hook_wired(hooks, event, matcher, script):
                missing.append(f"{p.name}: missing ({event}, {matcher}, {script})")

    if missing:
        return HealthResult(
            False, "hooks-wired",
            "; ".join(missing) + " -- run 'claudewheel patch-profiles' to sync",
        )
    return HealthResult(True, "hooks-wired", f"all {len(profiles)} profiles OK")


def check_settings_defaults(tokens: dict | None = None) -> HealthResult:
    """Verify each profile enforces expected defaults in settings.json."""
    profiles = _discover_profiles(tokens)
    if not profiles:
        return HealthResult(True, "settings-defaults", "no profiles found")

    issues: list[str] = []
    for p in profiles:
        settings_file = p.path / "settings.json"
        if not settings_file.exists():
            issues.append(f"{p.name}: no settings.json")
            continue
        try:
            s = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            issues.append(f"{p.name}: unreadable settings.json")
            continue

        if s.get("awaySummaryEnabled") is not False:
            issues.append(f"{p.name}: awaySummaryEnabled != false")
        cpd = s.get("cleanupPeriodDays")
        if not isinstance(cpd, (int, float)) or cpd < 365:
            issues.append(f"{p.name}: cleanupPeriodDays < 365 ({cpd!r})")
        if s.get("autoMemoryEnabled") is not False:
            issues.append(f"{p.name}: autoMemoryEnabled != false")
        perms = s.get("permissions", {})
        if perms.get("disableAutoMode") != "disable":
            issues.append(f"{p.name}: auto mode not disabled")
        cw = s.get("claudewheel", {})
        current_disallowed = set(cw.get("disallowedTools", []))
        missing_tools = sorted(set(DISALLOWED_TOOLS) - current_disallowed)
        if missing_tools:
            issues.append(f"{p.name}: missing disallowedTools: {', '.join(missing_tools)} (run 'claudewheel patch-profiles')")
        if "disallowedTools" in s:
            issues.append(f"{p.name}: has inert top-level disallowedTools key (run 'claudewheel patch-profiles')")

    if issues:
        return HealthResult(False, "settings-defaults", "; ".join(issues))
    return HealthResult(True, "settings-defaults", f"all {len(profiles)} profiles OK")


def _diff_json(label: str, canonical: object, actual: object) -> list[str]:
    """Return human-readable lines describing differences between two JSON values."""
    diffs: list[str] = []
    if isinstance(canonical, dict) and isinstance(actual, dict):
        for key in sorted(set(canonical) | set(actual)):
            if key not in actual:
                diffs.append(f"{label}.{key}: missing (expected {json.dumps(canonical[key])})")
            elif key not in canonical:
                diffs.append(f"{label}.{key}: extra (unexpected)")
            elif canonical[key] != actual[key]:
                diffs.extend(_diff_json(f"{label}.{key}", canonical[key], actual[key]))
    elif isinstance(canonical, list) and isinstance(actual, list):
        if set(canonical) != set(actual) if all(isinstance(x, str) for x in canonical + actual) else canonical != actual:
            missing = [x for x in canonical if x not in actual]
            extra = [x for x in actual if x not in canonical]
            if missing:
                diffs.append(f"{label}: missing {missing}")
            if extra:
                diffs.append(f"{label}: extra {extra}")
    else:
        diffs.append(f"{label}: expected {json.dumps(canonical)}, got {json.dumps(actual)}")
    return diffs


def check_shared_settings_drift(tokens: dict | None = None) -> HealthResult:
    """Compare each profile's hooks and disallowedTools against shared-settings.json."""
    # Load shared settings
    if not SHARED_SETTINGS_FILE.exists():
        return HealthResult(True, "settings-drift", "shared-settings.json not found (will be created on next launch)")

    try:
        shared = json.loads(SHARED_SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return HealthResult(False, "settings-drift", f"unreadable shared-settings.json: {e}")

    canonical_hooks = shared.get("hooks", {})
    canonical_disallowed = shared.get("disallowedTools", [])

    profiles = _discover_profiles(tokens)
    if not profiles:
        return HealthResult(True, "settings-drift", "no profiles found")

    all_diffs: list[str] = []
    for p in profiles:
        settings_file = p.path / "settings.json"
        if not settings_file.exists():
            all_diffs.append(f"{p.name}: no settings.json")
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            all_diffs.append(f"{p.name}: unreadable settings.json")
            continue

        # Compare hooks
        profile_hooks = settings.get("hooks", {})
        hook_diffs = _diff_json("hooks", canonical_hooks, profile_hooks)
        for d in hook_diffs:
            all_diffs.append(f"{p.name}: {d}")

        # Compare disallowedTools
        profile_disallowed = settings.get("claudewheel", {}).get("disallowedTools", [])
        tool_diffs = _diff_json("disallowedTools", canonical_disallowed, profile_disallowed)
        for d in tool_diffs:
            all_diffs.append(f"{p.name}: {d}")

    if all_diffs:
        return HealthResult(False, "settings-drift", "; ".join(all_diffs))
    return HealthResult(True, "settings-drift", f"all {len(profiles)} profiles in sync")


def _canonical_permission_diffs(label: str, perms: object) -> list[str]:
    """Return drift lines comparing a permissions block against the canonical model.

    Checks ``permissions.deny`` and ``permissions.ask`` against
    ``guardrail.canonical_deny_rules()`` / ``canonical_ask_rules()`` (reporting
    missing canonical entries and extra non-canonical ones) and flags any
    ``permissions.allow`` entry that is a known dead/conflicting allow
    (``guardrail.ALLOW_CONFLICTS``).
    """
    if not isinstance(perms, dict):
        perms = {}
    diffs: list[str] = []
    diffs.extend(_diff_json(f"{label}.deny", guardrail.canonical_deny_rules(), perms.get("deny", [])))
    diffs.extend(_diff_json(f"{label}.ask", guardrail.canonical_ask_rules(), perms.get("ask", [])))
    conflicting = [a for a in perms.get("allow", []) if a in set(guardrail.ALLOW_CONFLICTS)]
    if conflicting:
        diffs.append(f"{label}.allow: dead/conflicting {conflicting}")
    return diffs


def check_canonical_permissions_drift(tokens: dict | None = None) -> HealthResult:
    """Compare each profile's permissions against the canonical guardrail model.

    For every profile settings.json and for shared-settings.json's
    ``profileDefaults`` (which seeds new profiles), verify that
    ``permissions.deny`` / ``permissions.ask`` match the canonical guardrail
    rules exactly and that no ``permissions.allow`` entry is a known
    dead/conflicting allow. Reports MISSING canonical entries, EXTRA
    non-canonical entries, and conflicting allows per profile. Warnings only --
    never raises; ok is True only when everything matches and no conflicts exist.
    """
    all_diffs: list[str] = []

    # profileDefaults in shared-settings.json seeds new profiles, so it must
    # be canonical too.
    if SHARED_SETTINGS_FILE.exists():
        try:
            shared = json.loads(SHARED_SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            all_diffs.append(f"profileDefaults: unreadable shared-settings.json: {e}")
        else:
            pd_perms = shared.get("profileDefaults", {}).get("permissions", {})
            for d in _canonical_permission_diffs("permissions", pd_perms):
                all_diffs.append(f"profileDefaults: {d}")

    profiles = _discover_profiles(tokens)
    for p in profiles:
        settings_file = p.path / "settings.json"
        if not settings_file.exists():
            all_diffs.append(f"{p.name}: no settings.json")
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            all_diffs.append(f"{p.name}: unreadable settings.json")
            continue
        perms = settings.get("permissions", {})
        for d in _canonical_permission_diffs("permissions", perms):
            all_diffs.append(f"{p.name}: {d}")

    if all_diffs:
        return HealthResult(False, "canonical-drift", "; ".join(all_diffs))
    return HealthResult(True, "canonical-drift", f"{len(profiles)} profiles + profileDefaults match canonical")


def check_auth_shadow(tokens: dict | None = None) -> HealthResult:
    """Detect profiles where .credentials.json claudeAiOauth shadows a long-lived token."""
    from .profile_info import detect_auth_shadow

    profiles = _discover_profiles(tokens)
    if not profiles:
        return HealthResult(True, "auth-shadow", "no profiles found")

    shadowed: list[str] = []
    for p in profiles:
        if detect_auth_shadow(p.name):
            shadowed.append(p.name)

    if shadowed:
        return HealthResult(
            False, "auth-shadow",
            f"shadowed: {', '.join(shadowed)} — session credentials override long-lived tokens"
        )
    return HealthResult(True, "auth-shadow", "no auth shadow detected")


def check_token_expiry(tokens: dict | None = None,
                       token_error: TokenStoreError | None = None) -> HealthResult:
    """Warn if any token is approaching 1-year expiry (setup-token TTL).

    Token corruption surfaces here as a FAILED check: a *token_error* recorded by
    the single run-level load, or (for standalone calls) a fresh
    :class:`TokenStoreError` raised while loading. The actionable exception
    message is the detail.
    """
    if token_error is not None:
        return HealthResult(False, "token-expiry", str(token_error))
    tokens_file = TOKENS_FILE
    if not tokens_file.exists():
        return HealthResult(True, "token-expiry", "no tokens.json")
    if tokens is None:
        try:
            tokens = TokenStore(tokens_file).load()
        except TokenStoreError as e:
            return HealthResult(False, "token-expiry", str(e))
    from datetime import date
    today = date.today()
    mtime = tokens_file.stat().st_mtime
    expiring: list[str] = []
    min_remaining: float = TOKEN_TTL_DAYS
    for name, entry in tokens.items():
        remaining = compute_expiry(entry, mtime, today=today).remaining_days
        min_remaining = min(min_remaining, remaining)
        if remaining < 30:
            expiring.append(f"{name} (~{max(0, int(remaining))}d)")
    if expiring:
        return HealthResult(False, "token-expiry",
                            f"expiring soon: {', '.join(expiring)} — run claude setup-token")
    return HealthResult(True, "token-expiry", f"~{int(min_remaining)} days remaining")


def check_tokens(tokens: dict | None = None,
                 token_error: TokenStoreError | None = None) -> HealthResult:
    """Verify each profile has a matching entry in ~/.claudewheel/tokens.json.

    A corrupt tokens.json is the FAILED-check carve-out: when *token_error* is
    recorded (single run-level load) or a standalone call hits a
    :class:`TokenStoreError`, this check fails with the exception's actionable
    message instead of crashing the whole run.
    """
    if token_error is not None:
        return HealthResult(False, "tokens", str(token_error))
    tokens_file = TOKENS_FILE
    if not tokens_file.exists():
        return HealthResult(True, "tokens", "tokens.json not found")

    if tokens is None:
        try:
            tokens = TokenStore(tokens_file).load()
        except TokenStoreError as e:
            return HealthResult(False, "tokens", str(e))

    profiles = _discover_profiles(tokens)
    if not profiles:
        return HealthResult(True, "tokens", "no profiles found")

    missing: list[str] = []
    for p in profiles:
        # Settings-only profiles (no credentials, no token) are brand-new
        # profiles that haven't set up auth yet -- don't warn about them.
        if not p.has_credentials and not p.has_token:
            continue
        if parse_entry(tokens.get(p.name)) is None:
            missing.append(p.name)

    if missing:
        return HealthResult(False, "tokens", f"missing tokens: {', '.join(missing)}")
    return HealthResult(True, "tokens", f"all {len(profiles)} profiles OK")


def check_orphan_profiles(tokens: dict | None = None) -> HealthResult:
    """Detect profile dirs in ~/.claudewheel/profiles/ that are not registered.

    A directory is "orphan" if it:
      - lives in ~/.claudewheel/profiles/
      - is NOT discovered by _discover_profiles() (which checks .credentials.json,
        settings.json, and tokens.json)
      - is NOT listed in options.json's profile values

    For each orphan, we also flag if it contains broken symlinks (symlinks
    whose target does not exist).
    """
    store = _make_store()
    if not store.profiles_dir.is_dir():
        return HealthResult(True, "orphan-profiles", "no profiles dir found")

    # Registered profiles (discovered via .credentials.json, settings.json, or tokens.json)
    registered = {p.name for p in store.enumerate(tokens)}

    # Profiles known to options.json (may not have .credentials.json yet).
    options = OptionsFile(OPTIONS_FILE).load({})
    profile_sec = options.get("profile", {})
    options_profiles = set(profile_sec.get("values", []))
    options_profiles |= set(profile_sec.get("pinned", []))

    orphans: list[str] = []
    for entry in sorted(store.profiles_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name in registered or name in options_profiles:
            continue

        # Check for broken symlinks inside this orphan dir
        broken_links = []
        try:
            for child in entry.iterdir():
                if child.is_symlink() and not child.exists():
                    broken_links.append(child.name)
        except OSError:
            pass

        if broken_links:
            orphans.append(f"{name} (broken symlinks: {', '.join(broken_links)})")
        else:
            orphans.append(name)

    if orphans:
        return HealthResult(False, "orphan-profiles", f"orphans: {', '.join(orphans)}")
    return HealthResult(True, "orphan-profiles", "no orphan dirs found")


def check_file_permissions(tokens: dict | None = None) -> HealthResult:
    """Verify sensitive files have restrictive permissions (0600)."""
    profiles = _discover_profiles(tokens)
    issues: list[str] = []
    for p in profiles:
        creds = p.path / ".credentials.json"
        if creds.exists():
            mode = oct(creds.stat().st_mode & 0o777)
            if mode != "0o600":
                issues.append(f"{p.name}/.credentials.json is {mode}")
    tokens_file = TOKENS_FILE
    if tokens_file.exists():
        mode = oct(tokens_file.stat().st_mode & 0o777)
        if mode != "0o600":
            issues.append(f"tokens.json is {mode}")
    if issues:
        return HealthResult(False, "file-perms", "; ".join(issues))
    return HealthResult(True, "file-perms", "all sensitive files 0600")


def check_inode_renames() -> HealthResult:
    """Detect directory renames by comparing inode records against the filesystem."""
    if not INODES_FILE.exists():
        return HealthResult(True, "inode-renames", "no inode data yet")

    try:
        data = json.loads(INODES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return HealthResult(False, "inode-renames", "unreadable inodes.json")

    # Build reverse map: inode -> [paths]
    by_inode: dict[int, list[str]] = {}
    for path, inode in data.items():
        by_inode.setdefault(inode, []).append(path)

    renames: list[str] = []
    stale: list[str] = []
    for inode, paths in by_inode.items():
        if len(paths) < 2:
            # Single entry: check if path still exists
            if not os.path.exists(paths[0]):
                stale.append(paths[0])
            continue
        # Multiple paths with same inode: find which exist
        existing = [p for p in paths if os.path.exists(p)]
        missing = [p for p in paths if not os.path.exists(p)]
        if existing and missing:
            new = existing[0]
            for old in missing:
                renames.append(
                    f"{old} -> {new}. "
                    f"Run: claudewheel mv --post-hoc {old} {new}"
                )

    # Clean up stale entries (deleted dirs with no matching inode elsewhere)
    if stale:
        for s in stale:
            del data[s]
        try:
            INODES_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(INODES_FILE, data)
        except OSError:
            pass

    if renames:
        return HealthResult(False, "inode-renames", "; ".join(renames))
    if stale:
        return HealthResult(True, "inode-renames", f"cleaned {len(stale)} stale entries")
    return HealthResult(True, "inode-renames", "no renames detected")


def check_deployed_hook_drift() -> HealthResult:
    """Compare deployed hook scripts against the generated ``HOOK_SCRIPTS`` model.

    Byte-hashes each script deployed under ``SCRIPTS_DIR`` against the
    corresponding ``HOOK_SCRIPTS[name]`` string (the canonical model, generated
    from the guardrail spec at import). Drift means a deployed script no longer
    matches what ``claudewheel deploy-hooks`` would write -- usually a stale copy
    left over after the model was regenerated.

    Warn-only: reports drift but NEVER raises and is never a hard gate. Absence
    is not drift: if ``SCRIPTS_DIR`` does not exist (CI, fresh machines) or an
    individual model script has not been deployed yet, it is skipped and the
    check stays OK. Only the scripts present in both ``HOOK_SCRIPTS`` and on disk
    are compared.
    """
    if not SCRIPTS_DIR.is_dir():
        return HealthResult(True, "hook-drift", "no scripts dir (hooks not deployed)")

    drifted: list[str] = []
    checked = 0
    for name in sorted(HOOK_SCRIPTS):
        dest = SCRIPTS_DIR / name
        if not dest.exists():
            continue  # not deployed on this machine -> nothing to compare
        checked += 1
        try:
            disk = dest.read_bytes()
        except OSError as e:
            drifted.append(f"{name}: unreadable ({e})")
            continue
        if disk != HOOK_SCRIPTS[name].encode():
            drifted.append(name)

    if drifted:
        return HealthResult(
            False, "hook-drift",
            f"deployed scripts differ from model: {', '.join(drifted)} "
            "-- run 'claudewheel deploy-hooks <name> --force-overwrite'",
        )
    if checked == 0:
        return HealthResult(True, "hook-drift", "no model hook scripts deployed")
    return HealthResult(True, "hook-drift", f"all {checked} deployed hook scripts match model")


def _stale_hook_command_paths(hooks: object, scripts_dir: Path) -> list[str]:
    """Return claudewheel-managed hook commands NOT rooted at *scripts_dir*.

    Walks every hook command under *hooks* and considers only commands whose
    basename is a known claudewheel hook script (``HOOK_SCRIPTS``). A managed
    command is "stale" when its parent directory is not *scripts_dir* -- i.e. it
    points at a scripts directory left behind by a workspace relocation. Commands
    for user-custom (non-claudewheel) scripts are ignored entirely, so unrelated
    hooks under any directory are preserved.
    """
    stale: list[str] = []
    if not isinstance(hooks, dict):
        return stale
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command", "")
                if not cmd:
                    continue
                path = Path(cmd)
                if path.name in HOOK_SCRIPTS and path.parent != scripts_dir:
                    stale.append(cmd)
    return stale


def check_relocated_hook_paths(tokens: dict | None = None) -> HealthResult:
    """Detect hook commands pointing at a scripts dir other than the current one.

    The deployed-hook drift check compares script CONTENT hashes and so cannot
    see a hook whose command still references a STALE absolute scripts directory
    after the workspace was relocated (the substring matcher in
    ``check_hooks_wired`` also passes for a stale root). This check closes that
    blind spot: for ``shared-settings.json`` and every profile's
    ``settings.json``, it flags any claudewheel-managed hook command whose parent
    directory is not the current ``SCRIPTS_DIR``. Intact (current-root) and
    absent hooks pass; ``claudewheel patch-profiles`` repaths any it finds.
    """
    scripts_dir = SCRIPTS_DIR
    issues: list[str] = []

    if SHARED_SETTINGS_FILE.exists():
        try:
            shared = json.loads(SHARED_SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            shared = None
        if isinstance(shared, dict):
            for cmd in _stale_hook_command_paths(shared.get("hooks", {}), scripts_dir):
                issues.append(f"shared-settings.json: {cmd}")

    for p in _discover_profiles(tokens):
        settings_file = p.path / "settings.json"
        if not settings_file.exists():
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for cmd in _stale_hook_command_paths(settings.get("hooks", {}), scripts_dir):
            issues.append(f"{p.name}: {cmd}")

    if issues:
        return HealthResult(
            False, "hook-path-drift",
            "; ".join(issues)
            + f" -- hook commands should live under {scripts_dir}; "
            "run 'claudewheel patch-profiles' to fix",
        )
    return HealthResult(True, "hook-path-drift", "all hook commands under current scripts dir")


def run_health_check() -> list[HealthResult]:
    """Run all health checks and return results.

    Token data is loaded ONCE here (the single-load carve-out): a corrupt
    tokens.json does not crash the run -- the error is recorded, ``{}`` is used as
    the explicit token view so every profile-based check still runs (profiles
    enumerate dir-only, has_token False), and the recorded error surfaces as a
    FAILED token check via ``check_tokens`` / ``check_token_expiry``.
    """
    store = _make_store()
    token_error: TokenStoreError | None = None
    try:
        tokens: dict = store.token_store.load()
    except TokenStoreError as e:
        token_error = e
        tokens = {}

    return [
        check_tmpfs_quota(),
        check_tmp_claude_size(),
        check_shared_symlinks(tokens),
        check_hooks_wired(tokens),
        check_settings_defaults(tokens),
        check_shared_settings_drift(tokens),
        check_canonical_permissions_drift(tokens),
        check_deployed_hook_drift(),
        check_relocated_hook_paths(tokens),
        check_tokens(tokens, token_error),
        check_token_expiry(tokens, token_error),
        check_auth_shadow(tokens),
        check_orphan_profiles(tokens),
        check_file_permissions(tokens),
        check_inode_renames(),
    ]


def print_health_report(results: list[HealthResult], file=None) -> None:
    """Print health check results. Defaults to stdout; pass file=sys.stderr for non-interactive mode."""
    for r in results:
        status = "OK" if r.ok else "WARN"
        print(f"  [{status}] {r.label}: {r.detail}", file=file)
