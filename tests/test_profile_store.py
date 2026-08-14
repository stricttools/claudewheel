"""Parity and contract tests for claudewheel.profile_store.ProfileStore."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from claudewheel.profile_data import (
    PROFILE_DATA_DIRNAME,
    TOKEN_FILE_NAME,
    ProfileDataStore,
)
from claudewheel.profile_store import Profile, ProfileStore
from claudewheel.tokens import TokenStoreError
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    FakeArchiver,
    SandboxHomeTestCase,
    write_token_entry,
)


def _tree_mode(root: Path, dir_mode: int, file_mode: int) -> None:
    """chmod every file/dir under *root* (inclusive). Files first, then dirs."""
    dirs: list[str | Path] = [root]
    files: list[str] = []
    for dp, dns, fns in os.walk(root):
        for d in dns:
            dirs.append(os.path.join(dp, d))
        for f in fns:
            files.append(os.path.join(dp, f))
    for f in files:
        os.chmod(f, file_mode)
    for dpath in dirs:
        os.chmod(dpath, dir_mode)


class ProfileStoreEnumerateTests(SandboxHomeTestCase):
    """ProfileStore.enumerate() against pinned expectations for every fixture case.

    These were formerly parity tests comparing against the (now deleted)
    ``discovery.discover_profiles``; the enumeration rules are now pinned by the
    explicit (name, path, has_credentials, has_token) tuples asserted inline.
    """

    def _store(self) -> ProfileStore:
        return ProfileStore(
            profiles_dir=self.sandbox_paths["PROFILES_DIR"],
            claude_dir=self.home / ".claude",
        )

    def _tuples(self, profiles: list[Profile]) -> list[tuple[str, Path, bool, bool]]:
        return [(p.name, p.path, p.has_credentials, p.has_token) for p in profiles]

    # --- default profile variants ---------------------------------------

    def test_default_with_credentials(self) -> None:
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / ".credentials.json").write_text("{}")
        store = self._store().enumerate()
        self.assertEqual(
            self._tuples(store),
            [("default", self.home / ".claude", True, False)],
        )

    def test_default_dir_without_credentials_and_no_token_is_visible(self) -> None:
        """Lenient rule: ~/.claude qualifies as 'default' whenever it is a dir.

        Claude Code manages ~/.claude and may store credentials elsewhere (e.g.
        macOS Keychain), so cw discovers it even without .credentials.json.
        has_credentials/has_token reflect reality (both False here).
        """
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)  # dir exists, no credentials, no token
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("default", self.home / ".claude", False, False)],
        )

    def test_default_missing_dir_is_invisible(self) -> None:
        """No ~/.claude directory at all -> no 'default' profile."""
        self.assertEqual(self._store().enumerate(), [])

    def test_default_token_only(self) -> None:
        """A ~/.claude holding only a stored token entry, no credentials."""
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        write_token_entry(d, {"token": "tok-default"})
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("default", self.home / ".claude", False, True)],
        )

    # --- profiles_dir subdir variants -----------------------------------

    def test_profile_settings_only(self) -> None:
        p = self.sandbox_paths["PROFILES_DIR"] / "alpha"
        p.mkdir(parents=True, exist_ok=True)
        (p / "settings.json").write_text("{}")
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("alpha", p, False, False)],
        )

    def test_profile_credentials_only(self) -> None:
        p = self.make_profile("beta", credentials=True)  # writes .credentials.json
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("beta", p, True, False)],
        )

    def test_profile_both_files(self) -> None:
        p = self.sandbox_paths["PROFILES_DIR"] / "gamma"
        p.mkdir(parents=True, exist_ok=True)
        (p / ".credentials.json").write_text("{}")
        (p / "settings.json").write_text("{}")
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("gamma", p, True, False)],
        )

    def test_empty_profile_dir_is_invisible(self) -> None:
        (self.sandbox_paths["PROFILES_DIR"] / "empty").mkdir(
            parents=True, exist_ok=True
        )
        self.assertEqual(self._store().enumerate(), [])

    # --- claudewheel data dir -------------------------------------------

    def test_profile_carrying_only_the_data_dir_is_discovered(self) -> None:
        """The data directory alone qualifies a profile (no cred/settings)."""
        p = self.sandbox_paths["PROFILES_DIR"] / "delta"
        (p / PROFILE_DATA_DIRNAME).mkdir(parents=True, exist_ok=True)
        self.assertEqual(
            self._tuples(self._store().enumerate()),
            [("delta", p, False, False)],
        )

    def test_name_with_no_directory_is_invisible(self) -> None:
        self.assertEqual(self._store().enumerate(), [])

    def test_token_marking_on_credentialed_profile(self) -> None:
        p = self.make_profile("epsilon", credentials=True)
        write_token_entry(p, {"token": "tok-eps"})
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
        # A profile carrying only claudewheel's own data directory.
        tp = self.sandbox_paths["PROFILES_DIR"] / "beta"
        (tp / PROFILE_DATA_DIRNAME).mkdir(parents=True, exist_ok=True)
        names = [p.name for p in self._store().enumerate()]
        self.assertEqual(names, ["alpha", "beta", "default", "mu", "zeta"])


class ProfileStoreContractTests(SandboxHomeTestCase):
    """env(), corrupt-token handling, read-only resolution, and workspace wiring."""

    def _store(self) -> ProfileStore:
        return ProfileStore(
            profiles_dir=self.sandbox_paths["PROFILES_DIR"],
            claude_dir=self.home / ".claude",
        )

    def test_env_happy_path(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"token": "tok-alpha"})
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

    def test_env_injects_plan_tier_from_token_entry(self) -> None:
        """A token entry carrying plan-tier fields yields the matching env vars."""
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(
            p,
            {
                "token": "tok-alpha",
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            },
        )
        env = self._store().env("alpha")
        self.assertEqual(env["CLAUDE_CODE_SUBSCRIPTION_TYPE"], "max")
        self.assertEqual(env["CLAUDE_CODE_RATE_LIMIT_TIER"], "default_claude_max_20x")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")

    def test_env_plan_tier_independent_of_token(self) -> None:
        """Tier fields are injected even when the entry carries no token."""
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"subscriptionType": "pro"})
        env = self._store().env("alpha")
        self.assertEqual(env["CLAUDE_CODE_SUBSCRIPTION_TYPE"], "pro")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("CLAUDE_CODE_RATE_LIMIT_TIER", env)

    def test_env_omits_plan_tier_when_absent(self) -> None:
        """An entry without tier fields injects neither var."""
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"token": "tok-alpha"})
        env = self._store().env("alpha")
        self.assertNotIn("CLAUDE_CODE_SUBSCRIPTION_TYPE", env)
        self.assertNotIn("CLAUDE_CODE_RATE_LIMIT_TIER", env)

    def test_env_unknown_subscription_type_raises(self) -> None:
        """An unrecognized subscriptionType is a hard error naming the valid values."""
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"token": "t", "subscriptionType": "Max"})
        with self.assertRaises(ValueError) as ctx:
            self._store().env("alpha")
        msg = str(ctx.exception)
        self.assertIn("'Max'", msg)
        self.assertIn("alpha", msg)
        self.assertIn("max", msg)

    def test_env_unknown_rate_limit_tier_raises(self) -> None:
        """An unrecognized rateLimitTier is a hard error naming the valid values."""
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"token": "t", "rateLimitTier": "max_20x"})
        with self.assertRaises(ValueError) as ctx:
            self._store().env("alpha")
        self.assertIn("'max_20x'", str(ctx.exception))

    def test_env_default_ignores_plan_tier(self) -> None:
        """The vanilla default resolves empty even with a token entry carrying tiers."""
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        write_token_entry(d, {"token": "t", "subscriptionType": "max"})
        self.assertEqual(self._store().env("default"), {})

    def test_env_unknown_name_raises_listing_available(self) -> None:
        self.make_profile("alpha", credentials=True)
        self.make_profile("beta", credentials=True)
        with self.assertRaises(ValueError) as ctx:
            self._store().env("nope")
        msg = str(ctx.exception)
        self.assertIn("'nope'", msg)
        self.assertIn("alpha", msg)
        self.assertIn("beta", msg)

    def _corrupt_token(self, profile_dir: Path) -> None:
        """Leave an unparseable token entry in *profile_dir*."""
        path = write_token_entry(profile_dir, {"token": "t"})
        path.write_text("{invalid json")

    def test_enumerate_on_corrupt_token_entry_raises(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        self._corrupt_token(p)
        with self.assertRaises(TokenStoreError):
            self._store().enumerate()

    def test_discover_swallow_survives_a_corrupt_entry(self) -> None:
        """The swallow policy leaves that profile tokenless rather than raising."""
        p = self.make_profile("alpha", credentials=True)
        self._corrupt_token(p)
        result = self._store().discover(on_corrupt_tokens="swallow")
        self.assertEqual(
            [(x.name, x.path, x.has_credentials, x.has_token) for x in result],
            [("alpha", p, True, False)],
        )

    def test_discover_raise_propagates_a_corrupt_entry(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        self._corrupt_token(p)
        with self.assertRaises(TokenStoreError):
            self._store().discover(on_corrupt_tokens="raise")

    def test_one_corrupt_entry_does_not_hide_other_profiles(self) -> None:
        """Per-profile reads: a bad entry only affects its own profile."""
        bad = self.make_profile("alpha", credentials=True)
        good = self.make_profile("beta", credentials=True)
        write_token_entry(good, {"token": "tok-beta"})
        self._corrupt_token(bad)
        result = self._store().discover(on_corrupt_tokens="swallow")
        self.assertEqual(
            [(x.name, x.has_token) for x in result],
            [("alpha", False), ("beta", True)],
        )

    def test_discover_rejects_unknown_mode(self) -> None:
        """An unrecognized corrupt-tokens mode is a hard ValueError."""
        with self.assertRaises(ValueError):
            self._store().discover(on_corrupt_tokens="ignore")  # type: ignore[arg-type]

    # --- single-profile resolution reads only that profile ---------------

    def test_env_resolves_a_good_profile_despite_a_corrupt_sibling(self) -> None:
        """Resolving beta must not care that alpha's token file is unreadable."""
        bad = self.make_profile("alpha", credentials=True)
        good = self.make_profile("beta", credentials=True)
        write_token_entry(good, {"token": "tok-beta"})
        self._corrupt_token(bad)
        env = self._store().env("beta")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(good))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-beta")

    def test_env_on_the_corrupt_profile_itself_raises_naming_it(self) -> None:
        """The profile being resolved still hard-errors, naming its own file."""
        bad = self.make_profile("alpha", credentials=True)
        good = self.make_profile("beta", credentials=True)
        write_token_entry(good, {"token": "tok-beta"})
        self._corrupt_token(bad)
        with self.assertRaises(TokenStoreError) as ctx:
            self._store().env("alpha")
        self.assertIn("alpha", str(ctx.exception))

    def test_env_unknown_name_raises_valueerror_despite_a_corrupt_profile(self) -> None:
        """Listing the available names must not read anyone's token file."""
        bad = self.make_profile("alpha", credentials=True)
        self.make_profile("beta", credentials=True)
        self._corrupt_token(bad)
        with self.assertRaises(ValueError) as ctx:
            self._store().env("nope")
        msg = str(ctx.exception)
        self.assertIn("alpha", msg)
        self.assertIn("beta", msg)

    def test_env_opens_only_the_resolved_profiles_token_file(self) -> None:
        """The secondary effect: no other profile's secret file is opened."""
        self.make_profile("alpha", credentials=True)
        good = self.make_profile("beta", credentials=True)
        write_token_entry(self.sandbox_paths["PROFILES_DIR"] / "alpha", {"token": "a"})
        write_token_entry(good, {"token": "tok-beta"})

        read_files: list[Path] = []
        real_load = ProfileDataStore.load

        def recording_load(store: ProfileDataStore) -> dict[str, object]:
            read_files.append(store.token_file)
            return real_load(store)

        with patch.object(ProfileDataStore, "load", recording_load):
            self._store().env("beta")

        self.assertTrue(read_files, "env() read no token file at all")
        self.assertEqual(
            {f for f in read_files},
            {good / PROFILE_DATA_DIRNAME / TOKEN_FILE_NAME},
        )

    def test_get_survives_a_corrupt_sibling(self) -> None:
        """get() resolves one profile: a corrupt neighbour is not its business."""
        bad = self.make_profile("alpha", credentials=True)
        good = self.make_profile("beta", credentials=True)
        write_token_entry(good, {"token": "tok-beta"})
        self._corrupt_token(bad)
        got = self._store().get("beta")
        assert got is not None
        self.assertEqual(
            (got.name, got.path, got.has_credentials, got.has_token),
            ("beta", good, True, True),
        )

    def test_get_on_the_corrupt_profile_itself_raises_naming_it(self) -> None:
        bad = self.make_profile("alpha", credentials=True)
        self.make_profile("beta", credentials=True)
        self._corrupt_token(bad)
        with self.assertRaises(TokenStoreError) as ctx:
            self._store().get("alpha")
        self.assertIn("alpha", str(ctx.exception))

    def test_get_unknown_name_returns_none_despite_a_corrupt_profile(self) -> None:
        bad = self.make_profile("alpha", credentials=True)
        self._corrupt_token(bad)
        self.assertIsNone(self._store().get("missing"))

    def test_env_on_readonly_tree_succeeds_with_zero_writes(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"token": "tok-alpha"})
        # Lock the whole sandbox home down: dirs r-x, files r--.
        _tree_mode(self.home, dir_mode=0o555, file_mode=0o444)
        self.addCleanup(_tree_mode, self.home, 0o755, 0o644)

        env = self._store().env("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(p))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")

    def test_path_for_default_is_sandbox_claude(self) -> None:
        self.assertEqual(self._store().path_for("default"), self.home / ".claude")

    def test_env_default_returns_empty(self) -> None:
        """The vanilla default resolves to an EMPTY env: no config dir, no token.

        Even a 'default' tokens key must not inject a token.
        """
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        write_token_entry(d, {"token": "tok-default"})
        env = self._store().env("default")
        self.assertEqual(env, {})

    def test_env_default_missing_dir_raises(self) -> None:
        """With no ~/.claude dir, 'default' is not enumerable so env raises."""
        with self.assertRaises(ValueError):
            self._store().env("default")

    def test_get_returns_profile_or_none(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        store = self._store()
        got = store.get("alpha")
        self.assertIsInstance(got, Profile)
        assert got is not None
        self.assertEqual(got.path, p)
        self.assertEqual(got.config_dir, p)
        self.assertIsNone(store.get("missing"))

    def test_workspace_profiles_returns_working_store(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"token": "tok-alpha"})
        ws = Workspace.open(root=self.launcher_dir, claude_dir=self.home / ".claude")
        store = ws.profiles
        self.assertIsInstance(store, ProfileStore)
        self.assertEqual(store.path_for("alpha"), p)
        env = store.env("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(p))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")


