"""Tests for App._launch_profile_wizard TUI refresh after wizard."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.segment import DiscoveryResult, Segment, SegmentBar
from claudewheel.state import AUTH_BROWSER_KEY, save_state_value
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
        mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result),
        mock.patch("claudewheel.wizard.create_profile"),
        mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"),
        mock.patch("claudewheel.ui.show_page"),
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        patches = [
            mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result),
            mock.patch("claudewheel.wizard.create_profile"),
            mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"),
            mock.patch("claudewheel.ui.show_page"),
            mock.patch("claudewheel.discovery.discover_profiles", return_value=[]),
        ]
        if mock_auth:
            patches.append(mock.patch.object(app_mod, "_update_auth_from_metadata"))

        entered = []
        try:
            for p in patches:
                entered.append(p.start())

            # Patch the lazy import of run_profile_wizard
            with mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result) as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth, \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        # Use real _update_auth_from_metadata (not mocked) to verify it works
        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
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
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles") as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth, \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
            app._launch_profile_wizard(seg)

        mock_disc.assert_not_called()
        mock_auth.assert_not_called()


class AuthInterceptTests(unittest.TestCase):
    """When Enter is pressed to launch, unauthenticated profiles trigger an auth prompt."""

    def _make_app_with_profile(self, profile_name, authenticated, metadata=None):
        """Create a minimal App with a profile segment configured for auth testing."""
        seg = _make_profile_segment(discovered=[profile_name])
        if metadata is None:
            has_token = authenticated
            has_creds = authenticated
            metadata = {
                profile_name: {
                    "config_dir": f"~/.claudewheel/profiles/{profile_name}",
                    "has_token": has_token,
                    "has_credentials": has_creds,
                },
            }
        seg.state.update_metadata(metadata)
        # Activate auth tracking
        auth_set = set()
        for name, meta in metadata.items():
            if meta.get("has_token") or meta.get("has_credentials"):
                auth_set.add(name)
        seg.state.set_authenticated(auth_set)
        seg.select_value(profile_name)

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        app.bar = mock.MagicMock()
        app.bar.segments = [seg]
        app.bar.focused = seg
        app._flash = ""
        app._pending_install = None
        app._pending_install_seg = None
        app._show_provenance = False
        app._pending_discovery = {}
        return app, seg

    def test_launch_intercepted_when_unauthenticated(self):
        """When profile is unauthenticated, _intercept_unauth is called before launch."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)
        # Verify the segment is indeed unauthenticated
        self.assertTrue(seg.state.has_auth_status)
        self.assertFalse(seg.state.is_authenticated("noauth"))

        with mock.patch.object(app, "_intercept_unauth", return_value="skip") as mock_intercept:
            result = app._handle_key("ENTER")

        mock_intercept.assert_called_once_with(seg)
        self.assertEqual(result, "launch")

    def test_launch_proceeds_when_authenticated(self):
        """When profile is authenticated, _intercept_unauth is not called."""
        app, seg = self._make_app_with_profile("authed", authenticated=True)
        self.assertTrue(seg.state.has_auth_status)
        self.assertTrue(seg.state.is_authenticated("authed"))

        with mock.patch.object(app, "_intercept_unauth") as mock_intercept:
            result = app._handle_key("ENTER")

        mock_intercept.assert_not_called()
        self.assertEqual(result, "launch")

    def test_intercept_reruns_discovery_on_auth_success(self):
        """After successful auth, discovery is re-run and auth status updated."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        fresh_result = DiscoveryResult(
            values=["noauth"],
            metadata={
                "noauth": {
                    "config_dir": "~/.claudewheel/profiles/noauth",
                    "has_token": True,
                    "has_credentials": True,
                },
            },
        )

        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="authenticated") as mock_flow, \
             mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result) as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth_update:
            outcome = app._intercept_unauth(seg)

        self.assertEqual(outcome, "authenticated")
        mock_flow.assert_called_once_with(
            "~/.claudewheel/profiles/noauth", "noauth",
            app.theme, app.terminal,
            skip_label="Launch without auth")
        mock_disc.assert_called_once_with({}, {})
        mock_auth_update.assert_called_once_with(seg)
        # The terminal stays raw: the auth forms render borrowed
        app.terminal.exit_raw.assert_not_called()
        app.terminal.enter_raw.assert_not_called()

    def test_intercept_reruns_discovery_on_auth_failure(self):
        """On 'failed', discovery IS re-run (credentials may be partially written)."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        fresh_result = DiscoveryResult(
            values=["noauth"],
            metadata={
                "noauth": {
                    "config_dir": "~/.claudewheel/profiles/noauth",
                    "has_token": False,
                    "has_credentials": False,
                },
            },
        )

        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="failed"), \
             mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result) as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth_update:
            outcome = app._intercept_unauth(seg)

        self.assertEqual(outcome, "failed")
        mock_disc.assert_called_once_with({}, {})
        mock_auth_update.assert_called_once_with(seg)

    def test_intercept_skips_discovery_on_skip(self):
        """When auth is skipped, discovery is not re-run."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch.object(app_mod, "_discover_profiles") as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth_update:
            outcome = app._intercept_unauth(seg)

        self.assertEqual(outcome, "skip")
        mock_disc.assert_not_called()
        mock_auth_update.assert_not_called()
        # The terminal stays raw even on skip: no raw-mode cycling
        app.terminal.exit_raw.assert_not_called()
        app.terminal.enter_raw.assert_not_called()

    def test_intercept_skips_discovery_on_cancel(self):
        """When the auth form is cancelled, discovery is not re-run."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="cancel"), \
             mock.patch.object(app_mod, "_discover_profiles") as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth_update:
            outcome = app._intercept_unauth(seg)

        self.assertEqual(outcome, "cancel")
        mock_disc.assert_not_called()
        mock_auth_update.assert_not_called()

    def test_skip_auth_still_returns_launch(self):
        """Even when auth is skipped, _handle_key returns 'launch'."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"):
            result = app._handle_key("ENTER")

        self.assertEqual(result, "launch")

    def test_enter_outcome_skip_launches(self):
        """Outcome 'skip' from the intercept falls through to launch, no flash."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch.object(app, "_intercept_unauth", return_value="skip"):
            result = app._handle_key("ENTER")

        self.assertEqual(result, "launch")
        self.assertEqual(app._flash, "")

    def test_enter_outcome_authenticated_suppresses_launch(self):
        """Outcome 'authenticated' suppresses launch and flashes 'Authenticated'."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch.object(app, "_intercept_unauth", return_value="authenticated"):
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        self.assertEqual(app._flash, "Authenticated")

    def test_enter_outcome_cancel_suppresses_launch(self):
        """Outcome 'cancel' suppresses launch and flashes 'Auth cancelled'."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch.object(app, "_intercept_unauth", return_value="cancel"):
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        self.assertEqual(app._flash, "Auth cancelled")

    def test_enter_outcome_failed_suppresses_launch(self):
        """Outcome 'failed' suppresses launch and flashes 'Auth failed'."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch.object(app, "_intercept_unauth", return_value="failed"):
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        self.assertEqual(app._flash, "Auth failed")

    def test_enter_outcome_unverified_suppresses_launch(self):
        """Outcome 'unverified' suppresses launch and flashes the save note."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch.object(app, "_intercept_unauth",
                               return_value="unverified"):
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        self.assertEqual(app._flash, "Saved unverified token")

    def test_enter_unknown_outcome_fails_closed(self):
        """An unknown outcome string must never fall through to launch."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        with mock.patch.object(app, "_intercept_unauth",
                               return_value="something-new"):
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        self.assertIn("something-new", app._flash)

    def test_intercept_reruns_discovery_on_unverified(self):
        """On 'unverified' a token was written -- discovery IS re-run."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        fresh_result = DiscoveryResult(
            values=["noauth"],
            metadata={
                "noauth": {
                    "config_dir": "~/.claudewheel/profiles/noauth",
                    "has_token": True,
                    "has_credentials": False,
                },
            },
        )

        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True,
                        return_value="unverified"), \
             mock.patch.object(app_mod, "_discover_profiles",
                               return_value=fresh_result) as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth_update:
            outcome = app._intercept_unauth(seg)

        self.assertEqual(outcome, "unverified")
        mock_disc.assert_called_once_with({}, {})
        mock_auth_update.assert_called_once_with(seg)

    def test_authenticated_profile_launches_without_intercept_or_flash(self):
        """An authenticated profile launches directly: no intercept, no flash."""
        app, seg = self._make_app_with_profile("authed", authenticated=True)

        with mock.patch.object(app, "_intercept_unauth") as mock_intercept:
            result = app._handle_key("ENTER")

        mock_intercept.assert_not_called()
        self.assertEqual(result, "launch")
        self.assertEqual(app._flash, "")

    def test_intercept_updates_auth_status_on_success(self):
        """After successful auth, the profile appears in the authenticated set."""
        app, seg = self._make_app_with_profile("noauth", authenticated=False)

        fresh_result = DiscoveryResult(
            values=["noauth"],
            metadata={
                "noauth": {
                    "config_dir": "~/.claudewheel/profiles/noauth",
                    "has_token": True,
                    "has_credentials": False,
                },
            },
        )

        # Use real _update_auth_from_metadata to verify the full flow
        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="authenticated"), \
             mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result):
            app._intercept_unauth(seg)

        self.assertTrue(seg.state.has_auth_status)
        self.assertTrue(seg.state.is_authenticated("noauth"))

    def test_no_intercept_when_no_auth_tracking(self):
        """When auth tracking is not active, launch proceeds without intercept."""
        seg = _make_profile_segment(discovered=["myprofile"])
        seg.select_value("myprofile")
        # No auth status set -- has_auth_status is False

        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        app.bar = mock.MagicMock()
        app.bar.segments = [seg]
        app.bar.focused = seg
        app._flash = ""
        app._pending_install = None
        app._pending_install_seg = None
        app._show_provenance = False
        app._pending_discovery = {}

        with mock.patch.object(app, "_intercept_unauth") as mock_intercept:
            result = app._handle_key("ENTER")

        mock_intercept.assert_not_called()
        self.assertEqual(result, "launch")


