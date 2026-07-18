"""Tests for renderer dimming logic (auth status, installed, unavailable)."""

from __future__ import annotations

import unittest

from claudewheel.constants import DIM
from claudewheel.segment import Segment


class RenderOptionAuthDimmingTests(unittest.TestCase):
    """_render_option dims unauthenticated profiles via unavail_fg."""

    def _make_renderer(self):
        """Create a minimal Renderer with stub terminal and theme."""
        from claudewheel.renderer import Renderer

        class StubTerminal:
            rows = 40
            cols = 120

            def write(self, _):
                pass

            def flush(self):
                pass

        class StubTheme:
            empty_value_text = "---"
            empty_value_fg = ""
            search_match_fg = ""
            search_cursor_fg = ""
            search_no_match_fg = ""
            label_fg = ""
            separator_char = " | "
            separator_fg = ""
            overflow_arrow_fg = ""
            overflow_minimap_fg = ""
            overflow_minimap_focused_bg = ""
            overflow_minimap_char = "█"
            segment_colors = {
                "profile": {
                    "focus_bg": "",
                    "focus_fg": "",
                    "value_fg": "\x1b[38;2;0;255;0m",
                    "option_fg": DIM,
                    "unavailable_fg": "\x1b[38;2;85;85;85m",
                },
            }

        return Renderer(StubTerminal(), StubTheme())

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


if __name__ == "__main__":
    unittest.main()
