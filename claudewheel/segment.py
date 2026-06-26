"""Segment and SegmentBar dataclasses, option discovery, and cross-segment constraints."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigManager
from .discovery import discover_profiles
from .fuzzy import fuzzy_rank

NPM_CACHE_TTL = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Discovery dataclasses and registry
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryResult:
    """Structured result from a discovery function."""

    values: list[str] = field(default_factory=list)
    installed: set[str] = field(default_factory=set)
    requires: dict[str, dict[str, str]] = field(default_factory=dict)
    metadata: dict[str, dict] = field(default_factory=dict)


@dataclass
class DiscoveryEntry:
    """Registry entry mapping a discovery type to its function."""

    func: Callable  # (config: dict, state: dict) -> DiscoveryResult
    is_slow: bool = False
    verify: Callable | None = None  # for staleness checks (Phase 4)


def _deduplicate(items: list[str]) -> list[str]:
    """Remove duplicates preserving first occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


_COLLECTION_MAP = {
    "pinned": "_pinned",
    "discovered": "_discovered",
    "defaults": "_defaults",
}

_DEFAULT_COLLECTION_ORDER = ["pinned", "discovered", "defaults"]


@dataclass
class SegmentState:
    """Manages option collections with cache-invalidating mutation methods."""

    _discovered: list[str] = field(default_factory=list)
    _pinned: list[str] = field(default_factory=list)
    _defaults: list[str] = field(default_factory=list)
    _ephemeral: list[str] = field(default_factory=list)
    _installed: set[str] = field(default_factory=set)
    metadata: dict[str, dict] = field(default_factory=dict)
    collection_order: list[str] = field(default_factory=lambda: list(_DEFAULT_COLLECTION_ORDER))
    sort: str | None = None
    _options: list[str] | None = field(default=None, repr=False)

    @property
    def options(self) -> list[str]:
        if self._options is None:
            # Build ordered list from collection_order
            ordered: list[str] = []
            for name in self.collection_order:
                attr = _COLLECTION_MAP.get(name)
                if attr:
                    ordered.extend(getattr(self, attr))
            # Deduplicate (preserving first occurrence)
            deduped = _deduplicate(ordered)
            # Apply sort if configured
            if self.sort == "semver_desc":
                deduped.sort(key=version_sort_key, reverse=True)
            # Ephemeral always appended at the end (after sort)
            deduped = _deduplicate(deduped + self._ephemeral)
            self._options = deduped
        return self._options

    # -- Mutation methods (each invalidates cache) --

    def set_discovered(self, vals: list[str], *, verify_fn: Callable | None = None) -> None:
        if verify_fn is not None:
            # VERIFY policy: values removed from new list are checked before dropping
            new_set = set(vals)
            old_only = [v for v in self._discovered if v not in new_set]
            kept = [v for v in old_only if verify_fn(v)]
            # New values first, then verified survivors (preserves new list order)
            self._discovered = vals + kept
        else:
            # IMMEDIATE policy: new list fully replaces old
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
    selected_value: str | None = None  # None means nothing selected
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
    def selected_idx(self) -> int:
        """Computed index of selected_value in options, or -1 if unselected."""
        if self.selected_value is None:
            return -1
        try:
            return self.options.index(self.selected_value)
        except ValueError:
            return -1

    @property
    def value(self) -> str | None:
        if self.selected_value is None or self.selected_value not in self.options:
            return None
        return self.selected_value

    @property
    def filtered_options(self) -> list[str]:
        """Return options filtered by search_buffer using fuzzy matching."""
        if not self.search_buffer:
            return self.options
        return fuzzy_rank(self.search_buffer, self.options)

    def cycle(self, direction: int) -> None:
        """Move selection up (+1) or down (-1) through options.

        The ring has n+1 positions: [None, 0, 1, ..., n-1] where None is the
        blank/unselected state. With wrap=True, cycling continuously rotates
        through all positions including blank. With wrap=False, blank is
        reachable from EITHER end of the option list (UP from first OR
        DOWN from last), but going past blank in either direction stays at
        blank rather than continuing to the other end.
        """
        if not self.options:
            return
        n = len(self.options)
        # Resolve current position via computed property
        idx = self.selected_idx
        # Ring of size n+1: positions are -1, 0, 1, ..., n-1
        # Map -1 -> 0, 0 -> 1, ..., n-1 -> n for ring arithmetic
        ring_pos = idx + 1  # now in [0, n]
        ring_pos += direction
        if self.wrap:
            ring_pos %= (n + 1)
        else:
            # Off either end -> blank (ring_pos 0). Stay at blank otherwise.
            if ring_pos < 0 or ring_pos > n:
                ring_pos = 0
        # Convert back to value
        if ring_pos == 0:
            self.selected_value = None
        else:
            self.selected_value = self.options[ring_pos - 1]

    @property
    def is_on_plus(self) -> bool:
        """True if the current selection is the '+' creation sentinel."""
        return self.creatable and self.value == "+"

    def select_value(self, val: str) -> bool:
        """Select an option by its string value. Returns True if found."""
        if val in self.options:
            self.selected_value = val
            return True
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


