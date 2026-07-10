"""Tests for App._launch_profile_wizard TUI refresh after wizard."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel.binaries import BinaryLocator
from claudewheel.segment import DiscoveryResult, Segment, SegmentBar
from claudewheel.appdata import StateFile
from claudewheel.state import AUTH_BROWSER_KEY
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
    ]
    if fresh_result is not None:
        patches.append(mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result))
    return patches


class LocatorInjectionTests(unittest.TestCase):
    """The BinaryLocator must be explicitly injected -- no None fallback.

    Like the workspace, the locator is a required constructor dependency
    threaded from the CLI dispatch boundary. There is no silent
    ``BinaryLocator.default()`` fallback: omitting it is a hard TypeError.
    """

    def test_locator_is_required_parameter(self) -> None:
        import inspect
        sig = inspect.signature(app_mod.App.__init__)
        param = sig.parameters["locator"]
        self.assertIs(
            param.default, inspect.Parameter.empty,
            "App.__init__ locator must be required (no None fallback)")

    def test_constructing_without_locator_raises(self) -> None:
        with self.assertRaises(TypeError):
            app_mod.App(mock.MagicMock())


class WizardRefreshDiscoveryTests(unittest.TestCase):
    """After wizard creates a profile, _discover_profiles is re-run."""

    def _run_wizard_on_app(self, seg, wizard_result, fresh_result, mock_auth=True):
        """Run _launch_profile_wizard with all necessary mocks."""
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        patches = [
            mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result),
            mock.patch("claudewheel.wizard.create_profile"),
            mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"),
            mock.patch("claudewheel.ui.show_page"),
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result) as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth, \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True, return_value=wizard_result):
            app._launch_profile_wizard(seg)

        mock_disc.assert_called_once_with({}, {}, app.workspace)
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch.object(app_mod, "_update_auth_from_metadata"), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        # Use real _update_auth_from_metadata (not mocked) to verify it works
        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result), \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()

        with mock.patch.object(app_mod, "_discover_profiles") as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth, \
             mock.patch("claudewheel.wizard.create_profile"), \
             mock.patch("claudewheel.ui.show_page"), \
             mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="skip"), \
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

        from claudewheel.workspace import Workspace
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        # Real workspace so path_for("noauth") is deterministic for assertions.
        app.workspace = Workspace.default()
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        app.bar = mock.MagicMock()
        app.bar.segments = [seg]
        app.bar.focused = seg
        app._flash = ""
        app._show_provenance = False
        app._pending_discovery = {}
        app._bindings = app._build_bindings()
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

        from claudewheel.workspace import Workspace

        with mock.patch("claudewheel.wizard.run_auth_flow", autospec=True, return_value="authenticated") as mock_flow, \
             mock.patch.object(app_mod, "_discover_profiles", return_value=fresh_result) as mock_disc, \
             mock.patch.object(app_mod, "_update_auth_from_metadata") as mock_auth_update:
            outcome = app._intercept_unauth(seg)

        self.assertEqual(outcome, "authenticated")
        # config_dir is derived from the profile name via ProfileStore.path_for.
        expected_dir = str(app.workspace.profiles.path_for("noauth"))
        mock_flow.assert_called_once_with(
            app.workspace, BinaryLocator.default(),
            expected_dir, "noauth",
            app.theme, app.terminal,
            skip_label="Launch without auth")
        mock_disc.assert_called_once_with({}, {}, app.workspace)
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
        mock_disc.assert_called_once_with({}, {}, app.workspace)
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
        mock_disc.assert_called_once_with({}, {}, app.workspace)
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
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        app.bar = mock.MagicMock()
        app.bar.segments = [seg]
        app.bar.focused = seg
        app._flash = ""
        app._show_provenance = False
        app._pending_discovery = {}
        app._bindings = app._build_bindings()

        with mock.patch.object(app, "_intercept_unauth") as mock_intercept:
            result = app._handle_key("ENTER")

        mock_intercept.assert_not_called()
        self.assertEqual(result, "launch")


class ContinuousSessionTests(unittest.TestCase):
    """The create-profile flow runs as one continuous alt-screen session:
    the app terminal stays raw, and a summary page is shown after auth."""

    def _make_app(self):
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
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
             mock.patch("claudewheel.wizard.run_profile_wizard", autospec=True,
                        return_value=self._wizard_result(cancelled=True)):
            app._launch_profile_wizard(seg)

        mock_create.assert_not_called()
        mock_page.assert_not_called()


class InstallFlowFormTests(unittest.TestCase):
    """Install flow uses run_selection for confirm, cooked window for download,
    and show_page for the result."""

    def _make_app_with_uninstalled(self, version: str = "1.2.3"):
        """Build a minimal App where ENTER triggers the install flow."""
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        seg = mock.MagicMock()
        seg.key = "version"
        seg.label = "Version"
        seg.search_buffer = ""
        seg.creating = False
        seg.freeform = False
        seg._freeform_editing = False
        seg.searchable = False
        seg.is_on_plus = False
        seg.value = version
        seg.required = True
        seg.state.has_installed = True
        seg.state.is_installed.return_value = False
        seg.unavailable = set()
        app.bar = mock.MagicMock()
        app.bar.focused = seg
        app.bar.segments = [seg]
        app._flash = ""
        app._show_provenance = False
        app._pending_discovery = {}
        app._bindings = app._build_bindings()
        return app, seg

    def _track_cooked(self, app, events: list[str]) -> None:
        cm = app.terminal.cooked.return_value
        cm.__enter__.side_effect = lambda *a: events.append("cooked_enter")
        cm.__exit__.side_effect = lambda *a: events.append("cooked_exit") or False

    def test_confirm_install_downloads_and_marks_installed(self) -> None:
        """User confirms install -> download in cooked -> mark_installed -> success page."""
        app, seg = self._make_app_with_uninstalled()
        events: list[str] = []
        self._track_cooked(app, events)

        def fake_install(version, progress_callback=None):
            events.append("install")

        with mock.patch("claudewheel.ui.run_selection", return_value="install") as mock_sel, \
             mock.patch("claudewheel.install.install_version", side_effect=fake_install), \
             mock.patch("claudewheel.ui.show_page") as mock_page, \
             redirect_stdout(io.StringIO()):
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        # Confirm dialog shown with correct title
        self.assertIn("1.2.3", mock_sel.call_args[0][0])
        # Download happened inside cooked window
        self.assertEqual(events, ["cooked_enter", "install", "cooked_exit"])
        seg.state.mark_installed.assert_called_once_with("1.2.3")
        # Success page shown
        mock_page.assert_called_once()
        self.assertEqual(mock_page.call_args[0][0], "Install complete")

    def test_cancel_install_does_nothing(self) -> None:
        """User cancels at the confirm dialog -> no download, no page."""
        app, seg = self._make_app_with_uninstalled()

        with mock.patch("claudewheel.ui.run_selection", return_value="cancel") as mock_sel, \
             mock.patch("claudewheel.install.install_version") as mock_install, \
             mock.patch("claudewheel.ui.show_page") as mock_page:
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        mock_install.assert_not_called()
        mock_page.assert_not_called()
        seg.state.mark_installed.assert_not_called()
        app.terminal.cooked.assert_not_called()

    def test_escape_from_confirm_does_nothing(self) -> None:
        """Escape from run_selection (returns None) -> no download."""
        app, seg = self._make_app_with_uninstalled()

        with mock.patch("claudewheel.ui.run_selection", return_value=None), \
             mock.patch("claudewheel.install.install_version") as mock_install, \
             mock.patch("claudewheel.ui.show_page") as mock_page:
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        mock_install.assert_not_called()
        mock_page.assert_not_called()
        seg.state.mark_installed.assert_not_called()

    def test_install_failure_shows_error_page(self) -> None:
        """Download fails -> show_page with error message, mark_installed not called."""
        app, seg = self._make_app_with_uninstalled()

        with mock.patch("claudewheel.ui.run_selection", return_value="install"), \
             mock.patch("claudewheel.install.install_version",
                        side_effect=OSError("network down")), \
             mock.patch("claudewheel.ui.show_page") as mock_page, \
             redirect_stdout(io.StringIO()):
            result = app._handle_key("ENTER")

        self.assertIsNone(result)
        seg.state.mark_installed.assert_not_called()
        mock_page.assert_called_once()
        title = mock_page.call_args[0][0]
        lines = mock_page.call_args[0][1]
        self.assertEqual(title, "Install failed")
        self.assertTrue(any("network down" in line for line in lines))

    def test_no_pending_install_attribute(self) -> None:
        """App no longer has _pending_install or _pending_install_seg attributes."""
        app, _ = self._make_app_with_uninstalled()
        self.assertFalse(hasattr(app, "_pending_install"))
        self.assertFalse(hasattr(app, "_pending_install_seg"))

    def test_no_install_mode_in_bindings(self) -> None:
        """No binding entry with mode='install' exists in the registry."""
        app, _ = self._make_app_with_uninstalled()
        install_bindings = [b for b in app._bindings if b.mode == "install"]
        self.assertEqual(install_bindings, [])


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

    def _make_app(self) -> app_mod.App:
        """Build a minimal App poised to run _apply_slow_discovery's save path."""
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        state_file = self.state_file

        class _StubCfg:
            """AppConfigStore stand-in: wholesale save_state like the real one.

            Mirrors the out-of-band merge logic in AppConfigStore.save_state().
            """

            def __init__(self) -> None:
                # In-memory state loaded at startup -- no auth_browser yet.
                self.state: dict = {"launch_count": 1}
                self.options_def: dict = {}

            def save_state(self) -> None:
                try:
                    on_disk = json.loads(state_file.read_text())
                    if isinstance(on_disk, dict):
                        browser = on_disk.get("auth_browser")
                        if browser is not None:
                            self.state["auth_browser"] = browser
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
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
        StateFile(self.state_file).set_value(AUTH_BROWSER_KEY, "/usr/bin/ff")

        app._apply_slow_discovery()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk.get(AUTH_BROWSER_KEY), "/usr/bin/ff")

    def test_no_auth_browser_key_invented_when_absent(self) -> None:
        app = self._make_app()
        app._apply_slow_discovery()
        on_disk = json.loads(self.state_file.read_text())
        self.assertNotIn(AUTH_BROWSER_KEY, on_disk)