class LaunchEnvKeyTests(SandboxHomeTestCase):
    """The launch environment's key list, checked as a list rather than
    variable by variable.

    Per-variable tests can only ever cover the variables somebody remembered to
    write a test for; this one fails when the two sides of the list disagree,
    whichever side gained the entry.
    """

    def _store(self) -> ProfileStore:
        return ProfileStore(
            profiles_dir=self.sandbox_paths["PROFILES_DIR"],
            claude_dir=self.home / ".claude",
        )

    def test_a_fully_declared_profile_yields_exactly_the_declared_keys(self) -> None:
        from claudewheel.profile_store import PROFILE_ENV_KEYS

        p = self.make_profile("alpha", credentials=True)
        write_token_entry(
            p,
            {
                "token": "tok",
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            },
        )
        env = self._store().env("alpha")
        self.assertEqual(set(env), set(PROFILE_ENV_KEYS))

    def test_marketplace_autoinstall_is_suppressed_for_a_named_profile(self) -> None:
        p = self.make_profile("alpha", credentials=True)
        write_token_entry(p, {"token": "tok"})
        env = self._store().env("alpha")
        self.assertEqual(
            env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"], "1"
        )

    def test_suppression_does_not_depend_on_a_token(self) -> None:
        """Every named profile gets it, tokenless ones included."""
        self.make_profile("alpha", credentials=True)
        env = self._store().env("alpha")
        self.assertIn("CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL", env)

    def test_the_vanilla_profile_declares_nothing(self) -> None:
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self._store().env("default"), {})


