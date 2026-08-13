"""Tests for the per-profile claudewheel data store (token entry + modes)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from claudewheel.profile_data import (
    PROFILE_DATA_DIRNAME,
    PROFILE_DATA_DIR_MODE,
    TOKEN_FILE_MODE,
    TOKEN_FILE_NAME,
    ProfileDataStore,
)
from claudewheel.tokens import (
    EXPIRY_UNKNOWN_FIELD,
    TOKEN_TTL_DAYS,
    TokenExpiryDisposition,
    TokenStoreError,
    plan_by_key,
)

TTL = TokenExpiryDisposition.TTL
UNKNOWN = TokenExpiryDisposition.UNKNOWN

# Every token write states a plan; these two are the ones used throughout.
MAX_20X = plan_by_key("max-20x")
PRO = plan_by_key("pro")


class ProfileDataStoreTestCase(unittest.TestCase):
    """Base: a tmpdir profile directory with a store over it."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile_dir = Path(self._tmp.name) / "work"
        self.profile_dir.mkdir()
        self.store = ProfileDataStore(self.profile_dir)

    def mode_of(self, path: Path) -> int:
        return path.stat().st_mode & 0o777


class PathsTests(ProfileDataStoreTestCase):
    """The store derives every path from the profile dir alone."""

    def test_data_dir_is_dot_prefixed_subdir(self) -> None:
        self.assertEqual(self.store.data_dir, self.profile_dir / PROFILE_DATA_DIRNAME)
        self.assertTrue(PROFILE_DATA_DIRNAME.startswith("."))

    def test_token_file_inside_data_dir(self) -> None:
        self.assertEqual(self.store.token_file, self.store.data_dir / TOKEN_FILE_NAME)

    def test_construction_creates_nothing(self) -> None:
        ProfileDataStore(self.profile_dir)
        self.assertFalse(self.store.data_dir.exists())
        self.assertFalse(self.store.exists())


class RoundTripTests(ProfileDataStoreTestCase):
    """A token written for one profile reads back, at the intended modes."""

    def test_write_then_read(self) -> None:
        self.store.write_token("tok-abc", expiry=TTL, plan=MAX_20X)
        self.assertEqual(self.store.token(), "tok-abc")
        self.assertTrue(self.store.has_token())
        self.assertTrue(self.store.exists())

    def test_token_file_mode_is_restrictive(self) -> None:
        self.store.write_token("tok-abc", expiry=TTL, plan=MAX_20X)
        self.assertEqual(self.mode_of(self.store.token_file), TOKEN_FILE_MODE)
        self.assertEqual(TOKEN_FILE_MODE, 0o600)

    def test_data_dir_mode_is_owner_only(self) -> None:
        self.store.write_token("tok-abc", expiry=TTL, plan=MAX_20X)
        self.assertEqual(self.mode_of(self.store.data_dir), PROFILE_DATA_DIR_MODE)
        self.assertEqual(PROFILE_DATA_DIR_MODE, 0o700)

    def test_dir_mode_fixed_on_a_preexisting_loose_dir(self) -> None:
        self.store.data_dir.mkdir()
        self.store.data_dir.chmod(0o755)
        self.store.write_token("tok-abc", expiry=TTL, plan=MAX_20X)
        self.assertEqual(self.mode_of(self.store.data_dir), PROFILE_DATA_DIR_MODE)

    def test_entry_is_a_bare_object_not_keyed_by_name(self) -> None:
        self.store.write_token("tok-abc", expiry=TTL, plan=MAX_20X)
        entry = json.loads(self.store.token_file.read_text())
        self.assertEqual(entry["token"], "tok-abc")
        self.assertNotIn("work", entry)

    def test_missing_file_reads_as_no_data(self) -> None:
        self.assertEqual(self.store.load(), {})
        self.assertIsNone(self.store.token())
        self.assertFalse(self.store.has_token())
        self.assertIsNone(self.store.expiry())
        self.assertEqual(self.store.tier(), (None, None))
        self.assertEqual(self.store.plan_env(), {})

    def test_write_replaces_a_previous_entry(self) -> None:
        self.store.write_token("tok-old", expiry=TTL, plan=MAX_20X)
        self.store.write_token("tok-new", expiry=UNKNOWN, plan=PRO)
        entry = self.store.load()
        self.assertEqual(entry["token"], "tok-new")
        self.assertNotIn("expires_at", entry)

    def test_replacing_a_token_invalidates_the_declared_plan(self) -> None:
        """The new token carries the plan stated for IT, never the old one's.

        A plan declared for a retired token says nothing about the account the
        new one belongs to, so the entry is rebuilt rather than merged into and
        the caller has to state a plan again.
        """
        self.store.write_token("tok-old", expiry=TTL, plan=MAX_20X)
        self.store.write_token("tok-new", expiry=TTL, plan=PRO)
        entry = self.store.load()
        self.assertEqual(entry["subscriptionType"], "pro")
        self.assertNotIn("rateLimitTier", entry)


