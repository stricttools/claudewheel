"""TUI event loop, keyboard dispatch, and segment interaction."""

from __future__ import annotations

import copy
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .config import AppConfigStore, resolve_theme_name
from .segment import DiscoveryResult, Segment, build_segment_bar, evaluate_requires, merge_slow_results, run_slow_discovery_via_registry, _discover_profiles, _update_auth_from_metadata
from .terminal import Terminal, detect_mode2031_support
from .theme import parse_theme
from .renderer import Renderer
from .workspace import Workspace


# ---------------------------------------------------------------------------
# Phase 0: Data structures for the keybinding registry
# ---------------------------------------------------------------------------


@dataclass
class KeyContext:
    """Ephemeral snapshot of state relevant to key dispatch decisions."""

    focused: Segment
    mode: str
    seg_key: str
    search_buffer: str
    value: str | None
    is_on_plus: bool
    searchable: bool
    freeform: bool
    freeform_editing: bool
    creating: bool
    show_provenance: bool


@dataclass(frozen=True)
class Binding:
    """A single keybinding entry in the registry."""

    keys: frozenset[str] | None  # None = match-any-printable
    label: str | None  # None = hidden from hints
    condition: Callable[[KeyContext], bool] | None  # None = unconditional
    handler: Callable  # (app, key) -> str | None
    priority: int  # hint ordering (lower = shown first)
    mode: str | None  # "main" / "creating" / "freeform" / "install" / None (cross-mode)



