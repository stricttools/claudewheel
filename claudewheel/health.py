"""Pre-launch diagnostics: symlinks, tokens, hooks, permissions, and disk usage."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import OPTIONS_FILE, PROFILES_DIR, PROFILE_SHARED_DIRS, SHARED_DIR, SHARED_SETTINGS_FILE, SKILLS_DIR, TOKENS_FILE
from .defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from .discovery import discover_profiles


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


def check_tmp_claude_size() -> HealthResult:
    """Check size of /tmp/claude-$UID/ directory."""
    uid = os.getuid()
    tmp_dir = Path(f"/tmp/claude-{uid}")
    if not tmp_dir.exists():
        return HealthResult(True, "/tmp/claude", "not present")
    try:
        total = sum(f.stat().st_size for f in tmp_dir.rglob("*") if f.is_file())
        mb = total / (1024 * 1024)
        if mb > 2048:
            return HealthResult(False, "/tmp/claude", f"{mb:.0f} MB (>2 GB threshold)")
        return HealthResult(True, "/tmp/claude", f"{mb:.0f} MB")
    except Exception as e:
        return HealthResult(True, "/tmp/claude", f"check failed: {e}")


def _discover_profiles() -> list[tuple[str, Path]]:
    """Find Claude profile dirs via the shared discovery module.

    Returns a sorted list of (profile_name, profile_path) tuples.
    """
    return [(p.name, p.path) for p in discover_profiles()]


# -- Shared-store profile checks -------------------------------------------


def check_shared_symlinks() -> HealthResult:
    """Verify each profile's shared dirs are symlinks to ~/.claudewheel/shared/."""
    profiles = _discover_profiles()
    if not profiles:
        return HealthResult(True, "shared-symlinks", "no profiles found")

    broken: list[str] = []
    for name, pdir in profiles:
        # Shared dirs -> ~/.claudewheel/shared/<dir>
        for d in PROFILE_SHARED_DIRS:
            link = pdir / d
            target = SHARED_DIR / d
            if not link.is_symlink() or link.resolve() != target.resolve():
                broken.append(f"{name}/{d}")
        # skills -> ~/.claudewheel/skills
        sk = pdir / "skills"
        if SKILLS_DIR.is_dir() and (not sk.is_symlink() or sk.resolve() != SKILLS_DIR.resolve()):
            broken.append(f"{name}/skills")

    if broken:
        return HealthResult(False, "shared-symlinks", f"broken: {', '.join(broken)}")
    return HealthResult(True, "shared-symlinks", f"all {len(profiles)} profiles OK")


def check_hooks_wired() -> HealthResult:
    """Verify each profile's settings.json has required hooks.

    Checks:
    - UserPromptSubmit: hook-timestamp
    - PreToolUse: hook-block-worktree (matcher: Agent)
    """
    profiles = _discover_profiles()
    if not profiles:
        return HealthResult(True, "hooks-wired", "no profiles found")

    missing: list[str] = []
    for name, pdir in profiles:
        settings_file = pdir / "settings.json"
        if not settings_file.exists():
            missing.append(f"{name}: no settings.json")
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            missing.append(f"{name}: unreadable settings.json")
            continue

        hooks = settings.get("hooks", {})

        # Check UserPromptSubmit hooks
        ups_list = hooks.get("UserPromptSubmit", [])
        ups_commands: list[str] = []
        if ups_list and isinstance(ups_list, list):
            first = ups_list[0]
            hooks_list = first.get("hooks", []) if isinstance(first, dict) else []
            for h in hooks_list:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                ups_commands.append(cmd)

        combined = " ".join(ups_commands)
        if "hook-timestamp" not in combined:
            missing.append(f"{name}: missing hook-timestamp")

        # Check PreToolUse Agent hook (block-worktree)
        ptu_list = hooks.get("PreToolUse", [])
        has_block_worktree = False
        if ptu_list and isinstance(ptu_list, list):
            for entry in ptu_list:
                if not isinstance(entry, dict):
                    continue
                if entry.get("matcher") != "Agent":
                    continue
                entry_hooks = entry.get("hooks", [])
                for h in entry_hooks:
                    cmd = h.get("command", "") if isinstance(h, dict) else ""
                    if "hook-block-worktree" in cmd:
                        has_block_worktree = True
                        break
                if has_block_worktree:
                    break
        if not has_block_worktree:
            missing.append(f"{name}: missing PreToolUse hook-block-worktree")

    if missing:
        return HealthResult(False, "hooks-wired", "; ".join(missing))
    return HealthResult(True, "hooks-wired", f"all {len(profiles)} profiles OK")