class EntryFormatTests(ProfileDataStoreTestCase):
    """Every field the central store held per profile survives the move."""

    def test_ttl_disposition_records_created_and_expires(self) -> None:
        today = date(2026, 3, 1)
        self.store.write_token("tok", expiry=TTL, plan=MAX_20X, today=today)
        entry = self.store.load()
        self.assertEqual(entry["created"], "2026-03-01")
        self.assertEqual(
            entry["expires_at"], (today + timedelta(days=TOKEN_TTL_DAYS)).isoformat()
        )
        self.assertNotIn(EXPIRY_UNKNOWN_FIELD, entry)

    def test_unknown_disposition_records_the_marker_and_no_expiry(self) -> None:
        self.store.write_token(
            "tok", expiry=UNKNOWN, plan=MAX_20X, today=date(2026, 3, 1)
        )
        entry = self.store.load()
        self.assertTrue(entry[EXPIRY_UNKNOWN_FIELD])
        self.assertNotIn("expires_at", entry)

    def test_plan_fields_stored_on_write(self) -> None:
        self.store.write_token("tok", expiry=TTL, plan=MAX_20X)
        self.assertEqual(self.store.tier(), ("default_claude_max_20x", "max"))
        self.assertTrue(self.store.declares_plan())

    def test_a_plan_with_no_rate_limit_tier_writes_only_its_field(self) -> None:
        self.store.write_token("tok", expiry=TTL, plan=PRO)
        self.assertEqual(self.store.tier(), (None, "pro"))
        self.assertNotIn("rateLimitTier", self.store.load())
        self.assertTrue(self.store.declares_plan())

    def test_expiry_computed_from_the_entry(self) -> None:
        self.store.write_token("tok", expiry=TTL, plan=MAX_20X, today=date.today())
        expiry = self.store.expiry()
        assert expiry is not None
        self.assertEqual(expiry.created, date.today())
        self.assertEqual(expiry.remaining_days, TOKEN_TTL_DAYS)

    def test_expiry_unknown_reports_none_remaining(self) -> None:
        self.store.write_token("tok", expiry=UNKNOWN, plan=MAX_20X)
        expiry = self.store.expiry()
        assert expiry is not None
        self.assertIsNone(expiry.remaining_days)


class PlanEnvTests(ProfileDataStoreTestCase):
    """Plan-tier fields resolve to Claude Code env vars, validated."""

    def test_declared_tier_maps_to_env(self) -> None:
        self.store.write_token("tok", expiry=TTL, plan=MAX_20X)
        self.assertEqual(
            self.store.plan_env(),
            {
                "CLAUDE_CODE_SUBSCRIPTION_TYPE": "max",
                "CLAUDE_CODE_RATE_LIMIT_TIER": "default_claude_max_20x",
            },
        )

    def test_unrecognized_value_is_a_hard_error_naming_the_file(self) -> None:
        """A hand-edited entry is validated on read, not trusted."""
        self.store.data_dir.mkdir(parents=True, exist_ok=True)
        self.store.token_file.write_text(
            json.dumps({"token": "tok", "rateLimitTier": "turbo"})
        )
        with self.assertRaises(ValueError) as cm:
            self.store.plan_env()
        self.assertIn(str(self.store.token_file), str(cm.exception))
        self.assertIn("rateLimitTier", str(cm.exception))