class ReservedNameTests(SandboxHomeTestCase):
    """The one query every caller asks before offering to destroy a profile."""

    def _store(self) -> ProfileStore:
        return ProfileStore(
            profiles_dir=self.sandbox_paths["PROFILES_DIR"],
            claude_dir=self.home / ".claude",
        )

    def test_the_vanilla_profile_is_reserved(self) -> None:
        reason = self._store().reserved_reason("default")
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("default", reason)
        self.assertIn("~/.claude", reason)

    def test_the_reason_suggests_no_destructive_command(self) -> None:
        """A command that can never succeed must not be advertised."""
        reason = self._store().reserved_reason("default")
        assert reason is not None
        self.assertNotIn("--force", reason)
        self.assertNotIn("profile delete", reason)

    def test_an_ordinary_name_is_not_reserved(self) -> None:
        self.assertIsNone(self._store().reserved_reason("work"))

    def test_the_reserved_set_is_stated_once(self) -> None:
        from claudewheel.profile_store import RESERVED_PROFILE_NAMES

        self.assertEqual(RESERVED_PROFILE_NAMES, ("default",))

    def test_delete_refuses_with_the_same_reason(self) -> None:
        """The store's own backstop reads the query rather than a second copy."""
        store = self._store()
        with self.assertRaises(ValueError) as ctx:
            store.delete("default", archiver=FakeArchiver())
        self.assertEqual(str(ctx.exception), store.reserved_reason("default"))


if __name__ == "__main__":
    import unittest

    unittest.main()