class ContinuousSessionTests(unittest.TestCase):
    """The create-profile flow runs as one continuous alt-screen session:
    the app terminal stays raw, and a summary page is shown after auth."""

    def _make_app(self):
        app = object.__new__(app_mod.App)
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        return app

    def _wizard_result(self, cancelled: bool = False):
        wizard_result = mock.MagicMock()
        wizard_result.cancelled = cancelled
        wizard_result.name = "newprof"
        wizard_result.config_dir = "~/.claudewheel/profiles/newprof"
        return wizard_result

    def test_terminal_never_raw_cycled_during_wizard_flow(self) -> None:
        app = self._make_app()
        seg = _make_profile_segment(discovered=["existing"])
        fresh = DiscoveryResult(values=["existing", "newprof"], metadata={})

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True,
                        return_value=self._wizard_result()):
            app._launch_profile_wizard(seg)

        app.terminal.exit_raw.assert_not_called()
        app.terminal.enter_raw.assert_not_called()

    def test_summary_page_shown_after_auth(self) -> None:
        app = self._make_app()
        seg = _make_profile_segment(discovered=["existing"])
        fresh = DiscoveryResult(values=["existing", "newprof"], metadata={})
        summary = ["Created profile 'newprof':", "  Config dir: /x"]

        manager = mock.MagicMock()
        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile",
                        return_value=summary) as mock_create, \
             mock.patch("claudewheel.ui.show_page") as mock_page, \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True,
                        return_value="skip") as mock_auth, \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True,
                        return_value=self._wizard_result()):
            manager.attach_mock(mock_auth, "run_auth_flow")
            manager.attach_mock(mock_page, "show_page")
            app._launch_profile_wizard(seg)

        mock_create.assert_called_once()
        mock_page.assert_called_once_with(
            "Profile created", summary, app.theme, app.terminal)
        # The summary page appears after the auth flow, not before
        call_names = [c[0] for c in manager.mock_calls]
        self.assertLess(call_names.index("run_auth_flow"),
                        call_names.index("show_page"))

    def test_no_summary_page_on_cancel(self) -> None:
        app = self._make_app()
        seg = _make_profile_segment(discovered=["existing"])

        with mock.patch("claudewheel.wizard.create_profile") as mock_create, \
             mock.patch("claudewheel.ui.show_page") as mock_page, \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.discovery.discover_profiles", return_value=[]), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True,
                        return_value=self._wizard_result(cancelled=True)):
            app._launch_profile_wizard(seg)

        mock_create.assert_not_called()
        mock_page.assert_not_called()