class SetPlanTests(ProfileDataStoreTestCase):
    """set_plan merges the plan into the entry without disturbing the token."""

    def test_merges_into_an_existing_entry(self) -> None:
        self.store.write_token("tok", expiry=TTL, plan=MAX_20X)
        self.store.set_plan(plan_by_key("max-5x"))
        entry = self.store.load()
        self.assertEqual(entry["token"], "tok")
        self.assertEqual(entry["rateLimitTier"], "default_claude_max_5x")
        self.assertEqual(entry["subscriptionType"], "max")

    def test_a_plan_without_a_rate_limit_tier_leaves_the_old_one_behind(self) -> None:
        """Declaring Pro over Max 20x drops the field Pro does not carry."""
        self.store.write_token("tok", expiry=TTL, plan=MAX_20X)
        self.store.set_plan(PRO)
        entry = self.store.load()
        self.assertEqual(entry["subscriptionType"], "pro")
        self.assertEqual(
            self.store.plan_env(), {"CLAUDE_CODE_SUBSCRIPTION_TYPE": "pro"}
        )

    def test_creates_a_plan_only_entry(self) -> None:
        self.store.set_plan(PRO)
        self.assertEqual(self.store.load(), {"subscriptionType": "pro"})
        self.assertIsNone(self.store.token())
        self.assertEqual(self.mode_of(self.store.token_file), TOKEN_FILE_MODE)
        self.assertEqual(self.mode_of(self.store.data_dir), PROFILE_DATA_DIR_MODE)


class RemoveTests(ProfileDataStoreTestCase):
    """remove_token reports whether there was anything to remove."""

    def test_removes_an_existing_entry(self) -> None:
        self.store.write_token("tok", expiry=TTL, plan=MAX_20X)
        self.assertTrue(self.store.remove_token())
        self.assertFalse(self.store.token_file.exists())
        self.assertIsNone(self.store.token())

    def test_absent_entry_returns_false(self) -> None:
        self.assertFalse(self.store.remove_token())


class CorruptEntryTests(ProfileDataStoreTestCase):
    """A corrupt entry is a hard error, never a silent empty read."""

    def _write_raw(self, text: str) -> None:
        self.store.data_dir.mkdir(parents=True, exist_ok=True)
        self.store.token_file.write_text(text)

    def test_unparseable_json_raises(self) -> None:
        self._write_raw("{not json")
        with self.assertRaises(TokenStoreError) as cm:
            self.store.load()
        self.assertIn(str(self.store.token_file), str(cm.exception))

    def test_non_object_top_level_raises(self) -> None:
        self._write_raw('["tok"]')
        with self.assertRaises(TokenStoreError):
            self.store.load()

    def test_set_plan_refuses_to_clobber_a_corrupt_entry(self) -> None:
        self._write_raw("{not json")
        with self.assertRaises(TokenStoreError):
            self.store.set_plan(MAX_20X)
        self.assertEqual(self.store.token_file.read_text(), "{not json")


class IsolationTests(ProfileDataStoreTestCase):
    """Two profiles' stores never see each other's data."""

    def test_per_profile_files_are_independent(self) -> None:
        other_dir = self.profile_dir.parent / "hn"
        other_dir.mkdir()
        other = ProfileDataStore(other_dir)
        self.store.write_token("tok-work", expiry=TTL, plan=MAX_20X)
        other.write_token("tok-hn", expiry=TTL, plan=MAX_20X)
        self.assertEqual(self.store.token(), "tok-work")
        self.assertEqual(other.token(), "tok-hn")
        other.remove_token()
        self.assertEqual(self.store.token(), "tok-work")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
