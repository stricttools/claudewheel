"""Tests for renderer dimming logic (auth status, installed, unavailable)."""

from __future__ import annotations

import unittest

from claudewheel.constants import DIM
from claudewheel.renderer import Renderer
from claudewheel.segment import Segment
from claudewheel.terminal import Terminal
from claudewheel.theme import ThemeColors


class RenderOptionAuthDimmingTests(unittest.TestCase):
    """_render_option dims unauthenticated profiles via unavail_fg."""

    def _make_renderer(self) -> Renderer:
        """Create a minimal Renderer with stub terminal and real theme."""

        class StubTerminal(Terminal):
            def __init__(self) -> None:
                self.rows = 40
                self.cols = 120

            def write(self, text: str) -> None:
                pass

            def flush(self) -> None:
                pass

        theme = ThemeColors(
            global_fg="",
            label_fg="",
            separator_fg="",
            separator_char=" | ",
            empty_value_fg="",
            empty_value_text="---",
            search_cursor_fg="",
            search_match_fg="",
            search_no_match_fg="",
            overflow_arrow_fg="",
            overflow_minimap_fg="",
            overflow_minimap_focused_bg="",
            overflow_minimap_char="█",
            segment_colors={
                "profile": {
                    "focus_bg": "",
                    "focus_fg": "",
                    "value_fg": "\x1b[38;2;0;255;0m",
                    "option_fg": DIM,
                    "unavailable_fg": "\x1b[38;2;85;85;85m",
                },
            },
        )
        return Renderer(StubTerminal(), theme)

    def test_unauthenticated_option_gets_unavail_fg(self) -> None:
        """An unauthenticated profile option is rendered with unavail_fg color."""
        renderer = self._make_renderer()
        seg = Segment(key="profile", label="Profile")
        seg.state.set_authenticated({"authed-profile"})

        buf: list[str] = []
        unavail_fg = "\x1b[38;2;85;85;85m"
        renderer._render_option(
            buf, seg, "unauthed-profile", "unauthed-profile", DIM, unavail_fg
        )

        rendered = "".join(buf)
        self.assertIn(unavail_fg, rendered)
        self.assertIn("unauthed-profile", rendered)

    def test_authenticated_option_gets_normal_fg(self) -> None:
        """An authenticated profile option is rendered with normal option_fg."""
        renderer = self._make_renderer()
        seg = Segment(key="profile", label="Profile")
        seg.state.set_authenticated({"authed-profile"})

        buf: list[str] = []
        option_fg = DIM
        unavail_fg = "\x1b[38;2;85;85;85m"
        renderer._render_option(
            buf, seg, "authed-profile", "authed-profile", option_fg, unavail_fg
        )

        rendered = "".join(buf)
        # Should use option_fg (DIM), not unavail_fg
        self.assertNotIn(unavail_fg, rendered)

    def test_no_auth_status_no_dimming(self) -> None:
        """Without auth status active, no auth-based dimming occurs."""
        renderer = self._make_renderer()
        seg = Segment(key="profile", label="Profile")
        # Auth status not activated (default)

        buf: list[str] = []
        option_fg = DIM
        unavail_fg = "\x1b[38;2;85;85;85m"
        renderer._render_option(
            buf, seg, "some-profile", "some-profile", option_fg, unavail_fg
        )

        rendered = "".join(buf)
        # Should use normal option_fg, not unavail_fg
        self.assertNotIn(unavail_fg, rendered)

    def test_managed_option_not_dimmed(self) -> None:
        """A managed value (the vanilla default) renders normally, not dimmed.

        It is neither in the authenticated set nor treated as unauthenticated.
        """
        renderer = self._make_renderer()
        seg = Segment(key="profile", label="Profile")
        seg.state.set_authenticated({"authed-profile"})
        seg.state.set_managed({"default"})

        buf: list[str] = []
        unavail_fg = "\x1b[38;2;85;85;85m"
        renderer._render_option(buf, seg, "default", "default", DIM, unavail_fg)

        rendered = "".join(buf)
        self.assertNotIn(unavail_fg, rendered)
        self.assertIn("default", rendered)

    def test_installed_check_takes_priority_over_auth(self) -> None:
        """Installed-status dimming fires before auth check."""
        renderer = self._make_renderer()
        seg = Segment(key="version", label="Version")
        seg.state.set_installed({"1.0.0"})
        # Also set auth status -- but installed check should fire first
        seg.state.set_authenticated({"2.0.0"})

        buf: list[str] = []
        unavail_fg = "\x1b[38;2;85;85;85m"
        renderer._render_option(buf, seg, "2.0.0", "2.0.0", DIM, unavail_fg)

        rendered = "".join(buf)
        # 2.0.0 is NOT installed, so installed check dims it
        self.assertIn(unavail_fg, rendered)


class RenderStatusNoticeTests(unittest.TestCase):
    """_render_status prioritizes flash > provenance > notice > hints."""

    def _make_renderer(self) -> Renderer:
        class StubTerminal(Terminal):
            def __init__(self) -> None:
                self.rows = 40
                self.cols = 120

            def write(self, text: str) -> None:
                pass

            def flush(self) -> None:
                pass

        theme = ThemeColors(
            global_fg="",
            label_fg="",
            separator_fg="",
            separator_char=" | ",
            empty_value_fg="\x1b[38;2;1;2;3m",
            empty_value_text="---",
            search_cursor_fg="",
            search_match_fg="",
            search_no_match_fg="",
            overflow_arrow_fg="",
            overflow_minimap_fg="",
            overflow_minimap_focused_bg="",
            overflow_minimap_char="█",
            segment_colors={},
        )
        return Renderer(StubTerminal(), theme)

    def _status(
        self,
        *,
        flash: str = "",
        notice: str = "",
        hints: tuple[str, ...] = (),
        provenance: bool = False,
    ) -> str:
        renderer = self._make_renderer()
        renderer._show_provenance = provenance
        buf: list[str] = []
        # bar is unused by _render_status; None is safe.
        renderer._render_status(buf, None, flash, notice, list(hints))  # type: ignore[arg-type]
        return "".join(buf)

    def test_notice_renders_in_status_row(self) -> None:
        rendered = self._status(notice="1 stale token entry — press T to review")
        self.assertIn("1 stale token entry", rendered)

    def test_flash_takes_priority_over_notice(self) -> None:
        rendered = self._status(flash="Flashy", notice="Noticed")
        self.assertIn("Flashy", rendered)
        self.assertNotIn("Noticed", rendered)

    def test_notice_takes_priority_over_hints(self) -> None:
        rendered = self._status(notice="Noticed", hints=("q: quit", "i: inspect"))
        self.assertIn("Noticed", rendered)
        self.assertNotIn("q: quit", rendered)

    def test_provenance_takes_priority_over_notice(self) -> None:
        rendered = self._status(notice="Noticed", provenance=True)
        self.assertIn("discovered", rendered)
        self.assertNotIn("Noticed", rendered)

    def test_hints_render_when_no_notice(self) -> None:
        rendered = self._status(hints=("q: quit",))
        self.assertIn("q: quit", rendered)


if __name__ == "__main__":
    unittest.main()
