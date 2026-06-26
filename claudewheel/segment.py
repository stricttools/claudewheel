"""Segment and SegmentBar dataclasses, option discovery, and cross-segment constraints."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigManager
from .constants import TOKENS_FILE
from .discovery import discover_profiles
from .fuzzy import fuzzy_rank

NPM_CACHE_TTL = 3600  # 1 hour


def _deduplicate(items: list[str]) -> list[str]:
    """Remove duplicates preserving first occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


@dataclass
class SegmentState:
    """Manages option collections with cache-invalidating mutation methods."""

    _discovered: list[str] = field(default_factory=list)
    _pinned: list[str] = field(default_factory=list)
    _defaults: list[str] = field(default_factory=list)
    _ephemeral: list[str] = field(default_factory=list)
    _installed: set[str] = field(default_factory=set)
    metadata: dict[str, dict] = field(default_factory=dict)
    _options: list[str] | None = field(default=None, repr=False)

    @property
    def options(self) -> list[str]:
        if self._options is None:
            self._options = _deduplicate(
                self._pinned + self._discovered + self._defaults + self._ephemeral
            )
        return self._options

    # -- Mutation methods (each invalidates cache) --

    def set_discovered(self, vals: list[str]) -> None:
        self._discovered = vals
        self._options = None

    def add_pinned(self, val: str) -> None:
        if val not in self._pinned:
            self._pinned.append(val)
            self._options = None

    def remove_pinned(self, val: str) -> None:
        try:
            self._pinned.remove(val)
            self._options = None
        except ValueError:
            pass

    def set_defaults(self, vals: list[str]) -> None:
        self._defaults = vals
        self._options = None

    def add_ephemeral(self, val: str) -> None:
        if val not in self._ephemeral:
            self._ephemeral.append(val)
            self._options = None

    def set_installed(self, vals: set[str]) -> None:
        self._installed = vals

    def set_metadata(self, meta: dict[str, dict]) -> None:
        self.metadata = meta

    def update_metadata(self, partial: dict[str, dict]) -> None:
        self.metadata.update(partial)

    # -- Query methods --

    def source_of(self, val: str) -> str | None:
        if val in self._pinned:
            return "pinned"
        if val in self._discovered:
            return "discovered"
        if val in self._defaults:
            return "defaults"
        if val in self._ephemeral:
            return "ephemeral"
        return None

    def is_installed(self, val: str) -> bool:
        return val in self._installed