# ---------------------------------------------------------------------------
# Individual discovery functions -- extracted from discover_options match/case
# ---------------------------------------------------------------------------

def _discover_directory_listing(config: dict, state: dict) -> DiscoveryResult:
    """Discover options from a directory of files (e.g., installed versions)."""
    disc = config["discovery"]
    path = Path(disc["path"]).expanduser()
    if path.is_dir():
        entries = sorted(
            [e.name for e in path.iterdir() if e.is_file()],
            key=version_sort_key,
            reverse=True,
        )
        return DiscoveryResult(values=entries)
    return DiscoveryResult()


def _discover_npm_and_local(config: dict, state: dict) -> DiscoveryResult:
    """Discover versions from npm registry + locally installed files."""
    disc = config["discovery"]
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
    return DiscoveryResult(values=all_versions, installed=installed)


def _discover_npm_and_local_cached(config: dict, state: dict) -> DiscoveryResult:
    """Fast path for npm_and_local: use cached npm versions only if warm."""
    disc = config["discovery"]
    local_path = Path(disc["path"]).expanduser()
    installed = set()
    if local_path.is_dir():
        installed = {e.name for e in local_path.iterdir() if e.is_file()}
    cache = state.get("npm_versions_cache", {})
    cached_at = cache.get("fetched_at", 0)
    cached_versions = cache.get("versions", [])
    count = disc.get("count", 15)
    if time.time() - cached_at < NPM_CACHE_TTL and cached_versions:
        npm_versions = cached_versions[-count:]
    else:
        npm_versions = []
    all_versions = list(npm_versions)
    for v in installed:
        if v not in all_versions:
            all_versions.append(v)
    all_versions.sort(key=version_sort_key, reverse=True)
    return DiscoveryResult(values=all_versions, installed=installed)