def check_settings_defaults() -> HealthResult:
    """Verify each profile enforces expected defaults in settings.json."""
    profiles = _discover_profiles()
    if not profiles:
        return HealthResult(True, "settings-defaults", "no profiles found")

    issues: list[str] = []
    for name, pdir in profiles:
        settings_file = pdir / "settings.json"
        if not settings_file.exists():
            issues.append(f"{name}: no settings.json")
            continue
        try:
            s = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            issues.append(f"{name}: unreadable settings.json")
            continue

        if s.get("awaySummaryEnabled") is not False:
            issues.append(f"{name}: awaySummaryEnabled != false")
        cpd = s.get("cleanupPeriodDays")
        if not isinstance(cpd, (int, float)) or cpd < 365:
            issues.append(f"{name}: cleanupPeriodDays < 365 ({cpd!r})")
        if s.get("autoMemoryEnabled") is not False:
            issues.append(f"{name}: autoMemoryEnabled != false")
        perms = s.get("permissions", {})
        if len(perms.get("deny", [])) < 5:
            issues.append(f"{name}: fewer than 5 deny rules")
        if len(perms.get("ask", [])) < 4:
            issues.append(f"{name}: fewer than 4 ask rules")
        if perms.get("disableAutoMode") != "disable":
            issues.append(f"{name}: auto mode not disabled")
        cw = s.get("claudewheel", {})
        current_disallowed = set(cw.get("disallowedTools", []))
        missing_tools = sorted(set(DISALLOWED_TOOLS) - current_disallowed)
        if missing_tools:
            issues.append(f"{name}: missing disallowedTools: {', '.join(missing_tools)}")
        if "disallowedTools" in s:
            issues.append(f"{name}: has inert top-level disallowedTools key (run patch-profiles)")

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


def check_shared_settings_drift() -> HealthResult:
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

    profiles = _discover_profiles()
    if not profiles:
        return HealthResult(True, "settings-drift", "no profiles found")

    all_diffs: list[str] = []
    for name, pdir in profiles:
        settings_file = pdir / "settings.json"
        if not settings_file.exists():
            all_diffs.append(f"{name}: no settings.json")
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            all_diffs.append(f"{name}: unreadable settings.json")
            continue

        # Compare hooks
        profile_hooks = settings.get("hooks", {})
        hook_diffs = _diff_json("hooks", canonical_hooks, profile_hooks)
        for d in hook_diffs:
            all_diffs.append(f"{name}: {d}")

        # Compare disallowedTools
        profile_disallowed = settings.get("claudewheel", {}).get("disallowedTools", [])
        tool_diffs = _diff_json("disallowedTools", canonical_disallowed, profile_disallowed)
        for d in tool_diffs:
            all_diffs.append(f"{name}: {d}")

    if all_diffs:
        return HealthResult(False, "settings-drift", "; ".join(all_diffs))
    return HealthResult(True, "settings-drift", f"all {len(profiles)} profiles in sync")


def check_token_expiry() -> HealthResult:
    """Warn if any token is approaching 1-year expiry (setup-token TTL)."""
    tokens_file = TOKENS_FILE
    if not tokens_file.exists():
        return HealthResult(True, "token-expiry", "no tokens.json")
    try:
        tokens = json.loads(tokens_file.read_text())
    except (json.JSONDecodeError, OSError):
        return HealthResult(False, "token-expiry", "unreadable tokens.json")
    from datetime import date, timedelta
    today = date.today()
    expiring: list[str] = []
    min_remaining = 365
    for name, entry in tokens.items():
        remaining = 365
        if isinstance(entry, dict):
            if entry.get("expires_at"):
                try:
                    remaining = (date.fromisoformat(entry["expires_at"]) - today).days
                except (ValueError, TypeError):
                    pass
            elif entry.get("created"):
                try:
                    remaining = 365 - (today - date.fromisoformat(entry["created"])).days
                except (ValueError, TypeError):
                    pass
        else:
            import time
            remaining = 365 - (time.time() - tokens_file.stat().st_mtime) / 86400
        min_remaining = min(min_remaining, remaining)
        if remaining < 30:
            expiring.append(f"{name} (~{max(0, int(remaining))}d)")
    if expiring:
        return HealthResult(False, "token-expiry",
                            f"expiring soon: {', '.join(expiring)} — run claude setup-token")
    return HealthResult(True, "token-expiry", f"~{int(min_remaining)} days remaining")