@dataclass
class Segment:
    """A single segment in the TUI bar with options, selection state, and search."""

    key: str
    label: str
    state: SegmentState = field(default_factory=SegmentState)
    selected_idx: int = -1  # -1 means nothing selected
    search_buffer: str = ""
    show_options: bool = True
    wrap: bool = True
    min_width: int = 6
    max_width: int = 20
    required: bool = False
    searchable: bool = False
    tab_advances: bool = True
    option_requires: dict[str, dict[str, str]] = field(default_factory=dict)  # value -> {segment_key: constraint}
    unavailable: set[str] = field(default_factory=set)  # dynamically computed per render cycle
    creatable: bool = False  # whether this segment supports inline "+" creation
    freeform: bool = False   # whether typed text can be submitted as a new value directly
    _freeform_editing: bool = False  # True while actively editing a freeform buffer
    creating: bool = False   # True when in creation-mode text input
    create_buffer: str = ""  # text being typed for the new option
    # Bridge field: seeds state._defaults when passed at construction.
    # Callers can pass options= (translated to _init_options by __init__ wrapper).
    _init_options: list[str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._init_options is not None:
            self.state.set_defaults(list(self._init_options))
            self._init_options = None

    @property
    def options(self) -> list[str]:
        return self.state.options

    @property
    def value(self) -> str | None:
        if self.selected_idx < 0 or not self.options:
            return None
        return self.options[self.selected_idx]

    @property
    def filtered_options(self) -> list[str]:
        """Return options filtered by search_buffer using fuzzy matching."""
        if not self.search_buffer:
            return self.options
        return fuzzy_rank(self.search_buffer, self.options)

    def cycle(self, direction: int) -> None:
        """Move selection up (+1) or down (-1) through options.

        The ring has n+1 positions: [-1, 0, 1, ..., n-1] where -1 is the
        blank/unselected state. With wrap=True, cycling continuously rotates
        through all positions including blank. With wrap=False, blank is
        reachable from EITHER end of the option list (UP from first OR
        DOWN from last), but going past blank in either direction stays at
        blank rather than continuing to the other end.
        """
        if not self.options:
            return
        n = len(self.options)
        # Ring of size n+1: positions are -1, 0, 1, ..., n-1
        # Map -1 -> 0, 0 -> 1, ..., n-1 -> n for ring arithmetic
        ring_pos = self.selected_idx + 1  # now in [0, n]
        ring_pos += direction
        if self.wrap:
            ring_pos %= (n + 1)
        else:
            # Off either end -> blank (ring_pos 0). Stay at blank otherwise.
            if ring_pos < 0 or ring_pos > n:
                ring_pos = 0
        self.selected_idx = ring_pos - 1  # back to [-1, n-1]

    @property
    def is_on_plus(self) -> bool:
        """True if the current selection is the '+' creation sentinel."""
        return self.creatable and self.value == "+"

    def select_value(self, val: str) -> bool:
        """Select an option by its string value. Returns True if found."""
        try:
            self.selected_idx = self.options.index(val)
            return True
        except ValueError:
            return False


# Wrap __init__ so callers can pass options= (backward compat bridge).
# Translates options= to _init_options= which seeds state._defaults.
_Segment_orig_init = Segment.__init__

def _Segment_init_wrapper(self, *args, options=None, **kwargs):
    if options is not None:
        kwargs["_init_options"] = options
    _Segment_orig_init(self, *args, **kwargs)

Segment.__init__ = _Segment_init_wrapper


@dataclass
class SegmentBar:
    """Ordered collection of segments with focus tracking and navigation."""

    segments: list[Segment] = field(default_factory=list)
    focus_idx: int = 0

    @property
    def focused(self) -> Segment:
        if not self.segments:
            raise RuntimeError("SegmentBar has no segments -- check enabled_segments config")
        return self.segments[self.focus_idx]

    def move_focus(self, direction: int) -> None:
        if not self.segments:
            return
        n = len(self.segments)
        self.focus_idx = (self.focus_idx + direction) % n

    def get_selections(self) -> dict[str, str | None]:
        return {s.key: s.value for s in self.segments}


def version_sort_key(version: str) -> list[int]:
    """Split a version string on '.' and convert parts to ints for numeric sorting."""
    parts = []
    for part in version.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return parts


def fetch_npm_versions(state: dict, count: int = 15) -> list[str]:
    """Fetch recent Claude Code versions from npm, with 1-hour cache in state."""
    cache = state.get("npm_versions_cache", {})
    cached_at = cache.get("fetched_at", 0)
    cached_versions = cache.get("versions", [])

    if time.time() - cached_at < NPM_CACHE_TTL and cached_versions:
        return cached_versions[-count:]

    try:
        result = subprocess.run(
            ["npm", "view", "@anthropic-ai/claude-code", "versions", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            all_versions = json.loads(result.stdout)
            # Cache the full list
            state["npm_versions_cache"] = {
                "fetched_at": time.time(),
                "versions": all_versions,
            }
            return all_versions[-count:]
    except (Exception, KeyboardInterrupt):
        pass

    # On failure (or Ctrl-C), use cache even if stale
    if cached_versions:
        return cached_versions[-count:]
    return []


def discover_options(options_def: dict, state: dict, *, skip_slow: bool = False) -> dict[str, list[str]]:
    """Resolve option lists, running discovery (directory listing, state merge) as needed."""
    resolved = {}
    for key, opt in options_def.items():
        raw_values = list(opt.get("values", []))
        # Normalize: separate plain string values from objects with requirements
        values = []
        requires: dict[str, dict[str, str]] = {}
        for v in raw_values:
            if isinstance(v, dict):
                values.append(v["value"])
                if "requires" in v:
                    requires[v["value"]] = v["requires"]
            else:
                values.append(v)
        # Store requirements for build_segment_bar to pick up
        if requires:
            resolved[f"_requires_{key}"] = requires
        disc = opt.get("discovery")
        if disc:
            match disc["type"]:
                case "directory_listing":
                    path = Path(disc["path"]).expanduser()
                    if path.is_dir():
                        # Discover versions from filesystem, sorted newest-first
                        entries = sorted(
                            [e.name for e in path.iterdir() if e.is_file()],
                            key=version_sort_key,
                            reverse=True,
                        )
                        values = entries
                case "npm_and_local":
                    local_path = Path(disc["path"]).expanduser()
                    # Get locally installed versions
                    installed = set()
                    if local_path.is_dir():
                        installed = {e.name for e in local_path.iterdir() if e.is_file()}
                    # Get available versions from npm
                    if skip_slow:
                        # Use cached npm versions if warm, otherwise skip
                        cache = state.get("npm_versions_cache", {})
                        cached_at = cache.get("fetched_at", 0)
                        cached_versions = cache.get("versions", [])
                        count = disc.get("count", 15)
                        if time.time() - cached_at < NPM_CACHE_TTL and cached_versions:
                            npm_versions = cached_versions[-count:]
                        else:
                            npm_versions = []
                    else:
                        npm_versions = fetch_npm_versions(state, disc.get("count", 15))
                    # Merge: all npm versions (last N), plus any local-only ones
                    all_versions = list(npm_versions)
                    for v in installed:
                        if v not in all_versions:
                            all_versions.append(v)
                    all_versions.sort(key=version_sort_key, reverse=True)
                    values = all_versions
                    # Store installed set for build_segment_bar to pick up
                    resolved[f"_installed_{key}"] = installed
                case "directory_scan":
                    # Scan parent directories for subdirectories
                    parents = disc.get("parents", [])
                    found: list[str] = []
                    home = Path.home()
                    for parent in parents:
                        parent_path = Path(parent).expanduser()
                        if parent_path.is_dir():
                            for entry in sorted(parent_path.iterdir()):
                                if entry.is_dir() and not entry.name.startswith("."):
                                    # Convert to ~/... format
                                    try:
                                        rel = entry.relative_to(home)
                                        found.append("~/" + str(rel))
                                    except ValueError:
                                        found.append(str(entry))
                    # Merge with recent_dirs from state (recent first, then discovered)
                    state_field = disc.get("state_field")
                    recent = state.get(state_field, []) if state_field else []
                    seen: set[str] = set()
                    merged: list[str] = []
                    for v in recent + found + values:
                        if v not in seen:
                            seen.add(v)
                            merged.append(v)
                    values = merged
                case "claude_config_scan":
                    # Discover Claude profiles via shared discovery
                    discovered = discover_profiles()
                    profiles: list[str] = [p.name for p in discovered]
                    if profiles:
                        values = profiles
                    # Build metadata mapping profile names to config dirs
                    # so launch.py can set CLAUDE_CONFIG_DIR correctly
                    metadata: dict[str, dict[str, str]] = {}
                    for p in discovered:
                        if p.name == "default":
                            metadata["default"] = {"config_dir": "~/.claude"}
                        else:
                            metadata[p.name] = {"config_dir": f"~/.claudewheel/profiles/{p.name}"}
                    opt["metadata"] = metadata
                case "gh_auth":
                    if not skip_slow:
                        # Discover GitHub accounts from gh CLI auth status
                        try:
                            result = subprocess.run(
                                ["gh", "auth", "status"],
                                capture_output=True, text=True, timeout=5,
                            )
                            output = result.stdout + result.stderr
                            accounts = re.findall(
                                r"Logged in to github\.com account (\S+)", output,
                            )
                            if accounts:
                                # Deduplicate while preserving order
                                seen_accts: set[str] = set()
                                for acct in accounts:
                                    if acct not in seen_accts:
                                        seen_accts.add(acct)
                                        values.append(acct)
                        except (FileNotFoundError, subprocess.TimeoutExpired):
                            pass
                case "state_field":
                    # Merge state-tracked values with static defaults, preserving order
                    state_values = state.get(disc["field"], [])
                    seen: set[str] = set()
                    merged: list[str] = []
                    for v in state_values + values:
                        if v not in seen:
                            seen.add(v)
                            merged.append(v)
                    values = merged
        resolved[key] = values
    return resolved


def run_slow_discovery(options_def: dict, state: dict) -> dict[str, list[str]]:
    """Run only the slow discovery types (gh_auth, npm_and_local). Thread-safe: reads only."""
    result: dict[str, list[str]] = {}
    for key, opt in options_def.items():
        disc = opt.get("discovery")
        if not disc:
            continue
        match disc["type"]:
            case "gh_auth":
                raw_values = list(opt.get("values", []))
                values = []
                for v in raw_values:
                    if isinstance(v, dict):
                        values.append(v["value"])
                    else:
                        values.append(v)
                try:
                    proc = subprocess.run(
                        ["gh", "auth", "status"],
                        capture_output=True, text=True, timeout=5,
                    )
                    output = proc.stdout + proc.stderr
                    accounts = re.findall(
                        r"Logged in to github\.com account (\S+)", output,
                    )
                    if accounts:
                        seen_accts: set[str] = set()
                        for acct in accounts:
                            if acct not in seen_accts:
                                seen_accts.add(acct)
                                values.append(acct)
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                result[key] = values
            case "npm_and_local":
                local_path = Path(disc["path"]).expanduser()
                installed = set()
                if local_path.is_dir():
                    installed = {e.name for e in local_path.iterdir() if e.is_file()}
                npm_versions = fetch_npm_versions(state, disc.get("count", 15))
                all_versions = list(npm_versions)
                for v in installed:
                    if v not in all_versions:
                        all_versions.append(v)
                all_versions.sort(key=version_sort_key, reverse=True)
                result[key] = all_versions
                result[f"_installed_{key}"] = installed
    return result


def build_segment_bar(cfg: ConfigManager, *, skip_slow: bool = False) -> SegmentBar:
    """Construct the segment bar from config, applying discovery and last-state restore."""
    enabled = cfg.config.get("enabled_segments", [])
    resolved = discover_options(cfg.options_def, cfg.state, skip_slow=skip_slow)
    # Persist npm cache to state.json so it survives even if the user quits without launching
    cfg.save_state()
    segments: list[Segment] = []

    from .defaults import DEFAULT_OPTIONS
    last = cfg.state.get("last_config", {})

    for sdef in cfg.segments_def:
        key = sdef["key"]
        if key not in enabled:
            continue
        opt = cfg.options_def.get(key, {})
        seg = Segment(
            key=key,
            label=sdef["label"],
            show_options=sdef.get("show_options", True),
            wrap=sdef.get("wrap", True),
            min_width=sdef.get("min_width", 6),
            max_width=sdef.get("max_width", 20),
            required=sdef.get("required", False),
            searchable=sdef.get("searchable", False),
            tab_advances=sdef.get("tab_advances", True),
            creatable=sdef.get("creatable", False),
            freeform=sdef.get("freeform", False),
        )
        # Populate state collections
        seg.state.set_defaults(DEFAULT_OPTIONS.get(key, {}).get("values", []))
        for pinned_val in opt.get("pinned", []):
            seg.state.add_pinned(pinned_val)
        seg.state.set_discovered(resolved.get(key, []))
        # Seed metadata from persisted options (includes discovery metadata
        # written back to opt by claude_config_scan and similar discovery types)
        seg.state.set_metadata(dict(opt.get("metadata", {})))
        # Installed set and option requirements
        installed_key = f"_installed_{key}"
        if installed_key in resolved:
            seg.state.set_installed(resolved[installed_key])
        requires_key = f"_requires_{key}"
        if requires_key in resolved:
            seg.option_requires = resolved[requires_key]
        # "+" sentinel for creatable segments (bridge until Phase 7)
        if seg.creatable:
            seg.state.add_ephemeral("+")
        # Pre-select from last session's config if available
        if key in last:
            seg.select_value(last[key])
        segments.append(seg)

    # Auto-detect cwd for the directory segment if nothing was restored
    try:
        cwd = Path.cwd()
        home = Path.home()
        rel = cwd.relative_to(home)
        # When cwd IS home, relative_to returns '.', which would give "~/."
        cwd_tilde = "~" if str(rel) == "." else "~/" + str(rel)
        for seg in segments:
            if seg.key == "directory" and seg.selected_idx < 0:
                seg.select_value(cwd_tilde)
    except ValueError:
        pass

    if not segments:
        raise RuntimeError(
            "No segments enabled -- check enabled_segments in config.json"
        )

    return SegmentBar(segments=segments)


def merge_slow_results(bar: SegmentBar, results: dict[str, list[str]], state: dict) -> None:
    """Merge background discovery results into the live segment bar.

    For each segment with new options in *results*, update its discovered list
    via SegmentState, update the installed set, and restore the previous
    selection (falling back to last_config from *state*).
    """
    for seg in bar.segments:
        if seg.key not in results:
            continue
        # Remember current selection
        current_value = seg.value
        # Update discovered options via state (cache auto-invalidates)
        seg.state.set_discovered(results[seg.key])
        # Attach installed set if discovery produced one
        installed_key = f"_installed_{seg.key}"
        if installed_key in results:
            seg.state.set_installed(results[installed_key])
        # "+" is already in ephemeral from build time, no need to re-append
        # Restore selection
        if current_value is not None:
            seg.select_value(current_value)
        elif seg.key in state.get("last_config", {}):
            # If user hasn't made a selection yet, try last_config
            seg.select_value(state["last_config"][seg.key])


def evaluate_requires(bar: SegmentBar) -> None:
    """Recompute unavailable sets based on cross-segment requirements."""
    selections = bar.get_selections()
    for seg in bar.segments:
        unavailable: set[str] = set()
        for opt_value, reqs in seg.option_requires.items():
            for req_segment, constraint in reqs.items():
                current_value = selections.get(req_segment)
                if not _satisfies_constraint(current_value, constraint):
                    unavailable.add(opt_value)
                    break
        seg.unavailable = unavailable


def _satisfies_constraint(value: str | None, constraint: str) -> bool:
    """Check if a value satisfies a version constraint like '>=2.1.110'."""
    if value is None:
        return False
    if constraint.startswith(">="):
        return version_sort_key(value) >= version_sort_key(constraint[2:])
    elif constraint.startswith("<="):
        return version_sort_key(value) <= version_sort_key(constraint[2:])
    elif constraint.startswith(">"):
        return version_sort_key(value) > version_sort_key(constraint[1:])
    elif constraint.startswith("<"):
        return version_sort_key(value) < version_sort_key(constraint[1:])
    else:
        return value == constraint