def _discover_directory_scan(config: dict, state: dict) -> DiscoveryResult:
    """Discover directories by scanning parent directories.

    Recent dirs from state are used as hints: validated (must exist on disk),
    emitted first in the result, and pruned back to state (stale entries removed).
    Static values from options.json are NOT included -- they are handled by
    SegmentState.defaults via the defaults collection.
    """
    disc = config["discovery"]
    parents = disc.get("parents", [])
    found: list[str] = []
    home = Path.home()
    for parent in parents:
        parent_path = Path(parent).expanduser()
        if parent_path.is_dir():
            for entry in sorted(parent_path.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    try:
                        rel = entry.relative_to(home)
                        found.append("~/" + str(rel))
                    except ValueError:
                        found.append(str(entry))
    # Validate recent_dirs from state as hints (filter to existing dirs)
    state_field = disc.get("state_field")
    recent: list[str] = state.get(state_field, []) if state_field else []
    validated_recent: list[str] = [
        p for p in recent if Path(p).expanduser().is_dir()
    ]
    # Prune stale entries from state
    if state_field and state_field in state:
        state[state_field] = validated_recent
    # Merge: validated recent first, then parent-scan results (deduped)
    seen: set[str] = set()
    merged: list[str] = []
    for v in validated_recent + found:
        if v not in seen:
            seen.add(v)
            merged.append(v)
    return DiscoveryResult(values=merged)


def _discover_profiles(config: dict, state: dict) -> DiscoveryResult:
    """Discover Claude Code profiles via filesystem scan."""
    discovered = discover_profiles()
    profiles: list[str] = [p.name for p in discovered]
    metadata: dict[str, dict] = {}
    for p in discovered:
        if p.name == "default":
            metadata["default"] = {"config_dir": "~/.claude"}
        else:
            metadata[p.name] = {"config_dir": f"~/.claudewheel/profiles/{p.name}"}
    return DiscoveryResult(values=profiles, metadata=metadata)


def _discover_gh_accounts(config: dict, state: dict) -> DiscoveryResult:
    """Discover GitHub accounts from gh CLI auth status."""
    static_values = _parse_static_values(config)
    values = list(static_values)
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
            seen_accts: set[str] = set(values)
            for acct in accounts:
                if acct not in seen_accts:
                    seen_accts.add(acct)
                    values.append(acct)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return DiscoveryResult(values=values)


def _discover_state_field(config: dict, state: dict) -> DiscoveryResult:
    """Discover options by merging state-tracked values with static defaults."""
    disc = config["discovery"]
    static_values = _parse_static_values(config)
    state_values = state.get(disc["field"], [])
    seen: set[str] = set()
    merged: list[str] = []
    for v in state_values + static_values:
        if v not in seen:
            seen.add(v)
            merged.append(v)
    return DiscoveryResult(values=merged)


def _parse_static_values(config: dict) -> list[str]:
    """Extract plain string values from an options_def entry, stripping requires dicts."""
    raw = config.get("values", [])
    values: list[str] = []
    for v in raw:
        if isinstance(v, dict):
            values.append(v["value"])
        else:
            values.append(v)
    return values


def _parse_requires(config: dict) -> dict[str, dict[str, str]]:
    """Extract requires constraints from dict-style values in an options_def entry."""
    raw = config.get("values", [])
    requires: dict[str, dict[str, str]] = {}
    for v in raw:
        if isinstance(v, dict) and "requires" in v:
            requires[v["value"]] = v["requires"]
    return requires


# ---------------------------------------------------------------------------
# Discovery registry
# ---------------------------------------------------------------------------

DISCOVERY_REGISTRY: dict[str, DiscoveryEntry] = {
    "directory_listing": DiscoveryEntry(
        func=_discover_directory_listing,
        verify=lambda val, config: Path(config.get("discovery", {}).get("path", "")).expanduser().joinpath(val).is_file(),
    ),
    "npm_and_local": DiscoveryEntry(
        func=_discover_npm_and_local,
        is_slow=True,
        verify=lambda val, config: Path(config.get("discovery", {}).get("path", "")).expanduser().joinpath(val).is_dir(),
    ),
    "directory_scan": DiscoveryEntry(
        func=_discover_directory_scan,
        verify=lambda val, config: Path(val).expanduser().is_dir(),
    ),
    "claude_config_scan": DiscoveryEntry(func=_discover_profiles),  # IMMEDIATE
    "gh_auth": DiscoveryEntry(func=_discover_gh_accounts, is_slow=True),  # IMMEDIATE
    "state_field": DiscoveryEntry(func=_discover_state_field),  # no staleness check
}


# ---------------------------------------------------------------------------
# Registry-based discovery dispatch
# ---------------------------------------------------------------------------

def run_slow_discovery_via_registry(
    options_def: dict, state: dict,
) -> dict[str, DiscoveryResult]:
    """Run only slow discovery types via the registry. Thread-safe: reads only."""
    results: dict[str, DiscoveryResult] = {}
    for key, opt in options_def.items():
        disc = opt.get("discovery")
        if not disc:
            continue
        dtype = disc["type"]
        entry = DISCOVERY_REGISTRY.get(dtype)
        if entry and entry.is_slow:
            results[key] = entry.func(opt, state)
    return results


def populate_segment_state(
    seg: "Segment",
    options_def_entry: dict,
    state: dict,
    *,
    skip_slow: bool = True,
) -> None:
    """Populate a segment's state from discovery and static config.

    Looks up the discovery config, calls the registry function (unless slow
    and skip_slow is True), and writes results to seg.state.
    """
    disc = options_def_entry.get("discovery")
    requires = _parse_requires(options_def_entry)
    if requires:
        seg.option_requires = requires

    if not disc:
        return

    dtype = disc["type"]
    entry = DISCOVERY_REGISTRY.get(dtype)
    if not entry:
        return

    # Build verify_fn closure if the entry has a verify callback
    verify_fn = None
    if entry.verify:
        verify_fn = lambda val, _e=entry, _c=options_def_entry: _e.verify(val, _c)

    if entry.is_slow and skip_slow:
        # For npm_and_local, use the cached fast-path
        if dtype == "npm_and_local":
            result = _discover_npm_and_local_cached(options_def_entry, state)
            seg.state.set_discovered(result.values, verify_fn=verify_fn)
            if result.installed:
                seg.state.set_installed(result.installed)
        # Other slow types (gh_auth) produce nothing at startup
        return

    result = entry.func(options_def_entry, state)
    seg.state.set_discovered(result.values, verify_fn=verify_fn)
    if result.installed:
        seg.state.set_installed(result.installed)
    if result.metadata:
        seg.state.update_metadata(result.metadata)


# Per-segment merge specs: collection_order and sort overrides
_SEGMENT_MERGE_SPECS: dict[str, dict] = {
    "version": {"sort": "semver_desc"},
    "profile": {"collection_order": ["discovered"]},
    "model": {"collection_order": ["pinned", "defaults"]},
    "mcp": {"collection_order": ["pinned", "defaults"]},
    "permissions": {"collection_order": ["pinned", "defaults"]},
    "github": {"collection_order": ["pinned", "discovered"]},
    # directory uses default: ["pinned", "discovered", "defaults"]
}


def build_segment_bar(cfg: ConfigManager, *, skip_slow: bool = False) -> SegmentBar:
    """Construct the segment bar from config, applying discovery and last-state restore."""
    enabled = cfg.config.get("enabled_segments", [])
    segments: list[Segment] = []

    from .defaults import DEFAULT_OPTIONS
    last = cfg.state.get("last_config", {})

    for sdef in cfg.segments_def:
        key = sdef["key"]
        if key not in enabled:
            continue
        opt = cfg.options_def.get(key, {})

        # Apply per-segment merge spec
        merge_spec = _SEGMENT_MERGE_SPECS.get(key, {})

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

        # Configure merge spec on state
        if "collection_order" in merge_spec:
            seg.state.collection_order = list(merge_spec["collection_order"])
        if "sort" in merge_spec:
            seg.state.sort = merge_spec["sort"]

        # Populate defaults and pinned from config
        seg.state.set_defaults(DEFAULT_OPTIONS.get(key, {}).get("values", []))
        for pinned_val in opt.get("pinned", []):
            seg.state.add_pinned(pinned_val)

        # Seed metadata from persisted options
        seg.state.set_metadata(dict(opt.get("metadata", {})))

        # Run discovery via registry
        populate_segment_state(seg, opt, cfg.state, skip_slow=skip_slow)

        # "+" sentinel for creatable segments (bridge until Phase 7)
        if seg.creatable:
            seg.state.add_ephemeral("+")

        # Pre-select from last session's config if available
        if key in last:
            seg.select_value(last[key])
        segments.append(seg)

    # Persist npm cache to state.json so it survives even if the user quits without launching
    cfg.save_state()

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


def merge_slow_results(
    bar: SegmentBar,
    results: dict[str, DiscoveryResult],
    state: dict,
    options_def: dict | None = None,
) -> None:
    """Merge background discovery results into the live segment bar.

    For each segment with new options in *results*, update its discovered list
    via SegmentState, update the installed set, and restore the previous
    selection (falling back to last_config from *state*).

    When *options_def* is provided, staleness verify callbacks from the
    discovery registry are wired through so values that still exist on disk
    are not prematurely dropped.
    """
    for seg in bar.segments:
        if seg.key not in results:
            continue
        dr = results[seg.key]
        # Build verify_fn from registry if options_def available
        verify_fn = None
        if options_def is not None:
            opt = options_def.get(seg.key, {})
            disc = opt.get("discovery")
            if disc:
                entry = DISCOVERY_REGISTRY.get(disc["type"])
                if entry and entry.verify:
                    verify_fn = lambda val, _e=entry, _c=opt: _e.verify(val, _c)
        # Remember current selection
        current_value = seg.value
        # Update discovered options via state (cache auto-invalidates)
        seg.state.set_discovered(dr.values, verify_fn=verify_fn)
        # Attach installed set if discovery produced one
        if dr.installed:
            seg.state.set_installed(dr.installed)
        # Update metadata if discovery produced any
        if dr.metadata:
            seg.state.update_metadata(dr.metadata)
        # "+" is already in ephemeral from build time, no need to re-append
        # Restore selection: try current value first, fall back to last_config
        restored = False
        if current_value is not None:
            restored = seg.select_value(current_value)
        if not restored and seg.key in state.get("last_config", {}):
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