def check_tokens() -> HealthResult:
    """Verify each profile has a matching entry in ~/.claudewheel/tokens.json."""
    tokens_file = TOKENS_FILE
    if not tokens_file.exists():
        return HealthResult(True, "tokens", "tokens.json not found")

    try:
        tokens = json.loads(tokens_file.read_text())
    except (json.JSONDecodeError, OSError):
        return HealthResult(False, "tokens", "unreadable tokens.json")

    profiles = _discover_profiles()
    if not profiles:
        return HealthResult(True, "tokens", "no profiles found")

    missing: list[str] = []
    for name, _pdir in profiles:
        entry = tokens.get(name)
        has_token = (isinstance(entry, str) and bool(entry)) or \
                    (isinstance(entry, dict) and bool(entry.get("token")))
        if not has_token:
            missing.append(name)

    if missing:
        return HealthResult(False, "tokens", f"missing tokens: {', '.join(missing)}")
    return HealthResult(True, "tokens", f"all {len(profiles)} profiles OK")


def check_orphan_profiles() -> HealthResult:
    """Detect profile dirs in ~/.claudewheel/profiles/ that are not registered.

    A directory is "orphan" if it:
      - lives in ~/.claudewheel/profiles/
      - is NOT discovered by _discover_profiles() (which checks .credentials.json
        and tokens.json)
      - is NOT listed in options.json's profile values

    For each orphan, we also flag if it contains broken symlinks (symlinks
    whose target does not exist).
    """
    if not PROFILES_DIR.is_dir():
        return HealthResult(True, "orphan-profiles", "no profiles dir found")

    # Registered profiles (discovered via .credentials.json or tokens.json)
    registered = {name for name, _ in _discover_profiles()}

    # Profiles known to options.json (may not have .credentials.json yet)
    options_profiles: set[str] = set()
    try:
        options = json.loads(OPTIONS_FILE.read_text())
        options_profiles = set(options.get("profile", {}).get("values", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    orphans: list[str] = []
    for entry in sorted(PROFILES_DIR.iterdir()):
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


def check_file_permissions() -> HealthResult:
    """Verify sensitive files have restrictive permissions (0600)."""
    profiles = _discover_profiles()
    issues: list[str] = []
    for name, pdir in profiles:
        creds = pdir / ".credentials.json"
        if creds.exists():
            mode = oct(creds.stat().st_mode & 0o777)
            if mode != "0o600":
                issues.append(f"{name}/.credentials.json is {mode}")
    tokens_file = TOKENS_FILE
    if tokens_file.exists():
        mode = oct(tokens_file.stat().st_mode & 0o777)
        if mode != "0o600":
            issues.append(f"tokens.json is {mode}")
    if issues:
        return HealthResult(False, "file-perms", "; ".join(issues))
    return HealthResult(True, "file-perms", "all sensitive files 0600")


def run_health_check() -> list[HealthResult]:
    """Run all health checks and return results."""
    return [
        check_tmpfs_quota(),
        check_tmp_claude_size(),
        check_shared_symlinks(),
        check_hooks_wired(),
        check_settings_defaults(),
        check_shared_settings_drift(),
        check_tokens(),
        check_token_expiry(),
        check_orphan_profiles(),
        check_file_permissions(),
    ]


def print_health_report(results: list[HealthResult], file=None) -> None:
    """Print health check results. Defaults to stdout; pass file=sys.stderr for non-interactive mode."""
    for r in results:
        status = "OK" if r.ok else "WARN"
        print(f"  [{status}] {r.label}: {r.detail}", file=file)