class ProfileInspectKeyTests(unittest.TestCase):
    """The 'i' key opens the profile inspect page (Phase 4b guards)."""

    def _make_app(self, seg: Segment) -> app_mod.App:
        """Minimal App with a real _handle_key bound and *seg* focused."""
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        app.bar = mock.MagicMock()
        app.bar.segments = [seg]
        app.bar.focused = seg
        app._flash = ""
        app._show_provenance = False
        app._pending_discovery = {}
        app._bindings = app._build_bindings()
        return app

    def _inspect_mocks(self):
        """Patch the lazy imports inside _show_profile_inspect."""
        return (
            mock.patch("claudewheel.profile_info.gather_profile_info",
                       return_value=mock.MagicMock()),
            mock.patch("claudewheel.profile_info.format_report",
                       return_value=["line"]),
            mock.patch("claudewheel.ui.show_page"),
        )

    def test_i_opens_page_on_profile_segment(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        gather, fmt, page = self._inspect_mocks()
        with gather as mock_gather, fmt, page as mock_page:
            app._handle_key("i")
        mock_gather.assert_called_once_with(app.workspace, "work")
        mock_page.assert_called_once()
        self.assertIn("work", mock_page.call_args[0][0])

    def test_i_corrupt_tokens_flashes_no_page(self) -> None:
        """A corrupt tokens.json surfaces as a flash, not a crash or page."""
        from claudewheel.tokens import TokenStoreError
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        with mock.patch("claudewheel.profile_info.gather_profile_info",
                        side_effect=TokenStoreError("tokens.json is corrupt")), \
                mock.patch("claudewheel.ui.show_page") as mock_page:
            app._show_profile_inspect(seg)
        mock_page.assert_not_called()
        self.assertIn("corrupt", app._flash)

    def test_i_guarded_on_plus_sentinel(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.selected_value = "+"  # creatable sentinel
        app = self._make_app(seg)
        gather, fmt, page = self._inspect_mocks()
        with gather, fmt, page as mock_page:
            app._handle_key("i")
        mock_page.assert_not_called()

    def test_i_guarded_on_empty_selection(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.selected_value = None
        app = self._make_app(seg)
        gather, fmt, page = self._inspect_mocks()
        with gather, fmt, page as mock_page:
            app._handle_key("i")
        mock_page.assert_not_called()

    def test_i_guarded_when_searching(self) -> None:
        """With a non-empty search buffer, 'i' is a search character."""
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        seg.searchable = True
        seg.search_buffer = "wo"
        app = self._make_app(seg)
        gather, fmt, page = self._inspect_mocks()
        with gather, fmt, page as mock_page:
            app._handle_key("i")
        mock_page.assert_not_called()
        self.assertEqual(seg.search_buffer, "woi")

    def test_i_ignored_on_other_segments(self) -> None:
        seg = Segment(key="model", label="Model", options=["opus", "sonnet"])
        seg.select_value("opus")
        app = self._make_app(seg)
        gather, fmt, page = self._inspect_mocks()
        with gather, fmt, page as mock_page:
            result = app._handle_key("i")
        mock_page.assert_not_called()
        self.assertIsNone(result)  # not a quit, not a launch

    def test_i_searches_on_other_searchable_segments(self) -> None:
        seg = Segment(key="model", label="Model", options=["opus", "sonnet"])
        seg.searchable = True
        app = self._make_app(seg)
        gather, fmt, page = self._inspect_mocks()
        with gather, fmt, page as mock_page:
            app._handle_key("i")
        mock_page.assert_not_called()
        self.assertEqual(seg.search_buffer, "i")


class InspectAuthShadowFixTests(unittest.TestCase):
    """Pressing 'f' on the inspect page fixes auth shadow when detected."""

    def _make_app(self, seg: Segment) -> app_mod.App:
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        app.bar = mock.MagicMock()
        app.bar.segments = [seg]
        app.bar.focused = seg
        app._flash = ""
        app._show_provenance = False
        app._pending_discovery = {}
        app._bindings = app._build_bindings()
        return app

    def test_f_fixes_auth_shadow_when_detected(self) -> None:
        from claudewheel.profile_ops import FixAuthResult

        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        report = mock.MagicMock(has_auth_shadow=True)
        with (
            mock.patch("claudewheel.profile_info.gather_profile_info",
                       return_value=report),
            mock.patch("claudewheel.profile_info.format_report",
                       return_value=["line"]),
            mock.patch("claudewheel.ui.show_page", return_value="f") as mock_page,
            mock.patch("claudewheel.profile_ops.fix_auth_shadow",
                       return_value=FixAuthResult(ok=True)) as mock_fix,
        ):
            app._show_profile_inspect(seg)
        mock_fix.assert_called_once_with(app.workspace, "work")
        self.assertEqual(app._flash, "Auth shadow fixed")
        # Verify hint shows the fix option
        _, kwargs = mock_page.call_args
        self.assertIn("f: fix auth shadow", kwargs.get("hint", ""))

    def test_other_key_does_not_fix(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        report = mock.MagicMock(has_auth_shadow=True)
        with (
            mock.patch("claudewheel.profile_info.gather_profile_info",
                       return_value=report),
            mock.patch("claudewheel.profile_info.format_report",
                       return_value=["line"]),
            mock.patch("claudewheel.ui.show_page", return_value="q"),
            mock.patch("claudewheel.profile_ops.fix_auth_shadow") as mock_fix,
        ):
            app._show_profile_inspect(seg)
        mock_fix.assert_not_called()
        self.assertEqual(app._flash, "")

    def test_no_shadow_f_ignored(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        report = mock.MagicMock(has_auth_shadow=False)
        with (
            mock.patch("claudewheel.profile_info.gather_profile_info",
                       return_value=report),
            mock.patch("claudewheel.profile_info.format_report",
                       return_value=["line"]),
            mock.patch("claudewheel.ui.show_page", return_value="f") as mock_page,
            mock.patch("claudewheel.profile_ops.fix_auth_shadow") as mock_fix,
        ):
            app._show_profile_inspect(seg)
        mock_fix.assert_not_called()
        self.assertEqual(app._flash, "")
        # Verify hint does NOT offer fix
        _, kwargs = mock_page.call_args
        self.assertNotIn("f: fix auth shadow", kwargs.get("hint", ""))


class ProfileDeleteKeyTests(unittest.TestCase):
    """CTRL_D/DELETE run the profile delete flow (Phase 5c)."""

    def _make_app(self, seg: Segment,
                  state: dict | None = None) -> app_mod.App:
        """Minimal App with a real _handle_key bound and *seg* focused."""
        app = object.__new__(app_mod.App)
        app._locator = BinaryLocator.default()
        app.workspace = mock.MagicMock()
        app.workspace.profiles.enumerate.return_value = []
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        app.cfg = mock.MagicMock()
        app.cfg.state = state if state is not None else {}
        app.bar = mock.MagicMock()
        app.bar.segments = [seg]
        app.bar.focused = seg
        app._flash = ""
        app._show_provenance = False
        app._pending_discovery = {}
        app._bindings = app._build_bindings()
        # The flow re-runs discovery on success; keep it out of unit tests.
        app._refresh_profile_segment = mock.MagicMock()
        return app

    def _report(self, danger: bool = False,
                shared_dirs: dict | None = None) -> mock.MagicMock:
        report = mock.MagicMock()
        report.danger = danger
        report.shared_dirs = shared_dirs if shared_dirs is not None else {}
        report.has_credentials = True
        report.has_token = False
        report.disk_usage_bytes = 2048
        report.active_sessions = 0
        return report

    def _flow_mocks(self, app, report=None, selection="cancel", result=None,
                    raises=None):
        """Patch the lazy imports inside _delete_profile_flow.

        The delete goes through the injected ``app.workspace.profiles`` store, so
        we wire that directly to ``self._store`` (exposed for assertions on
        ``self._store.delete``). Returns (gather, nullcontext, run_selection,
        show_page) -- the second slot preserves the 4-tuple shape used by every
        call site.
        """
        import contextlib
        from claudewheel.profile_store import DeletionResult
        if report is None:
            report = self._report()

        self._store = mock.MagicMock()
        if raises is not None:
            self._store.delete.side_effect = raises
        else:
            if result is None:
                result = DeletionResult(
                    removed_symlinks=0, removed_real=0,
                    removed_from_options=True, removed_from_tokens=True,
                    last_config_purged=True,
                )
            self._store.delete.return_value = result

        # The flow now deletes via the injected workspace, not Workspace.default().
        app.workspace.profiles = self._store

        return (
            mock.patch("claudewheel.profile_info.gather_profile_info",
                       return_value=report),
            contextlib.nullcontext(),
            mock.patch("claudewheel.ui.run_selection",
                       return_value=selection),
            mock.patch("claudewheel.ui.show_page"),
        )

    # -- guards ------------------------------------------------------------

    def test_guarded_on_plus_sentinel(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.selected_value = "+"
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, )
        with gather as mock_gather, ws, sel, page:
            app._handle_key("CTRL_D")
        mock_gather.assert_not_called()
        self._store.delete.assert_not_called()

    def test_guarded_on_empty_selection(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.selected_value = None
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, )
        with gather as mock_gather, ws, sel, page:
            app._handle_key("DELETE")
        mock_gather.assert_not_called()
        self._store.delete.assert_not_called()

    def test_guarded_when_searching(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        seg.searchable = True
        seg.search_buffer = "wo"
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, )
        with gather as mock_gather, ws, sel, page:
            app._handle_key("CTRL_D")
        mock_gather.assert_not_called()
        self._store.delete.assert_not_called()
        self.assertEqual(seg.search_buffer, "wo")  # buffer untouched

    def test_ignored_on_other_segments(self) -> None:
        seg = Segment(key="model", label="Model", options=["opus", "sonnet"])
        seg.select_value("opus")
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, )
        with gather as mock_gather, ws, sel, page:
            result = app._handle_key("CTRL_D")
        mock_gather.assert_not_called()
        self._store.delete.assert_not_called()
        self.assertIsNone(result)  # not a quit, not a launch

    def test_delete_key_triggers_flow_like_ctrl_d(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, selection="cancel")
        with gather as mock_gather, ws, sel, page:
            app._handle_key("DELETE")
        mock_gather.assert_called_once_with(app.workspace, "work")

    # -- danger hard-block ---------------------------------------------------

    def test_danger_shows_hard_block_page_and_never_deletes(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        report = self._report(danger=True,
                              shared_dirs={"projects": "real-dir",
                                           "todos": "intact"})
        gather, ws, sel, page = self._flow_mocks(app, report=report)
        with gather, ws, sel as mock_sel, page as mock_page:
            app._handle_key("CTRL_D")
        self._store.delete.assert_not_called()
        mock_sel.assert_not_called()
        mock_page.assert_called_once()
        title, lines = mock_page.call_args[0][0], mock_page.call_args[0][1]
        self.assertIn("work", title)
        joined = "\n".join(lines)
        self.assertIn("projects", joined)
        self.assertNotIn("todos:", joined)  # intact dirs are not at risk
        self.assertIn("--force-delete-data", joined)

    # -- confirm ------------------------------------------------------------

    def test_cancel_choice_does_not_delete(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, selection="cancel")
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")
        self._store.delete.assert_not_called()
        self.assertEqual(seg.value, "work")

    def test_escape_from_confirm_does_not_delete(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, selection=None)
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")
        self._store.delete.assert_not_called()

    def test_confirm_defaults_to_cancel(self) -> None:
        """Cancel is first in the options and pre-focused via initial_key."""
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, selection="cancel")
        with gather, ws, sel as mock_sel, page:
            app._handle_key("CTRL_D")
        args, kwargs = mock_sel.call_args
        options = args[1]
        self.assertEqual(options[0][0], "cancel")
        self.assertEqual(kwargs.get("initial_key"), "cancel")

    # -- delete + cleanup -----------------------------------------------------

    def test_confirm_delete_calls_store_without_force(self) -> None:
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = self._make_app(seg)
        gather, ws, sel, page = self._flow_mocks(app, selection="delete")
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")
        # TUI never forces: no allow_data_destruction, running is pre-checked.
        self._store.delete.assert_called_once_with("work")

    def test_success_runs_all_cleanup_steps(self) -> None:
        seg = _make_profile_segment(discovered=["work", "other"],
                                    pinned=["work"])
        seg.select_value("work")
        seg.state.metadata = {"work": {"has_token": True},
                              "other": {"has_token": True}}
        state = {"last_config": {"profile": "work", "model": "opus"}}
        app = self._make_app(seg, state=state)
        gather, ws, sel, page = self._flow_mocks(app, selection="delete")
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")
        # 1. last_config purged in memory (disk purge happened in the store)
        self.assertNotIn("profile", state["last_config"])
        self.assertEqual(state["last_config"]["model"], "opus")
        # 2. unpinned
        self.assertNotIn("work", seg.state._pinned)
        # 3. metadata dropped
        self.assertNotIn("work", seg.state.metadata)
        self.assertIn("other", seg.state.metadata)
        # 4. selection cleared
        self.assertIsNone(seg.selected_value)
        # 5. segment refreshed
        app._refresh_profile_segment.assert_called_once_with(seg)
        # flash
        self.assertIn("Deleted profile 'work'", app._flash)

    def test_success_leaves_other_profiles_last_config(self) -> None:
        """last_config naming a different profile is left alone in memory."""
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        state = {"last_config": {"profile": "other"}}
        app = self._make_app(seg, state=state)
        gather, ws, sel, page = self._flow_mocks(app, selection="delete")
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")
        self.assertEqual(state["last_config"]["profile"], "other")

    def test_purged_last_config_not_resurrected_by_save(self) -> None:
        """After a TUI delete, a wholesale state save must not write the
        deleted profile back into last_config on disk."""
        from claudewheel.state import save_launch_state

        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        state = {"last_config": {"profile": "work", "model": "opus"}}
        app = self._make_app(seg, state=state)
        gather, ws, sel, page = self._flow_mocks(app, selection="delete")
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")

        # Simulate the app's later wholesale save: selections come from the
        # segments (profile now None, so it is dropped from last_config).
        saved = {}
        app.cfg.save_state = lambda: saved.update(app.cfg.state)
        save_launch_state(app.cfg, {"profile": seg.value, "model": "opus"})
        self.assertNotIn("profile", saved["last_config"])

    # -- refusals -------------------------------------------------------------

    def test_running_profile_blocked_and_skips_cleanup(self) -> None:
        """A profile with active sessions is refused (TUI policy) before the
        store is touched; no cleanup happens."""
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        state = {"last_config": {"profile": "work"}}
        app = self._make_app(seg, state=state)
        report = self._report()
        report.active_sessions = 1
        gather, ws, sel, page = self._flow_mocks(app, report=report,
                                                 selection="delete")
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")
        self.assertIn("active sessions", app._flash)
        self._store.delete.assert_not_called()
        # No cleanup: selection and last_config untouched
        self.assertEqual(seg.value, "work")
        self.assertEqual(state["last_config"]["profile"], "work")
        app._refresh_profile_segment.assert_not_called()

    def test_store_refusal_flashes_message(self) -> None:
        """A ValueError refusal from the store surfaces as a flash; no cleanup."""
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        state = {"last_config": {"profile": "work"}}
        app = self._make_app(seg, state=state)
        gather, ws, sel, page = self._flow_mocks(app, 
            selection="delete",
            raises=ValueError("Profile 'work' holds REAL data at: projects"))
        with gather, ws, sel, page:
            app._handle_key("CTRL_D")
        self.assertIn("projects", app._flash)
        self.assertEqual(seg.value, "work")
        self.assertEqual(state["last_config"]["profile"], "work")
        app._refresh_profile_segment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