class ApplySlowDiscoverySaveStateTests(unittest.TestCase):
    """Regression: _apply_slow_discovery ends with a wholesale cfg.save_state().

    If the auth wizard wrote auth_browser straight to state.json after the
    app loaded its in-memory state, that wholesale save must not clobber the
    freshly-written key (same mitigation as state.save_launch_state).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = Path(self._tmp.name) / "state.json"
        patcher = mock.patch("claudewheel.state.STATE_FILE", self.state_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_app(self) -> app_mod.App:
        """Build a minimal App poised to run _apply_slow_discovery's save path."""
        app = object.__new__(app_mod.App)
        state_file = self.state_file

        class _StubCfg:
            """ConfigManager stand-in: wholesale save_state like the real one."""

            def __init__(self) -> None:
                # In-memory state loaded at startup -- no auth_browser yet.
                self.state: dict = {"launch_count": 1}
                self.options_def: dict = {}

            def save_state(self) -> None:
                state_file.write_text(json.dumps(self.state, indent=2) + "\n")

        app.cfg = _StubCfg()
        app.bar = SegmentBar(segments=[_make_profile_segment(discovered=["default"])])
        app._slow_results = {}  # non-None so the save path runs
        app._slow_state_copy = None
        app._pending_discovery = {}
        return app

    def test_auth_browser_written_out_of_band_survives(self) -> None:
        app = self._make_app()
        # Auth wizard persists the browser choice to disk mid-session, after
        # the app's in-memory state was loaded.
        save_state_value(AUTH_BROWSER_KEY, "/usr/bin/ff")

        app._apply_slow_discovery()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk.get(AUTH_BROWSER_KEY), "/usr/bin/ff")

    def test_no_auth_browser_key_invented_when_absent(self) -> None:
        app = self._make_app()
        app._apply_slow_discovery()
        on_disk = json.loads(self.state_file.read_text())
        self.assertNotIn(AUTH_BROWSER_KEY, on_disk)


if __name__ == "__main__":
    unittest.main()
