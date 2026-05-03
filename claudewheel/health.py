"""Health check utilities for claudewheel."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import OPTIONS_FILE


@dataclass
class HealthResult:
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
    """Find Claude profile dirs (~/.claude-<name>/) that contain .credentials.json.

    Returns a sorted list of (profile_name, profile_path) tuples.
    """
    home = Path.home()
    profiles: list[tuple[str, Path]] = []
    for entry in sorted(home.iterdir()):
        if (
            entry.is_dir()
            and entry.name.startswith(".claude-")
            and (entry / ".credentials.json").exists()
        ):
            name = entry.name[len(".claude-"):]  # strip prefix
            profiles.append((name, entry))
    return profiles


# -- Shared-store profile checks -------------------------------------------


def check_shared_symlinks() -> HealthResult:
    """Verify each profile's shared dirs are symlinks to ~/.claude-shared/."""
    shared = Path.home() / ".claude-shared"
    expected_dirs = ["projects", "session-env", "file-history", "tasks", "todos"]
    profiles = _discover_profiles()
    if not profiles:
        return HealthResult(True, "shared-symlinks", "no profiles found")

    broken: list[str] = []
    for name, pdir in profiles:
        # Standard dirs -> ~/.claude-shared/<dir>
        for d in expected_dirs:
            link = pdir / d
            target = shared / d
            if not link.is_symlink() or link.resolve() != target.resolve():
                broken.append(f"{name}/{d}")
        # paste-cache -> ~/.claude-shared/paste-cache
        pc = pdir / "paste-cache"
        pc_target = shared / "paste-cache"
        if not pc.is_symlink() or pc.resolve() != pc_target.resolve():
            broken.append(f"{name}/paste-cache")
        # skills -> ~/.claude-common/skills
        sk = pdir / "skills"
        sk_target = Path.home() / ".claude-common" / "skills"
        if sk_target.is_dir() and (not sk.is_symlink() or sk.resolve() != sk_target.resolve()):
            broken.append(f"{name}/skills")

    if broken:
        return HealthResult(False, "shared-symlinks", f"broken: {', '.join(broken)}")
    return HealthResult(True, "shared-symlinks", f"all {len(profiles)} profiles OK")


def check_xattr_coverage() -> HealthResult:
    """Sample .jsonl files in ~/.claude-shared/projects/ for origin-profile xattr."""
    projects_dir = Path.home() / ".claude-shared" / "projects"
    if not projects_dir.exists():
        return HealthResult(True, "xattr-coverage", "projects dir not found")

    # Collect up to 200 .jsonl files
    files: list[Path] = []
    for f in projects_dir.rglob("*.jsonl"):
        files.append(f)
        if len(files) >= 200:
            break

    if not files:
        return HealthResult(True, "xattr-coverage", "no .jsonl files found")

    tagged = 0
    for f in files:
        try:
            os.getxattr(str(f), b"user.origin-profile")
            tagged += 1
        except OSError:
            pass

    pct = tagged * 100 / len(files)
    detail = f"{pct:.0f}% of {len(files)} sampled files have xattr"
    if pct >= 95:
        return HealthResult(True, "xattr-coverage", detail)
    return HealthResult(False, "xattr-coverage", detail)


def check_hook_integrity() -> HealthResult:
    """Verify hook-stamp-origin contains sentinel and flock patterns."""
    hook = Path.home() / ".claude-common" / "scripts" / "hook-stamp-origin"
    if not hook.exists():
        return HealthResult(False, "hook-integrity", "hook-stamp-origin missing")
    try:
        content = hook.read_text()
    except OSError:
        return HealthResult(False, "hook-integrity", "unreadable")
    issues: list[str] = []
    if ".stamped-" not in content:
        issues.append("no sentinel check")
    if "flock" not in content:
        issues.append("no flock")
    if issues:
        return HealthResult(False, "hook-integrity", "; ".join(issues))
    return HealthResult(True, "hook-integrity", "sentinel + flock present")


def check_hooks_wired() -> HealthResult:
    """Verify each profile's settings.json has hook-timestamp and hook-stamp-origin hooks."""
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

        # Navigate hooks.UserPromptSubmit[0].hooks
        ups_list = settings.get("hooks", {}).get("UserPromptSubmit", [])
        # Collect all command strings from all hook entries
        commands: list[str] = []
        if ups_list and isinstance(ups_list, list):
            first = ups_list[0]
            hooks_list = first.get("hooks", []) if isinstance(first, dict) else []
            for h in hooks_list:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                commands.append(cmd)

        combined = " ".join(commands)
        has_timestamp = "hook-timestamp" in combined
        has_origin = "hook-stamp-origin" in combined
        if not has_timestamp:
            missing.append(f"{name}: missing hook-timestamp")
        if not has_origin:
            missing.append(f"{name}: missing hook-stamp-origin")

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

    if issues:
        return HealthResult(False, "settings-defaults", "; ".join(issues))
    return HealthResult(True, "settings-defaults", f"all {len(profiles)} profiles OK")


def check_token_expiry() -> HealthResult:
    """Warn if any token is approaching 1-year expiry (setup-token TTL)."""
    tokens_file = Path.home() / ".claudelauncher" / "tokens.json"
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
    """Verify each profile has a matching entry in ~/.claudelauncher/tokens.json."""
    tokens_file = Path.home() / ".claudelauncher" / "tokens.json"
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
    """Detect .claude-* dirs that are neither registered profiles nor in options.json.

    A directory is "orphan" if it:
      - lives in ~/ and matches .claude-*
      - is NOT in the known non-profile set (.claude-shared, .claude-common)
      - has no .credentials.json (so _discover_profiles skips it)
      - is NOT listed in options.json's profile values

    For each orphan, we also flag if it contains broken symlinks (symlinks
    whose target does not exist).
    """
    home = Path.home()
    skip = {".claude-shared", ".claude-common"}

    # Registered profiles: dirs that have .credentials.json
    registered = {name for name, _ in _discover_profiles()}

    # Profiles known to options.json (may not have .credentials.json yet)
    options_profiles: set[str] = set()
    try:
        options = json.loads(OPTIONS_FILE.read_text())
        options_profiles = set(options.get("profile", {}).get("values", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    orphans: list[str] = []
    for entry in sorted(home.iterdir()):
        if not entry.name.startswith(".claude-") or entry.name in skip:
            continue
        if not entry.is_dir():
            continue
        name = entry.name[len(".claude-"):]
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
    tokens_file = Path.home() / ".claudelauncher" / "tokens.json"
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
        check_xattr_coverage(),
        check_hooks_wired(),
        check_settings_defaults(),
        check_tokens(),
        check_token_expiry(),
        check_orphan_profiles(),
        check_file_permissions(),
        check_hook_integrity(),
    ]


def print_health_report(results: list[HealthResult]) -> None:
    """Print health check results to stdout."""
    for r in results:
        status = "OK" if r.ok else "WARN"
        print(f"  [{status}] {r.label}: {r.detail}")
