"""Tests for App._launch_profile_wizard TUI refresh after wizard."""

from __future__ import annotations

import unittest
from unittest import mock

from claudewheel.segment import DiscoveryResult, Segment
from claudewheel import app as app_mod


def _make_profile_segment(
    discovered: list[str] | None = None,
    pinned: list[str] | None = None,
) -> Segment:
    """Build a profile segment with optional discovered/pinned values."""
    seg = Segment(
        key="profile",
        label="Profile",
        creatable=True,
    )
    seg.state.collection_order = ["pinned", "discovered"]
    if discovered:
        seg.state.set_discovered(discovered)
    if pinned:
        for p in pinned:
            seg.state.add_pinned(p)
    return seg


def _wizard_mocks(wizard_result, fresh_result=None):
    """Context manager stack for mocking wizard internals.

    Patches the lazy imports inside _launch_profile_wizard: wizard functions
    live in claudewheel.wizard, discovery in claudewheel.discovery.
    """
    patches = [
        mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result),
        mock.patch("claudewheel.wizard.create_profile"),
        mock.patch("claudewheel.wizard.run_auth_flow"),
        mock.patch("claudewheel.discovery.discover_profiles", return_value=[]),
    ]
    if fresh_result is not None:
        patches.append(mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result))
    return patches


class WizardRefreshDiscoveryTests(unittest.TestCase):
    """After wizard creates a profile, _discover_profiles is re-run."""

    def _run_wizard_on_app(self, seg, wizard_result, fresh_result, mock_auth=True):
        """Run _launch_profile_wizard with all necessary mocks."""
        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        patches = [
            mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result),
            mock.patch("claudewheel.wizard.create_profile"),
            mock.patch("claudewheel.wizard.run_auth_flow"),
            mock.patch("claudewheel.discovery.discover_profiles", return_value=[]),
        ]
        if mock_auth:
            patches.append(mock.patch.object(app_mod, "_update_auth_from_metadata"))

        entered = []
        try:
            for p in patches:
                entered.append(p.start())

            # Patch the lazy import of run_profile_wizard
            with mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
                app._launch_profile_wizard(seg)
        finally:
            for p in patches:
                p.stop()

        return entered

    def test_discovery_rerun_after_wizard(self) -> None:
        """Verify _discover_profiles is called after create_profile + auth."""
        seg = _make_profile_segment(discovered=["existing"])
        fresh_result = DiscoveryResult(
            values=["existing", "newprof"],
            metadata={
                "existing": {"config_dir": "~/.claudewheel/profiles/existing",
                             "has_token": True, "has_credentials": True},
                "newprof": {"config_dir": "~/.claudewheel/profiles/newprof",
                            "has_token": True, "has_credentials": False},
            },
        )

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "newprof"
        wizard_result.config_dir = "~/.claudewheel/profiles/newprof"

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result) as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth, \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.wizard.run_auth_flow"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
            app._launch_profile_wizard(seg)

        mock_disc.assert_called_once_with({}, {})
        mock_auth.assert_called_once_with(seg)

    def test_discovered_values_updated_after_wizard(self) -> None:
        """After wizard, the segment's discovered values include the new profile."""
        seg = _make_profile_segment(discovered=["old"])
        fresh_result = DiscoveryResult(
            values=["old", "brand-new"],
            metadata={
                "old": {"config_dir": "~/.claudewheel/profiles/old",
                        "has_token": True, "has_credentials": True},
                "brand-new": {"config_dir": "~/.claudewheel/profiles/brand-new",
                              "has_token": False, "has_credentials": True},
            },
        )

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "brand-new"
        wizard_result.config_dir = "~/.claudewheel/profiles/brand-new"

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.wizard.run_auth_flow"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
            app._launch_profile_wizard(seg)

        # Discovered values should be updated
        self.assertIn("brand-new", seg.state._discovered)
        self.assertIn("old", seg.state._discovered)

    def test_metadata_updated_after_wizard(self) -> None:
        """After wizard, the segment's metadata includes entries from fresh discovery."""
        seg = _make_profile_segment(discovered=["existing"])
        fresh_result = DiscoveryResult(
            values=["existing", "newprof"],
            metadata={
                "existing": {"config_dir": "~/.claudewheel/profiles/existing",
                             "has_token": True, "has_credentials": True},
                "newprof": {"config_dir": "~/.claudewheel/profiles/newprof",
                            "has_token": True, "has_credentials": False},
            },
        )

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "newprof"
        wizard_result.config_dir = "~/.claudewheel/profiles/newprof"

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.wizard.run_auth_flow"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
            app._launch_profile_wizard(seg)

        self.assertIn("newprof", seg.state.metadata)
        self.assertEqual(
            seg.state.metadata["newprof"]["config_dir"],
            "~/.claudewheel/profiles/newprof",
        )

    def test_new_profile_selected_after_wizard(self) -> None:
        """After wizard, the new profile is the selected value."""
        seg = _make_profile_segment(discovered=["existing"])
        fresh_result = DiscoveryResult(
            values=["existing", "fresh"],
            metadata={
                "existing": {"config_dir": "~/.claudewheel/profiles/existing",
                             "has_token": True, "has_credentials": True},
                "fresh": {"config_dir": "~/.claudewheel/profiles/fresh",
                          "has_token": True, "has_credentials": True},
            },
        )

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "fresh"
        wizard_result.config_dir = "~/.claudewheel/profiles/fresh"

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.wizard.run_auth_flow"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
            app._launch_profile_wizard(seg)

        self.assertEqual(seg.value, "fresh")


