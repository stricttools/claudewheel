"""TUI event loop, keyboard dispatch, and segment interaction."""

from __future__ import annotations

import copy
import signal
import sys
import threading

from .config import ConfigManager
from .segment import DiscoveryResult, Segment, build_segment_bar, evaluate_requires, merge_slow_results, run_slow_discovery_via_registry, _discover_profiles, _update_auth_from_metadata
from .terminal import Terminal
from .theme import parse_theme
from .renderer import Renderer


class App:
    """TUI application managing the event loop, keyboard handling, and segment interaction."""

    def __init__(self, cfg: ConfigManager | None = None, overrides: dict[str, str] | None = None):
        self.cfg = cfg if cfg is not None else ConfigManager()
        self.bar = build_segment_bar(self.cfg, skip_slow=True)
        # Apply CLI arg overrides (after last_config pre-fill, before TUI)
        if overrides:
            for seg in self.bar.segments:
                if seg.key in overrides:
                    val = overrides[seg.key]
                    if not seg.select_value(val) and seg.freeform:
                        # Freeform segment: add the value if not already in options
                        seg.state.add_ephemeral(val)
                        seg.select_value(val)
        # Start slow discovery in background thread
        self._slow_results: dict[str, DiscoveryResult] | None = None
        self._slow_state_copy: dict | None = None  # isolated copy for bg thread
        # Deferred discovery results for the focused segment (Phase 8)
        self._pending_discovery: dict[str, DiscoveryResult] = {}
        self._slow_thread = threading.Thread(
            target=self._run_slow_discovery_thread,
            daemon=True,
        )
        self._slow_thread.start()
        self.terminal = Terminal()
        self.theme = parse_theme(self.cfg.theme)
        self.renderer = Renderer(
            self.terminal, self.theme,
            minimap_mode=self.cfg.config.get("minimap", "auto"),
        )
        self.running = False
        self._flash: str = ""  # Temporary message shown for one render cycle
        self._pending_install: str | None = None  # version awaiting install confirmation
        self._pending_install_seg: Segment | None = None
        self._show_provenance: bool = False  # Phase 9: provenance overlay toggle

    def run_tui(self) -> dict[str, str | None] | None:
        """Enter the TUI loop. Returns selections on launch, None on quit."""
        self.running = True
        self.terminal.enter_raw()

        def on_resize(signum, frame):
            self.terminal.rows, self.terminal.cols = self.terminal.get_size()
            self.renderer.render(self.bar)

        signal.signal(signal.SIGWINCH, on_resize)

        def on_term(signum, frame):
            self.terminal.exit_raw()
            sys.exit(1)

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGHUP, on_term)

        try:
            evaluate_requires(self.bar)
            self.renderer.render(self.bar, show_provenance=self._show_provenance)
            while self.running:
                try:
                    key = self.terminal.read_key()
                except KeyboardInterrupt:
                    return None
                action = self._handle_key(key)
                if action == "launch":
                    self._promote_ephemeral()
                    return self.bar.get_selections()
                elif action == "quit":
                    return None
                # Check if background discovery finished
                if self._slow_results is not None and not self._slow_thread.is_alive():
                    self._apply_slow_discovery()
                evaluate_requires(self.bar)
                self.renderer.render(self.bar, self._flash, show_provenance=self._show_provenance)
                self._flash = ""  # Clear flash after one render cycle
        finally:
            self.terminal.exit_raw()

    def _promote_ephemeral(self) -> None:
        """Promote ephemeral selections to pinned on disk before launch."""
        for seg in self.bar.segments:
            val = seg.selected_value
            if val is not None and seg.state.source_of(val) == "ephemeral":
                self.cfg.add_option(seg.key, val)
                seg.state.add_pinned(val)

    def _run_slow_discovery_thread(self) -> None:
        """Background thread: run slow discovery and store results."""
        state_copy = copy.deepcopy(self.cfg.state)
        self._slow_state_copy = state_copy
        self._slow_results = run_slow_discovery_via_registry(self.cfg.options_def, state_copy)

    def _apply_slow_discovery(self) -> None:
        """Merge slow discovery results into the live segment bar.

        Results for the focused segment are deferred to avoid disrupting the
        user's current interaction.  Unfocused segments are updated immediately.

        Known limitation: while results are buffered, evaluate_requires() uses
        the focused segment's pre-discovery options for cross-segment constraint
        checks. Constraint-based dimming may be briefly stale until defocus.
        """
        results = self._slow_results
        if results is None:
            return
        self._slow_results = None  # Consume results once

        focused_key = self.bar.focused.key

        # Split results: immediate for unfocused, deferred for focused
        immediate: dict[str, DiscoveryResult] = {}
        for key, dr in results.items():
            if key == focused_key:
                self._pending_discovery[key] = dr
                # Mark the segment so the renderer can show an indicator
                for seg in self.bar.segments:
                    if seg.key == key:
                        seg.has_pending = True
                        break
            else:
                immediate[key] = dr

        if immediate:
            merge_slow_results(self.bar, immediate, self.cfg.state, options_def=self.cfg.options_def)

        # Copy npm cache from the isolated state copy back to the live state
        if self._slow_state_copy:
            self.cfg.state["npm_versions_cache"] = self._slow_state_copy.get("npm_versions_cache", {})
            self._slow_state_copy = None
        self.cfg.save_state()

    def _apply_pending_for_segment(self, seg: Segment) -> None:
        """Apply any deferred discovery results for *seg* and clear pending state."""
        if seg.key not in self._pending_discovery:
            return
        dr = self._pending_discovery.pop(seg.key)
        merge_slow_results(
            self.bar,
            {seg.key: dr},
            self.cfg.state,
            options_def=self.cfg.options_def,
        )
        seg.has_pending = False

    def _defocus(self) -> None:
        """Run deferred-apply housekeeping on the segment about to lose focus."""
        focused = self.bar.focused
        self._apply_pending_for_segment(focused)
        # Clear freeform editing state
        focused.search_buffer = ""
        focused._freeform_editing = False

    def _handle_key(self, key: str) -> str | None:
        """Process a keypress and return an action string or None to continue."""
        focused = self.bar.focused

        # Creation mode intercepts all keys
        if focused.creating:
            return self._handle_create_key(key, focused)

        # Freeform editing mode: route all keys to the freeform handler which
        # supports LEFT/RIGHT (exit + navigate) and BACKSPACE (exit when empty).
        if focused.freeform and focused.search_buffer and focused._freeform_editing:
            return self._handle_freeform_key(key, focused)

        # Pending install confirmation
        if self._pending_install:
            return self._handle_install_key(key)

        match key:
            case "LEFT" | "SHIFT_TAB":
                self._defocus()
                self.bar.move_focus(-1)
            case "RIGHT":
                self._defocus()
                self.bar.move_focus(1)
            case "UP":
                if focused.search_buffer:
                    focused.search_buffer = ""
                focused._freeform_editing = False
                focused.cycle(-1)
            case "DOWN":
                if focused.search_buffer:
                    focused.search_buffer = ""
                focused._freeform_editing = False
                focused.cycle(1)
            case "ENTER":
                # "+" on profile segment launches the wizard
                if focused.is_on_plus and focused.key == "profile":
                    return self._launch_profile_wizard(focused)
                # Enter creation mode if on the "+" sentinel (other segments)
                if focused.is_on_plus:
                    focused.creating = True
                    focused.create_buffer = ""
                    return None
                # Check for required segments without a selection
                missing = [
                    s.label
                    for s in self.bar.segments
                    if s.required and s.value is None
                ]
                if missing:
                    self._flash = f"Required: {', '.join(missing)}"
                    return None
                # Check for non-installed version -- offer to install
                for s in self.bar.segments:
                    if s.value and s.state.has_installed and not s.state.is_installed(s.value):
                        self._pending_install = s.value
                        self._pending_install_seg = s
                        self._flash = f"{s.value} not on disk. Enter=install, Esc=cancel"
                        return None
                # Check for unavailable selections
                for s in self.bar.segments:
                    if s.value and s.value in s.unavailable:
                        self._flash = f"{s.label}: {s.value} not available for this version"
                        return None
                # Check for unauthenticated profile -- intercept and offer auth
                for s in self.bar.segments:
                    if s.key == "profile" and s.value:
                        if s.state.has_auth_status and not s.state.is_authenticated(s.value):
                            self._intercept_unauth(s)
                        break
                return "launch"
            case "TAB":
                if focused.is_on_plus and focused.key == "profile":
                    return self._launch_profile_wizard(focused)
                # Enter creation mode if on the "+" sentinel (other segments)
                if focused.is_on_plus:
                    focused.creating = True
                    focused.create_buffer = ""
                    return None
                if focused.searchable and focused.search_buffer:
                    # Accept the top fuzzy match
                    matches = focused.filtered_options
                    if matches:
                        focused.select_value(matches[0])
                    focused.search_buffer = ""
                    if focused.tab_advances:
                        self._apply_pending_for_segment(focused)
                        self.bar.move_focus(1)
                elif focused.tab_advances:
                    self._apply_pending_for_segment(focused)
                    self.bar.move_focus(1)
            case "BACKSPACE":
                if focused.freeform and not focused._freeform_editing and focused.value:
                    # First backspace on a freeform segment: seed buffer from current value
                    trimmed = focused.value[:-1]
                    if trimmed:
                        focused.search_buffer = trimmed
                        focused._freeform_editing = True
                elif focused.searchable and focused.search_buffer:
                    focused.search_buffer = focused.search_buffer[:-1]
            case "ESC":
                focused.search_buffer = ""
                focused._freeform_editing = False
            case "CTRL_C":
                return "quit"
            case _:
                # Single printable characters
                if len(key) == 1 and key.isprintable():
                    # Provenance overlay toggle (only when not actively searching)
                    if key == "?" and not focused.search_buffer:
                        self._show_provenance = not self._show_provenance
                    elif focused.freeform and not focused._freeform_editing and focused.value:
                        # First keypress on a freeform segment: seed buffer from current value
                        focused.search_buffer = focused.value + key
                        focused._freeform_editing = True
                    elif focused.searchable and (focused.search_buffer or key != "q"):
                        focused.search_buffer += key
                    elif key == "q":
                        return "quit"
        return None

    def _handle_freeform_key(self, key: str, seg: Segment) -> str | None:
        """Handle keystrokes in freeform editing mode (search buffer active on a freeform segment)."""
        match key:
            case "ENTER":
                # Submit the typed text as the value
                text = seg.search_buffer.strip()
                if text:
                    seg.state.add_ephemeral(text)
                    seg.select_value(text)
                seg.search_buffer = ""
                seg._freeform_editing = False
                if seg.tab_advances:
                    self._apply_pending_for_segment(seg)
                    self.bar.move_focus(1)
                return None
            case "TAB":
                # Accept the top fuzzy match (not the raw text)
                matches = seg.filtered_options
                if matches:
                    seg.select_value(matches[0])
                seg.search_buffer = ""
                seg._freeform_editing = False
                if seg.tab_advances:
                    self._apply_pending_for_segment(seg)
                    self.bar.move_focus(1)
                return None
            case "BACKSPACE":
                seg.search_buffer = seg.search_buffer[:-1]
                if not seg.search_buffer:
                    seg._freeform_editing = False
                return None
            case "LEFT":
                seg.search_buffer = ""
                seg._freeform_editing = False
                self._apply_pending_for_segment(seg)
                self.bar.move_focus(-1)
                return None
            case "RIGHT":
                seg.search_buffer = ""
                seg._freeform_editing = False
                self._apply_pending_for_segment(seg)
                self.bar.move_focus(1)
                return None
            case "ESC":
                seg.search_buffer = ""
                seg._freeform_editing = False
                return None
            case "CTRL_C":
                seg.search_buffer = ""
                seg._freeform_editing = False
                return "quit"
            case _:
                if len(key) == 1 and key.isprintable():
                    seg.search_buffer += key
        return None

    def _handle_install_key(self, key: str) -> str | None:
        """Handle keystrokes during install confirmation."""
        version = self._pending_install
        seg = self._pending_install_seg
        self._pending_install = None
        self._pending_install_seg = None

        if key != "ENTER" or not version or not seg:
            return None

        from .install import install_version

        # Exit alt screen so the user sees download progress
        self.terminal.exit_raw()
        print(f"Downloading Claude Code {version}...")

        def on_progress(downloaded: int, total: int) -> None:
            if total > 0:
                mb_done = downloaded / (1024 * 1024)
                mb_total = total / (1024 * 1024)
                pct = downloaded * 100 // total
                print(f"\r  {mb_done:.0f}/{mb_total:.0f} MB ({pct}%)", end="", flush=True)

        try:
            install_version(version, progress_callback=on_progress)
            print(f"\nInstalled {version} successfully. Press Enter to continue...")
            seg.state.mark_installed(version)
        except OSError as e:
            print(f"\nInstallation failed: {e}")
            print("Press Enter to continue...")
        try:
            input()
        except KeyboardInterrupt:
            pass
        # Re-enter alt screen
        self.terminal.enter_raw()
        return None

    def _intercept_unauth(self, seg: Segment) -> None:
        """Prompt auth for an unauthenticated profile before launch.

        Exits raw mode, runs the auth flow, and re-enters raw mode.
        On success, re-runs profile discovery and updates auth status.
        On skip/failure, returns silently (launch proceeds regardless).
        """
        from .wizard import run_auth_flow

        profile_name = seg.value
        meta = seg.state.metadata.get(profile_name, {})
        config_dir = meta.get("config_dir", "")

        self.terminal.exit_raw()
        print(f"Profile '{profile_name}' has no authentication configured.")

        success = run_auth_flow(config_dir, profile_name)

        if success:
            self._refresh_profile_segment(seg)

        self.terminal.enter_raw()

    def _refresh_profile_segment(self, seg: Segment) -> None:
        """Re-run profile discovery and update the segment's auth status."""
        fresh = _discover_profiles({}, {})
        seg.state.set_discovered(fresh.values)
        if fresh.metadata:
            seg.state.update_metadata(fresh.metadata)
        _update_auth_from_metadata(seg)

    def _launch_profile_wizard(self, seg: Segment) -> str | None:
        """Exit TUI, run the profile wizard, create profile, return to TUI."""
        from .wizard import run_profile_wizard, create_profile, run_auth_flow
        from .discovery import discover_profiles
        self.terminal.exit_raw()
        existing = [p.name for p in discover_profiles()]
        result = run_profile_wizard(existing)
        if not result.cancelled:
            create_profile(result, self.cfg)
            run_auth_flow(result.config_dir, result.name)
            # Add the new profile to the segment's live options (pinned)
            seg.state.add_pinned(result.name)
            # Re-run discovery to pick up the newly created profile
            self._refresh_profile_segment(seg)
            seg.select_value(result.name)
        self.terminal.enter_raw()
        return None

    def _handle_create_key(self, key: str, seg: Segment) -> str | None:
        """Handle keystrokes while in creation mode."""
        match key:
            case "ENTER":
                if seg.create_buffer.strip():
                    self._confirm_create(seg)
                return None
            case "ESC":
                seg.creating = False
                seg.create_buffer = ""
                return None
            case "BACKSPACE":
                seg.create_buffer = seg.create_buffer[:-1]
                return None
            case "CTRL_C":
                seg.creating = False
                seg.create_buffer = ""
                return "quit"
            case _:
                if len(key) == 1 and key.isprintable():
                    seg.create_buffer += key
        return None

    def _confirm_create(self, seg: Segment) -> None:
        """Confirm creation of a new option."""
        name = seg.create_buffer.strip()
        seg.creating = False
        seg.create_buffer = ""

        if not name or name == "+" or name in seg.options:
            return  # invalid or duplicate

        # Add as pinned (appears before virtual "+" in display_options)
        seg.state.add_pinned(name)
        seg.select_value(name)

        # Persist to options.json
        self.cfg.add_option(seg.key, name)
