"""Parity and contract tests for claudewheel.profile_store.ProfileStore."""

from __future__ import annotations

import os

from claudewheel.profile_store import Profile, ProfileStore
from claudewheel.tokens import TokenStore, TokenStoreError
from claudewheel.workspace import Workspace
from tests.wheelhelpers import SandboxHomeTestCase, write_json


def _tree_mode(root, dir_mode: int, file_mode: int) -> None:
    """chmod every file/dir under *root* (inclusive). Files first, then dirs."""
    dirs = [root]
    files = []
    for dp, dns, fns in os.walk(root):
        for d in dns:
            dirs.append(os.path.join(dp, d))
        for f in fns:
            files.append(os.path.join(dp, f))
    for f in files:
        os.chmod(f, file_mode)
    for d in dirs:
        os.chmod(d, dir_mode)


class ProfileStoreParityTests(SandboxHomeTestCase):
    """ProfileStore.enumerate() must match discovery.discover_profiles() tuple-for-tuple."""

    def _store(self) -> ProfileStore:
        return ProfileStore(
            profiles_dir=self.sandbox_paths["PROFILES_DIR"],
            claude_dir=self.home / ".claude",
            token_store=TokenStore(self.sandbox_paths["TOKENS_FILE"]),
        )

    def _tuples(self, profiles) -> list[tuple]:
        return [(p.name, p.path, p.has_credentials, p.has_token) for p in profiles]

    def _assert_parity(self) -> None:
        """Run both engines against the current sandbox fixtures; assert identical."""
        import claudewheel.discovery as disc

        # Rebind discovery's import-time path constants at the sandbox. Path.home
        # is already poisoned by the base class, so ~/.claude resolves in-sandbox.
        self.patch_constants_across(
            [disc], ["PROFILES_DIR", "TOKENS_FILE", "SHARED_DIR", "SKILLS_DIR"]
        )
        discovery_tuples = self._tuples(disc.discover_profiles())
        store_tuples = self._tuples(self._store().enumerate())
        self.assertEqual(store_tuples, discovery_tuples)

    # --- default profile variants ---------------------------------------

    def test_default_with_credentials(self) -> None:
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / ".credentials.json").write_text("{}")
        self._assert_parity()
        store = self._store().enumerate()
        self.assertEqual(
            self._tuples(store),
            [("default", self.home / ".claude", True, False)],
        )

    def test_default_dir_without_credentials_and_no_token_is_invisible(self) -> None:
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)  # dir exists, no credentials, no token
        self._assert_parity()
        self.assertEqual(self._store().enumerate(), [])

    def test_default_token_only(self) -> None:
        """Corrected-rule case: tokens key 'default', dir exists, no credentials."""
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        write_json(self.sandbox_paths["TOKENS_FILE"], {"default": "tok-default"})
        self._assert_parity()
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("default", self.home / ".claude", False, True)],
        )

    # --- profiles_dir subdir variants -----------------------------------

    def test_profile_settings_only(self) -> None:
        p = self.sandbox_paths["PROFILES_DIR"] / "alpha"
        p.mkdir(parents=True, exist_ok=True)
        (p / "settings.json").write_text("{}")
        self._assert_parity()
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("alpha", p, False, False)],
        )

    def test_profile_credentials_only(self) -> None:
        p = self.make_profile("beta", credentials=True)  # writes .credentials.json
        self._assert_parity()
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("beta", p, True, False)],
        )

    def test_profile_both_files(self) -> None:
        p = self.sandbox_paths["PROFILES_DIR"] / "gamma"
        p.mkdir(parents=True, exist_ok=True)
        (p / ".credentials.json").write_text("{}")
        (p / "settings.json").write_text("{}")
        self._assert_parity()
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("gamma", p, True, False)],
        )

    def test_empty_profile_dir_is_invisible(self) -> None:
        (self.sandbox_paths["PROFILES_DIR"] / "empty").mkdir(parents=True, exist_ok=True)
        self._assert_parity()
        self.assertEqual(self._store().enumerate(), [])

    # --- token entry variants -------------------------------------------

    def test_token_entry_with_existing_dir(self) -> None:
        p = self.sandbox_paths["PROFILES_DIR"] / "delta"
        p.mkdir(parents=True, exist_ok=True)  # empty dir, no cred/settings
        write_json(self.sandbox_paths["TOKENS_FILE"], {"delta": "tok-delta"})
        self._assert_parity()
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("delta", p, False, True)],
        )

    def test_token_entry_without_dir_is_invisible(self) -> None:
        write_json(self.sandbox_paths["TOKENS_FILE"], {"ghost": "tok-ghost"})
        self._assert_parity()
        self.assertEqual(self._store().enumerate(), [])

    def test_token_marking_on_credentialed_profile(self) -> None:
        p = self.make_profile("epsilon", credentials=True)
        write_json(self.sandbox_paths["TOKENS_FILE"], {"epsilon": "tok-eps"})
        self._assert_parity()
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("epsilon", p, True, True)],
        )

    # --- sorting ---------------------------------------------------------

    def test_name_sorting(self) -> None:
        self.make_profile("zeta", credentials=True)
        self.make_profile("alpha", credentials=True)
        self.make_profile("mu", credentials=True)
        # A default profile too, to test cross-source sort placement.
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / ".credentials.json").write_text("{}")
        # A token-only profile with an existing dir.
        tp = self.sandbox_paths["PROFILES_DIR"] / "beta"
        tp.mkdir(parents=True, exist_ok=True)
        write_json(self.sandbox_paths["TOKENS_FILE"], {"beta": "tok-beta"})
        self._assert_parity()
        names = [p.name for p in self._store().enumerate()]
        self.assertEqual(names, ["alpha", "beta", "default", "mu", "zeta"])