class App:
    """TUI application managing the event loop, keyboard handling, and segment interaction."""

    def __init__(self, workspace: Workspace, cfg: AppConfigStore | None = None,
                 overrides: dict[str, str] | None = None):
        self.workspace = workspace
        self.cfg = cfg if cfg is not None else workspace.appconfig()
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
        theme_name = resolve_theme_name(self.cfg.config.get("theme", "auto"))
        self.theme = parse_theme(self.cfg.load_theme(theme_name))
        self.renderer = Renderer(
            self.terminal, self.theme,
            minimap_mode=self.cfg.config.get("minimap", "auto"),
        )
        self.running = False
        self._flash: str = ""  # Temporary message shown for one render cycle
        self._show_provenance: bool = False  # Phase 9: provenance overlay toggle
        self._mode2031_supported: bool = False
        self._bindings: list[Binding] = self._build_bindings()

    def run_tui(self) -> dict[str, str | None] | None:
        """Enter the TUI loop. Returns selections on launch, None on quit."""
        self.running = True
        # Detect Mode 2031 support before entering raw (uses its own cbreak)
        mode2031_result = detect_mode2031_support()
        self._mode2031_supported = mode2031_result is not None
        self.terminal.enter_raw()
        # Subscribe to Mode 2031 after entering raw mode
        if self._mode2031_supported:
            self.terminal.subscribe_mode2031()

        def on_resize(signum, frame):
            self.terminal.rows, self.terminal.cols = self.terminal.get_size()
            self.renderer.render(self.bar, hints=self._compute_hints())

        signal.signal(signal.SIGWINCH, on_resize)

        def on_term(signum, frame):
            self.terminal.exit_raw()
            sys.exit(1)

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGHUP, on_term)

        try:
            evaluate_requires(self.bar)
            self.renderer.render(self.bar, show_provenance=self._show_provenance, hints=self._compute_hints())
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
                self.renderer.render(self.bar, self._flash, show_provenance=self._show_provenance, hints=self._compute_hints())
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
        self._slow_results = run_slow_discovery_via_registry(
            self.cfg.options_def, state_copy, self.workspace)

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

    # ------------------------------------------------------------------
    # Key context builder
    # ------------------------------------------------------------------

    def _build_context(self) -> KeyContext:
        """Build an ephemeral KeyContext from current app state."""
        focused = self.bar.focused
        # Mode precedence: creating > freeform > main
        if focused.creating:
            mode = "creating"
        elif focused.freeform and focused.search_buffer and focused._freeform_editing:
            mode = "freeform"
        else:
            mode = "main"
        return KeyContext(
            focused=focused,
            mode=mode,
            seg_key=focused.key,
            search_buffer=focused.search_buffer,
            value=focused.value,
            is_on_plus=focused.is_on_plus,
            searchable=focused.searchable,
            freeform=focused.freeform,
            freeform_editing=focused._freeform_editing,
            creating=focused.creating,
            show_provenance=self._show_provenance,
        )

    # ------------------------------------------------------------------
    # Hint computation
    # ------------------------------------------------------------------

    def _compute_hints(self) -> list[str]:
        """Compute visible hint labels from the binding registry for the current state."""
        ctx = self._build_context()
        filtered = [
            b for b in self._bindings
            if (b.mode is None or b.mode == ctx.mode)
            and (b.condition is None or b.condition(ctx))
            and b.label is not None
        ]
        filtered.sort(key=lambda b: b.priority)
        return [b.label for b in filtered]

    # ------------------------------------------------------------------
    # Registry-based dispatch
    # ------------------------------------------------------------------

    def _handle_key(self, key: str) -> str | None:
        """Process a keypress via the binding registry."""
        ctx = self._build_context()
        for b in self._bindings:
            if b.mode is not None and b.mode != ctx.mode:
                continue
            # Key matching
            if b.keys is not None:
                if key not in b.keys:
                    continue
            else:
                # keys=None means match-any-printable
                if not (len(key) == 1 and key.isprintable()):
                    continue
            # Condition check
            if b.condition is not None and not b.condition(ctx):
                continue
            return b.handler(self, key)
        return None

    # ------------------------------------------------------------------
    # Handler methods extracted from the old match/case dispatch
    # ------------------------------------------------------------------

    def _h_main_left(self, key: str) -> str | None:
        self._defocus()
        self.bar.move_focus(-1)
        return None

    def _h_main_right(self, key: str) -> str | None:
        self._defocus()
        self.bar.move_focus(1)
        return None

    def _h_main_up(self, key: str) -> str | None:
        focused = self.bar.focused
        if focused.search_buffer:
            focused.search_buffer = ""
        focused._freeform_editing = False
        focused.cycle(-1)
        return None

    def _h_main_down(self, key: str) -> str | None:
        focused = self.bar.focused
        if focused.search_buffer:
            focused.search_buffer = ""
        focused._freeform_editing = False
        focused.cycle(1)
        return None

    def _h_main_enter(self, key: str) -> str | None:
        focused = self.bar.focused
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
        # Check for non-installed version -- offer to install via form
        for s in self.bar.segments:
            if s.value and s.state.has_installed and not s.state.is_installed(s.value):
                self._run_install_flow(s, s.value)
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
                    outcome = self._intercept_unauth(s)
                    if outcome != "skip":
                        flashes = {
                            "authenticated": "Authenticated",
                            "unverified": "Saved unverified token",
                            "cancel": "Auth cancelled",
                            "failed": "Auth failed",
                        }
                        self._flash = flashes.get(
                            outcome, f"Auth outcome: {outcome}")
                        return None
                break
        return "launch"

    def _h_main_tab(self, key: str) -> str | None:
        focused = self.bar.focused
        if focused.is_on_plus and focused.key == "profile":
            return self._launch_profile_wizard(focused)
        if focused.is_on_plus:
            focused.creating = True
            focused.create_buffer = ""
            return None
        if focused.searchable and focused.search_buffer:
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
        return None

    def _h_main_backspace(self, key: str) -> str | None:
        focused = self.bar.focused
        if focused.freeform and not focused._freeform_editing and focused.value:
            trimmed = focused.value[:-1]
            if trimmed:
                focused.search_buffer = trimmed
                focused._freeform_editing = True
        elif focused.searchable and focused.search_buffer:
            focused.search_buffer = focused.search_buffer[:-1]
        return None

    def _h_main_esc(self, key: str) -> str | None:
        focused = self.bar.focused
        focused.search_buffer = ""
        focused._freeform_editing = False
        return None

    def _h_main_ctrl_c(self, key: str) -> str | None:
        return "quit"

    def _h_main_delete(self, key: str) -> str | None:
        focused = self.bar.focused
        if (focused.key == "profile"
                and not focused.search_buffer
                and focused.value is not None):
            self._delete_profile_flow(focused)
        return None

    def _h_main_question(self, key: str) -> str | None:
        self._show_provenance = not self._show_provenance
        return None

    def _h_main_inspect(self, key: str) -> str | None:
        self._show_profile_inspect(self.bar.focused)
        return None

    def _h_main_freeform_seed(self, key: str) -> str | None:
        focused = self.bar.focused
        focused.search_buffer = focused.value + key
        focused._freeform_editing = True
        return None

    def _h_main_search(self, key: str) -> str | None:
        self.bar.focused.search_buffer += key
        return None

    def _h_main_quit(self, key: str) -> str | None:
        return "quit"

    # -- Cross-mode handlers --

    def _h_theme_switch(self, key: str) -> str | None:
        """Handle Mode 2031 theme-change notification."""
        mode = "dark" if key == "THEME_DARK" else "light"
        theme_dict = self.cfg.load_theme(mode)
        self.theme = parse_theme(theme_dict)
        self.renderer.theme = self.theme
        self.cfg.theme = theme_dict
        return None

    # -- Freeform mode handlers --

    def _h_freeform_enter(self, key: str) -> str | None:
        seg = self.bar.focused
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

    def _h_freeform_tab(self, key: str) -> str | None:
        seg = self.bar.focused
        matches = seg.filtered_options
        if matches:
            seg.select_value(matches[0])
        seg.search_buffer = ""
        seg._freeform_editing = False
        if seg.tab_advances:
            self._apply_pending_for_segment(seg)
            self.bar.move_focus(1)
        return None

    def _h_freeform_backspace(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.search_buffer = seg.search_buffer[:-1]
        if not seg.search_buffer:
            seg._freeform_editing = False
        return None

    def _h_freeform_left(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.search_buffer = ""
        seg._freeform_editing = False
        self._apply_pending_for_segment(seg)
        self.bar.move_focus(-1)
        return None

    def _h_freeform_right(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.search_buffer = ""
        seg._freeform_editing = False
        self._apply_pending_for_segment(seg)
        self.bar.move_focus(1)
        return None

    def _h_freeform_esc(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.search_buffer = ""
        seg._freeform_editing = False
        return None

    def _h_freeform_ctrl_c(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.search_buffer = ""
        seg._freeform_editing = False
        return "quit"

    def _h_freeform_printable(self, key: str) -> str | None:
        self.bar.focused.search_buffer += key
        return None

    # -- Creating mode handlers --

    def _h_create_enter(self, key: str) -> str | None:
        seg = self.bar.focused
        if seg.create_buffer.strip():
            self._confirm_create(seg)
        return None

    def _h_create_esc(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.creating = False
        seg.create_buffer = ""
        return None

    def _h_create_backspace(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.create_buffer = seg.create_buffer[:-1]
        return None

    def _h_create_ctrl_c(self, key: str) -> str | None:
        seg = self.bar.focused
        seg.creating = False
        seg.create_buffer = ""
        return "quit"

    def _h_create_printable(self, key: str) -> str | None:
        self.bar.focused.create_buffer += key
        return None

    # -- Install flow (form-based) --

    def _run_install_flow(self, seg: Segment, version: str) -> None:
        """Confirm install via run_selection, download in cooked, show result page."""
        from .ui import run_selection, show_page

        choice = run_selection(
            f"Install Claude Code v{version}?",
            [("install", "Install"), ("cancel", "Cancel")],
            self.theme, self.terminal,
        )
        if choice != "install":
            return

        from .install import install_version

        def on_progress(downloaded: int, total: int) -> None:
            if total > 0:
                mb_done = downloaded / (1024 * 1024)
                mb_total = total / (1024 * 1024)
                pct = downloaded * 100 // total
                print(f"\r  {mb_done:.0f}/{mb_total:.0f} MB ({pct}%)", end="", flush=True)

        error: str | None = None
        with self.terminal.cooked():
            print(f"Downloading Claude Code {version}...")
            try:
                install_version(version, progress_callback=on_progress)
                seg.state.mark_installed(version)
            except OSError as e:
                error = str(e)

        if error:
            show_page("Install failed", [
                f"Version: {version}",
                "",
                f"Error: {error}",
            ], self.theme, self.terminal)
        else:
            show_page("Install complete", [
                f"Claude Code {version} installed successfully.",
            ], self.theme, self.terminal)

    # ------------------------------------------------------------------
    # Registry builder (called from __init__)
    # ------------------------------------------------------------------

    def _build_bindings(self) -> list[Binding]:
        """Build the full binding registry from handler methods."""
        return [
            # =============================================================
            # CROSS-MODE (mode=None): checked first regardless of mode
            # =============================================================
            Binding(
                keys=frozenset({"THEME_DARK"}),
                label=None,  # hidden
                condition=None,
                handler=App._h_theme_switch,
                priority=0,
                mode=None,
            ),
            Binding(
                keys=frozenset({"THEME_LIGHT"}),
                label=None,  # hidden
                condition=None,
                handler=App._h_theme_switch,
                priority=0,
                mode=None,
            ),
            # =============================================================
            # CREATING MODE
            # =============================================================
            Binding(
                keys=frozenset({"ENTER"}),
                label="enter: confirm",
                condition=None,
                handler=App._h_create_enter,
                priority=20,
                mode="creating",
            ),
            Binding(
                keys=frozenset({"ESC"}),
                label="esc: cancel",
                condition=None,
                handler=App._h_create_esc,
                priority=20,
                mode="creating",
            ),
            Binding(
                keys=frozenset({"BACKSPACE"}),
                label="bksp: delete",
                condition=None,
                handler=App._h_create_backspace,
                priority=20,
                mode="creating",
            ),
            Binding(
                keys=frozenset({"CTRL_C"}),
                label=None,  # hidden
                condition=None,
                handler=App._h_create_ctrl_c,
                priority=20,
                mode="creating",
            ),
            Binding(
                keys=None,  # match-any-printable
                label=None,
                condition=None,
                handler=App._h_create_printable,
                priority=99,
                mode="creating",
            ),
            # =============================================================
            # FREEFORM MODE
            # =============================================================
            Binding(
                keys=frozenset({"ENTER"}),
                label="enter: submit",
                condition=None,
                handler=App._h_freeform_enter,
                priority=20,
                mode="freeform",
            ),
            Binding(
                keys=frozenset({"TAB"}),
                label="tab: accept match",
                condition=None,
                handler=App._h_freeform_tab,
                priority=20,
                mode="freeform",
            ),
            Binding(
                keys=frozenset({"BACKSPACE"}),
                label="bksp: delete",
                condition=None,
                handler=App._h_freeform_backspace,
                priority=20,
                mode="freeform",
            ),
            Binding(
                keys=frozenset({"LEFT"}),
                label=None,  # hidden (undocumented exit)
                condition=None,
                handler=App._h_freeform_left,
                priority=40,
                mode="freeform",
            ),
            Binding(
                keys=frozenset({"RIGHT"}),
                label=None,  # hidden (undocumented exit)
                condition=None,
                handler=App._h_freeform_right,
                priority=40,
                mode="freeform",
            ),
            Binding(
                keys=frozenset({"ESC"}),
                label="esc: cancel",
                condition=None,
                handler=App._h_freeform_esc,
                priority=20,
                mode="freeform",
            ),
            Binding(
                keys=frozenset({"CTRL_C"}),
                label=None,  # hidden
                condition=None,
                handler=App._h_freeform_ctrl_c,
                priority=20,
                mode="freeform",
            ),
            Binding(
                keys=None,  # match-any-printable
                label=None,
                condition=None,
                handler=App._h_freeform_printable,
                priority=99,
                mode="freeform",
            ),
            # =============================================================
            # MAIN MODE
            # =============================================================
            Binding(
                keys=frozenset({"LEFT", "SHIFT_TAB"}),
                label=None,  # SHIFT_TAB hidden
                condition=None,
                handler=App._h_main_left,
                priority=40,
                mode="main",
            ),
            Binding(
                keys=frozenset({"RIGHT"}),
                label="arrows: navigate",
                condition=None,
                handler=App._h_main_right,
                priority=40,
                mode="main",
            ),
            Binding(
                keys=frozenset({"UP"}),
                label=None,
                condition=None,
                handler=App._h_main_up,
                priority=40,
                mode="main",
            ),
            Binding(
                keys=frozenset({"DOWN"}),
                label=None,
                condition=None,
                handler=App._h_main_down,
                priority=40,
                mode="main",
            ),
            Binding(
                keys=frozenset({"ENTER"}),
                label="enter: launch",
                condition=None,
                handler=App._h_main_enter,
                priority=30,
                mode="main",
            ),
            Binding(
                keys=frozenset({"TAB"}),
                label="tab: next",
                condition=None,
                handler=App._h_main_tab,
                priority=40,
                mode="main",
            ),
            Binding(
                keys=frozenset({"BACKSPACE"}),
                label=None,
                condition=None,
                handler=App._h_main_backspace,
                priority=40,
                mode="main",
            ),
            Binding(
                keys=frozenset({"ESC"}),
                label="esc: clear",
                condition=None,
                handler=App._h_main_esc,
                priority=20,
                mode="main",
            ),
            Binding(
                keys=frozenset({"CTRL_C"}),
                label=None,  # hidden
                condition=None,
                handler=App._h_main_ctrl_c,
                priority=60,
                mode="main",
            ),
            Binding(
                keys=frozenset({"CTRL_D", "DELETE"}),
                label="del: delete",
                condition=lambda ctx: (
                    ctx.seg_key == "profile"
                    and not ctx.search_buffer
                    and ctx.value is not None
                ),
                handler=App._h_main_delete,
                priority=50,
                mode="main",
            ),
            # Provenance toggle: ? when not searching
            Binding(
                keys=frozenset({"?"}),
                label="?: sources",
                condition=lambda ctx: not ctx.search_buffer,
                handler=App._h_main_question,
                priority=60,
                mode="main",
            ),
            # Profile inspect: i when on profile, no search, value present
            Binding(
                keys=frozenset({"i"}),
                label="i: inspect",
                condition=lambda ctx: (
                    ctx.seg_key == "profile"
                    and not ctx.search_buffer
                    and ctx.value is not None
                ),
                handler=App._h_main_inspect,
                priority=50,
                mode="main",
            ),
            # Freeform seed: first printable on a freeform segment with a value
            Binding(
                keys=None,  # match-any-printable
                label=None,
                condition=lambda ctx: (
                    ctx.freeform
                    and not ctx.freeform_editing
                    and ctx.value is not None
                ),
                handler=App._h_main_freeform_seed,
                priority=70,
                mode="main",
            ),
            # Search or quit: searchable segment. The 'q' special case (quit
            # when buffer is empty) is handled dynamically inside the handler.
            Binding(
                keys=None,  # match-any-printable
                label=None,
                condition=lambda ctx: ctx.searchable,
                handler=App._h_main_search_or_quit,
                priority=80,
                mode="main",
            ),
            # Quit with 'q' when non-searchable (fallback for non-searchable segments)
            Binding(
                keys=frozenset({"q"}),
                label="q: quit",
                condition=lambda ctx: not ctx.searchable,
                handler=App._h_main_quit,
                priority=60,
                mode="main",
            ),
        ]

    def _h_main_search_or_quit(self, key: str) -> str | None:
        """Handle printable key on searchable segment: search or quit."""
        focused = self.bar.focused
        if focused.search_buffer or key != "q":
            focused.search_buffer += key
            return None
        # key is 'q' and buffer is empty -> quit
        return "quit"

    def _intercept_unauth(self, seg: Segment) -> str:
        """Prompt auth for an unauthenticated profile before launch.

        The app's terminal stays raw: the auth forms render borrowed as
        pages in the existing alt screen, and the subprocess steps inside
        the auth flow open their own cooked windows. On "authenticated",
        "unverified" (a token was saved without validation), and "failed"
        (credentials may be partially written), re-runs profile discovery
        and updates auth status. Returns the auth flow outcome:
        "authenticated", "unverified", "skip", "cancel", or "failed".
        """
        from .wizard import run_auth_flow

        from .binaries import BinaryLocator

        profile_name = seg.value
        # config_dir is derived from the profile name via the ProfileStore's
        # single path_for convention, never from persisted metadata (which no
        # longer carries it).
        config_dir = str(self.workspace.profiles.path_for(profile_name))

        outcome = run_auth_flow(self.workspace, BinaryLocator.default(),
                                config_dir, profile_name,
                                self.theme, self.terminal,
                                skip_label="Launch without auth")

        if outcome in ("authenticated", "unverified", "failed"):
            self._refresh_profile_segment(seg)

        return outcome

    def _show_profile_inspect(self, seg: Segment) -> None:
        """Show a fullscreen inspect page for the focused profile option.

        The app's terminal stays raw: show_page renders borrowed in the
        existing alt screen and the main TUI repaints on return.
        If the profile has an auth shadow, the hint offers 'f' to fix it.
        """
        from .profile_info import format_report, gather_profile_info
        from .profile_ops import fix_auth_shadow
        from .tokens import TokenStoreError
        from .ui import show_page

        # A corrupt tokens.json surfaces as TokenStoreError from gather_profile_info.
        # Catch it narrowly and surface a clean flash instead of crashing the TUI.
        try:
            report = gather_profile_info(self.workspace, seg.value)
        except TokenStoreError as e:
            self._flash = f"Cannot inspect: {e}"
            return
        if report.has_auth_shadow:
            hint = "f: fix auth shadow   any key: close"
        else:
            hint = "any key: close"
        key = show_page(f"Profile: {seg.value}", format_report(report),
                        self.theme, self.terminal, hint=hint)
        if key == "f" and report.has_auth_shadow:
            result = fix_auth_shadow(self.workspace, seg.value)
            if result.ok:
                self._flash = "Auth shadow fixed"
            else:
                self._flash = f"Could not fix: {result.reason}"

    def _delete_profile_flow(self, seg: Segment) -> None:
        """Confirm and delete the focused profile from the TUI.

        Profiles holding REAL data at shared-dir names are hard-blocked
        with a fullscreen page pointing at the CLI escape hatch -- the TUI
        offers no override. Otherwise an informed two-option confirm runs
        (Cancel default-focused). The running check stays CLI/TUI policy;
        the actual deletion goes through ProfileStore.delete (no force
        flags). Store refusals raise ValueError, surfaced as a flash.
        """
        from .profile_info import _format_size, gather_profile_info
        from .ui import run_selection, show_page

        name = seg.value
        report = gather_profile_info(self.workspace, name)

        if report.danger:
            at_risk = sorted(d for d, s in report.shared_dirs.items()
                             if s == "real-dir")
            show_page(
                f"Cannot delete '{name}'",
                [
                    "Shared-dir names holding REAL data (not symlinks):",
                    *(f"  {d}" for d in at_risk),
                    "",
                    "Deleting this profile would destroy that data.",
                    "The TUI offers no override. If you are certain, run:",
                    f"  claudewheel profile delete {name} "
                    "--no-force-delete --force-delete-data",
                ],
                self.theme, self.terminal)
            return

        if report.has_credentials and report.has_token:
            auth = "credentials+token"
        elif report.has_credentials:
            auth = "credentials"
        elif report.has_token:
            auth = "token"
        else:
            auth = "no auth"
        facts = (f"{auth}, {_format_size(report.disk_usage_bytes)}, "
                 f"{report.active_sessions} active sessions")
        choice = run_selection(
            f"Delete profile '{name}'?",
            [("cancel", "Cancel"), ("delete", f"Delete ({facts})")],
            self.theme, self.terminal, initial_key="cancel")
        if choice != "delete":
            return

        # Running check is TUI policy (ProfileStore.delete does not enforce it).
        if report.active_sessions > 0:
            self._flash = f"Not deleted: '{name}' has active sessions"
            return

        try:
            self.workspace.profiles.delete(name)
        except ValueError as e:
            self._flash = f"Not deleted: {e}"
            return

        # In-memory cleanup. The store already purged last_config["profile"]
        # from state.json on disk; drop it from the in-memory state too so
        # the app's later wholesale save_state() doesn't resurrect it.
        last = self.cfg.state.get("last_config", {})
        if last.get("profile") == name:
            del last["profile"]
        seg.state.remove_pinned(name)
        seg.state.metadata.pop(name, None)
        seg.selected_value = None
        self._refresh_profile_segment(seg)
        self._flash = f"Deleted profile '{name}'"

    def _refresh_profile_segment(self, seg: Segment) -> None:
        """Re-run profile discovery and update the segment's auth status."""
        fresh = _discover_profiles({}, {}, self.workspace)
        seg.state.set_discovered(fresh.values)
        if fresh.metadata:
            seg.state.update_metadata(fresh.metadata)
        _update_auth_from_metadata(seg)

    def _launch_profile_wizard(self, seg: Segment) -> str | None:
        """Run the create-profile flow as one continuous alt-screen session.

        The app's terminal stays raw throughout: the wizard form, the auth
        forms, and the creation summary page all render borrowed in the
        existing alt screen (subprocess steps open cooked windows inside
        the auth flow). The main TUI repaints on return.
        """
        from .binaries import BinaryLocator
        from .ui import show_page
        from .wizard import run_profile_wizard, create_profile, run_auth_flow
        existing = [p.name for p in self.workspace.profiles.enumerate()]
        result = run_profile_wizard(self.workspace, existing, self.theme, self.terminal)
        if not result.cancelled:
            summary = create_profile(self.workspace, result)
            run_auth_flow(self.workspace, BinaryLocator.default(),
                          result.config_dir, result.name,
                          self.theme, self.terminal,
                          skip_label="Skip for now")
            show_page("Profile created", summary, self.theme, self.terminal)
            # Add the new profile to the segment's live options (pinned)
            seg.state.add_pinned(result.name)
            # Re-run discovery to pick up the newly created profile. This
            # runs unconditionally for every auth outcome (including
            # "unverified"): the profile itself is new, and on
            # "authenticated"/"unverified"/"failed" credentials may have
            # been written.
            self._refresh_profile_segment(seg)
            seg.select_value(result.name)
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