class WizardRefreshAuthTests(unittest.TestCase):
    """After wizard and re-discovery, auth status is recomputed."""

    def test_auth_computed_from_fresh_metadata(self) -> None:
        """_update_auth_from_metadata sees the fresh metadata and sets auth status."""
        seg = _make_profile_segment(discovered=["existing"])
        fresh_result = DiscoveryResult(
            values=["existing", "authed"],
            metadata={
                "existing": {"config_dir": "~/.claudewheel/profiles/existing",
                             "has_token": True, "has_credentials": True},
                "authed": {"config_dir": "~/.claudewheel/profiles/authed",
                           "has_token": True, "has_credentials": False},
            },
        )

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "authed"
        wizard_result.config_dir = "~/.claudewheel/profiles/authed"

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        # Use real _update_auth_from_metadata (not mocked) to verify it works
        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.wizard.run_auth_flow"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
            app._launch_profile_wizard(seg)

        # "existing" and "authed" both have has_token=True
        self.assertTrue(seg.state.has_auth_status)
        self.assertTrue(seg.state.is_authenticated("existing"))
        self.assertTrue(seg.state.is_authenticated("authed"))

    def test_unauthenticated_profile_not_in_auth_set(self) -> None:
        """A profile with has_token=False and has_credentials=False is not authenticated."""
        seg = _make_profile_segment(discovered=["existing"])
        fresh_result = DiscoveryResult(
            values=["existing", "noauth"],
            metadata={
                "existing": {"config_dir": "~/.claudewheel/profiles/existing",
                             "has_token": True, "has_credentials": True},
                "noauth": {"config_dir": "~/.claudewheel/profiles/noauth",
                           "has_token": False, "has_credentials": False},
            },
        )

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "noauth"
        wizard_result.config_dir = "~/.claudewheel/profiles/noauth"

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.wizard.run_auth_flow"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
            app._launch_profile_wizard(seg)

        self.assertTrue(seg.state.has_auth_status)
        self.assertTrue(seg.state.is_authenticated("existing"))
        self.assertFalse(seg.state.is_authenticated("noauth"))


class WizardCancelledTests(unittest.TestCase):
    """When wizard is cancelled, no discovery or auth refresh happens."""

    def test_cancelled_wizard_skips_refresh(self) -> None:
        """Cancelled wizard does not call _discover_profiles or _update_auth_from_metadata."""
        seg = _make_profile_segment(discovered=["existing"])

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = True

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles") as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth, \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.wizard.run_auth_flow"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", return_value=wizard_result):
            app._launch_profile_wizard(seg)

        mock_disc.assert_not_called()
        mock_auth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