class ProfileStoreContractTests(SandboxHomeTestCase):
    """env(), corrupt-token handling, read-only resolution, and workspace wiring."""

    def _store(self) -> ProfileStore:
        return ProfileStore(
            profiles_dir=self.sandbox_paths["PROFILES_DIR"],
            claude_dir=self.home / ".claude",
            token_store=TokenStore(self.sandbox_paths["TOKENS_FILE"]),
        )

    def test_env_happy_path(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        write_json(self.sandbox_paths["TOKENS_FILE"], {"alpha": "tok-alpha"})
        env = self._store().env("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(p))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")

    def test_env_tokenless_profile_omits_token(self) -> None:
        p = self.sandbox_paths["PROFILES_DIR"] / "alpha"
        p.mkdir(parents=True, exist_ok=True)
        (p / "settings.json").write_text("{}")
        env = self._store().env("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(p))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_env_unknown_name_raises_listing_available(self) -> None:
        self.make_profile("alpha", credentials=True)
        self.make_profile("beta", credentials=True)
        with self.assertRaises(ValueError) as ctx:
            self._store().env("nope")
        msg = str(ctx.exception)
        self.assertIn("'nope'", msg)
        self.assertIn("alpha", msg)
        self.assertIn("beta", msg)

    def test_enumerate_explicit_empty_tokens_survives_corrupt_file(self) -> None:
        """enumerate(tokens={}) never touches the file, so a corrupt one can't raise."""
        p = self.make_profile("alpha", credentials=True)
        self.sandbox_paths["TOKENS_FILE"].write_text("{invalid json")
        result = self._store().enumerate(tokens={})
        self.assertEqual(
            [(x.name, x.path, x.has_credentials, x.has_token) for x in result],
            [("alpha", p, True, False)],
        )

    def test_enumerate_none_on_corrupt_tokens_raises(self) -> None:
        self.make_profile("alpha", credentials=True)
        self.sandbox_paths["TOKENS_FILE"].write_text("{invalid json")
        with self.assertRaises(TokenStoreError):
            self._store().enumerate()

    def test_env_on_readonly_tree_succeeds_with_zero_writes(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        write_json(self.sandbox_paths["TOKENS_FILE"], {"alpha": "tok-alpha"})
        # Lock the whole sandbox home down: dirs r-x, files r--.
        _tree_mode(self.home, dir_mode=0o555, file_mode=0o444)
        self.addCleanup(_tree_mode, self.home, 0o755, 0o644)

        env = self._store().env("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(p))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")

    def test_path_for_default_is_sandbox_claude(self) -> None:
        self.assertEqual(self._store().path_for("default"), self.home / ".claude")

    def test_get_returns_profile_or_none(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        store = self._store()
        got = store.get("alpha")
        self.assertIsInstance(got, Profile)
        self.assertEqual(got.path, p)
        self.assertEqual(got.config_dir, p)
        self.assertIsNone(store.get("missing"))

    def test_workspace_profiles_returns_working_store(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        write_json(self.sandbox_paths["TOKENS_FILE"], {"alpha": "tok-alpha"})
        ws = Workspace.open(root=self.launcher_dir, claude_dir=self.home / ".claude")
        store = ws.profiles
        self.assertIsInstance(store, ProfileStore)
        self.assertEqual(store.path_for("alpha"), p)
        env = store.env("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(p))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")


if __name__ == "__main__":
    import unittest

    unittest.main()
