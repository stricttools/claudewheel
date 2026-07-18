"""Tests for token entry parsing, expiry computation, and the TokenStore facade."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from claudewheel.tokens import (
    TOKEN_TTL_DAYS,
    TokenStore,
    TokenStoreError,
    compute_expiry,
    parse_entry,
)


# ---------------------------------------------------------------------------
# parse_entry
# ---------------------------------------------------------------------------


class ParseEntryTests(unittest.TestCase):
    """Tests for parse_entry() covering both formats and garbage."""

    def test_bare_string(self) -> None:
        self.assertEqual(parse_entry("tok-abc"), "tok-abc")

    def test_dict_with_token(self) -> None:
        entry = {"token": "tok-dict", "created": "2025-01-01"}
        self.assertEqual(parse_entry(entry), "tok-dict")

    def test_dict_with_all_fields(self) -> None:
        entry = {
            "token": "tok-full",
            "created": "2025-01-01",
            "expires_at": "2026-01-01",
        }
        self.assertEqual(parse_entry(entry), "tok-full")

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(parse_entry(""))

    def test_none_returns_none(self) -> None:
        self.assertIsNone(parse_entry(None))

    def test_dict_without_token_returns_none(self) -> None:
        self.assertIsNone(parse_entry({"created": "2025-01-01"}))

    def test_dict_with_empty_token_returns_none(self) -> None:
        self.assertIsNone(parse_entry({"token": ""}))

    def test_garbage_types_return_none(self) -> None:
        self.assertIsNone(parse_entry(42))
        self.assertIsNone(parse_entry(["tok-in-list"]))
        self.assertIsNone(parse_entry(3.14))
        self.assertIsNone(parse_entry(True))


# ---------------------------------------------------------------------------
# compute_expiry
# ---------------------------------------------------------------------------


class ComputeExpiryTests(unittest.TestCase):
    """Tests for compute_expiry() across all entry formats."""

    TODAY = date(2026, 7, 1)
    MTIME = 1_700_000_000.0  # arbitrary; unused for dict entries

    def test_expires_at_takes_precedence(self) -> None:
        """Explicit expires_at wins even when created is also present."""
        entry = {"token": "t", "created": "2026-01-01", "expires_at": "2026-12-31"}
        result = compute_expiry(entry, self.MTIME, today=self.TODAY)
        self.assertEqual(result.created, date(2026, 1, 1))
        self.assertEqual(result.expires, date(2026, 12, 31))
        self.assertEqual(result.remaining_days, (date(2026, 12, 31) - self.TODAY).days)

    def test_expires_at_without_created(self) -> None:
        entry = {"token": "t", "expires_at": "2026-08-01"}
        result = compute_expiry(entry, self.MTIME, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertEqual(result.expires, date(2026, 8, 1))
        self.assertEqual(result.remaining_days, 31)

    def test_created_only(self) -> None:
        entry = {"token": "t", "created": "2026-01-01"}
        result = compute_expiry(entry, self.MTIME, today=self.TODAY)
        self.assertEqual(result.created, date(2026, 1, 1))
        self.assertEqual(
            result.expires, date(2026, 1, 1) + timedelta(days=TOKEN_TTL_DAYS)
        )
        elapsed = (self.TODAY - date(2026, 1, 1)).days
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS - elapsed)

    def test_invalid_expires_at_assumes_fresh(self) -> None:
        """Unparseable expires_at yields (None, None, TTL) -- historical behavior."""
        entry = {"token": "t", "created": "2026-01-01", "expires_at": "not-a-date"}
        result = compute_expiry(entry, self.MTIME, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertIsNone(result.expires)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS)

    def test_invalid_created_assumes_fresh(self) -> None:
        entry = {"token": "t", "created": "garbage"}
        result = compute_expiry(entry, self.MTIME, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertIsNone(result.expires)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS)

    def test_dict_without_dates_assumes_fresh(self) -> None:
        result = compute_expiry({"token": "t"}, self.MTIME, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertIsNone(result.expires)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS)

    def test_legacy_bare_string_uses_mtime(self) -> None:
        """Bare-string entries date from the tokens.json file mtime."""
        ten_days = 10 * 86400
        mtime = time.time() - ten_days
        result = compute_expiry("tok-legacy", mtime)
        expected_created = date.fromtimestamp(mtime)
        self.assertEqual(result.created, expected_created)
        self.assertEqual(
            result.expires, expected_created + timedelta(days=TOKEN_TTL_DAYS)
        )
        self.assertAlmostEqual(result.remaining_days, TOKEN_TTL_DAYS - 10, delta=0.1)

    def test_default_today_is_today(self) -> None:
        """Omitting today uses date.today()."""
        created = date.today() - timedelta(days=100)
        entry = {"token": "t", "created": created.isoformat()}
        result = compute_expiry(entry, self.MTIME)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS - 100)


# ---------------------------------------------------------------------------
# TokenStore.add (ported from the former module-level add_token tests)
# ---------------------------------------------------------------------------


class TokenStoreAddTests(unittest.TestCase):
    """Tests for TokenStore.add()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.launcher_dir = Path(self._tmp.name) / ".claudewheel"
        self.launcher_dir.mkdir()
        self.tokens_file = self.launcher_dir / "tokens.json"
        self.store = TokenStore(self.tokens_file)

    def _write_tokens(self, tokens: dict) -> None:
        self.tokens_file.write_text(json.dumps(tokens, indent=2) + "\n")

    def test_creates_fresh_file(self) -> None:
        """When tokens.json doesn't exist, creates it with the entry."""
        self.assertFalse(self.tokens_file.exists())
        self.store.add("newprof", "tok-123")

        tokens = json.loads(self.tokens_file.read_text())
        self.assertIn("newprof", tokens)
        self.assertEqual(tokens["newprof"]["token"], "tok-123")
        self.assertIn("created", tokens["newprof"])

    def test_writes_all_three_fields(self) -> None:
        """Entry has token, created (today), and expires_at (created + TTL)."""
        self.store.add("prof", "tok-xyz")

        entry = json.loads(self.tokens_file.read_text())["prof"]
        self.assertEqual(entry["token"], "tok-xyz")
        created = date.fromisoformat(entry["created"])
        expires = date.fromisoformat(entry["expires_at"])
        self.assertEqual(created, date.today())
        self.assertEqual(expires, created + timedelta(days=TOKEN_TTL_DAYS))

    def test_adds_to_existing_file(self) -> None:
        """When tokens.json exists, adds the entry without clobbering others."""
        self._write_tokens({"existing": {"token": "tok-old", "created": "2025-01-01"}})
        self.store.add("newprof", "tok-456")

        tokens = json.loads(self.tokens_file.read_text())
        self.assertIn("existing", tokens)
        self.assertIn("newprof", tokens)
        self.assertEqual(tokens["newprof"]["token"], "tok-456")

    def test_updates_existing_token(self) -> None:
        """Overwrites a profile's token entry when it already exists."""
        self._write_tokens({"myprof": {"token": "old-tok", "created": "2024-01-01"}})
        self.store.add("myprof", "new-tok")

        tokens = json.loads(self.tokens_file.read_text())
        self.assertEqual(tokens["myprof"]["token"], "new-tok")
        # Created date should be updated too
        self.assertNotEqual(tokens["myprof"]["created"], "2024-01-01")

    def test_file_permissions_on_fresh_creation(self) -> None:
        """Fresh file gets 0600 permissions."""
        self.assertFalse(self.tokens_file.exists())
        self.store.add("secured", "tok-sec")

        mode = self.tokens_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_update_preserves_0600_permissions(self) -> None:
        """Updating an existing 0600 tokens.json must not loosen it to umask
        default -- the atomic tmp-swap replaces the target inode, so the tmp
        file's perms win. Regression test for the tmp-swap perms bug."""
        old_umask = os.umask(0o022)  # pin umask so the tmp file defaults 0644
        self.addCleanup(os.umask, old_umask)
        self._write_tokens({"myprof": {"token": "old-tok", "created": "2024-01-01"}})
        self.tokens_file.chmod(0o600)

        self.store.add("myprof", "new-tok")

        mode = self.tokens_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_raises_oserror_and_is_not_overwritten(self) -> None:
        """A corrupt tokens.json is a hard OSError (callers catch OSError),
        and the corrupt content is left untouched -- never silently clobbered."""
        self.tokens_file.write_text("{not json")

        with self.assertRaises(OSError) as ctx:
            self.store.add("prof", "tok-x")
        self.assertIn("corrupt", str(ctx.exception).lower())
        # Original corrupt content preserved for the user to inspect/recover.
        self.assertEqual(self.tokens_file.read_text(), "{not json")

    def test_atomic_write_leaves_no_tmp_file(self) -> None:
        """The tmp-file swap leaves no .tmp sibling and valid JSON behind."""
        self.store.add("prof", "tok-atomic")
        self.assertFalse(self.tokens_file.with_suffix(".tmp").exists())
        # File is valid, complete JSON
        tokens = json.loads(self.tokens_file.read_text())
        self.assertEqual(tokens["prof"]["token"], "tok-atomic")


class TokenStoreAddTierTests(unittest.TestCase):
    """Tests for TokenStore.add() optional tier/subscription params."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.launcher_dir = Path(self._tmp.name) / ".claudewheel"
        self.launcher_dir.mkdir()
        self.tokens_file = self.launcher_dir / "tokens.json"
        self.store = TokenStore(self.tokens_file)

    def test_tier_included_when_provided(self) -> None:
        self.store.add(
            "prof", "tok-1", tier="default_claude_pro", subscription="claude_pro"
        )
        entry = json.loads(self.tokens_file.read_text())["prof"]
        self.assertEqual(entry["rateLimitTier"], "default_claude_pro")
        self.assertEqual(entry["subscriptionType"], "claude_pro")
        self.assertEqual(entry["token"], "tok-1")

    def test_tier_omitted_when_none(self) -> None:
        self.store.add("prof", "tok-2")
        entry = json.loads(self.tokens_file.read_text())["prof"]
        self.assertNotIn("rateLimitTier", entry)
        self.assertNotIn("subscriptionType", entry)

    def test_tier_only_no_subscription(self) -> None:
        self.store.add("prof", "tok-3", tier="default_claude_max_20x")
        entry = json.loads(self.tokens_file.read_text())["prof"]
        self.assertEqual(entry["rateLimitTier"], "default_claude_max_20x")
        self.assertNotIn("subscriptionType", entry)


class TokenStoreSetTierTests(unittest.TestCase):
    """Tests for TokenStore.set_tier() -- tier-only storage in tokens.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.launcher_dir = Path(self._tmp.name) / ".claudewheel"
        self.launcher_dir.mkdir()
        self.tokens_file = self.launcher_dir / "tokens.json"
        self.store = TokenStore(self.tokens_file)

    def _write_tokens(self, tokens: dict) -> None:
        self.tokens_file.write_text(json.dumps(tokens, indent=2) + "\n")

    def test_creates_tier_only_entry(self) -> None:
        """When no prior entry exists, creates a tier-only entry (no token)."""
        self.store.set_tier(
            "newprof", tier="default_claude_pro", subscription="claude_pro"
        )
        tokens = json.loads(self.tokens_file.read_text())
        entry = tokens["newprof"]
        self.assertEqual(entry["rateLimitTier"], "default_claude_pro")
        self.assertEqual(entry["subscriptionType"], "claude_pro")
        self.assertIsNone(parse_entry(entry))  # no token field

    def test_merges_into_existing_token_entry(self) -> None:
        """Adds tier fields to an existing token entry without overwriting the token."""
        self._write_tokens(
            {
                "prof": {
                    "token": "tok-x",
                    "created": "2025-01-01",
                    "expires_at": "2026-01-01",
                }
            }
        )
        self.store.set_tier("prof", tier="default_claude_max_5x")
        tokens = json.loads(self.tokens_file.read_text())
        entry = tokens["prof"]
        self.assertEqual(entry["token"], "tok-x")
        self.assertEqual(entry["rateLimitTier"], "default_claude_max_5x")
        self.assertEqual(entry["created"], "2025-01-01")

    def test_upgrades_bare_string_entry(self) -> None:
        """A legacy bare-string entry is upgraded to a dict."""
        self._write_tokens({"prof": "tok-legacy"})
        self.store.set_tier("prof", tier="default_claude_pro")
        tokens = json.loads(self.tokens_file.read_text())
        entry = tokens["prof"]
        self.assertEqual(entry["token"], "tok-legacy")
        self.assertEqual(entry["rateLimitTier"], "default_claude_pro")

    def test_noop_when_both_none(self) -> None:
        """Does nothing when both tier and subscription are None."""
        self.store.set_tier("prof", tier=None, subscription=None)
        self.assertFalse(self.tokens_file.exists())

    def test_corrupt_file_raises_oserror(self) -> None:
        """A corrupt tokens.json raises OSError (write-path contract)."""
        self.tokens_file.write_text("{not json")
        with self.assertRaises(OSError):
            self.store.set_tier("prof", tier="x")

    def test_preserves_other_profiles(self) -> None:
        """Storing tier for one profile preserves other profiles' entries."""
        self._write_tokens({"other": {"token": "tok-other"}})
        self.store.set_tier("newprof", tier="default_claude_pro")
        tokens = json.loads(self.tokens_file.read_text())
        self.assertIn("other", tokens)
        self.assertEqual(tokens["other"]["token"], "tok-other")


# ---------------------------------------------------------------------------
# TokenStore
# ---------------------------------------------------------------------------


class TokenStoreTests(unittest.TestCase):
    """Tests for the path-injected TokenStore facade."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "tokens.json"
        self.store = TokenStore(self.path)

    def _write(self, tokens: dict | list | str) -> None:
        if isinstance(tokens, str):
            self.path.write_text(tokens)
        else:
            self.path.write_text(json.dumps(tokens, indent=2) + "\n")

    # -- load --------------------------------------------------------------

    def test_load_missing_file_returns_empty(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.load(), {})

    def test_load_corrupt_raises_with_path_in_message(self) -> None:
        self._write("{not json")
        with self.assertRaises(TokenStoreError) as ctx:
            self.store.load()
        self.assertIn(str(self.path), str(ctx.exception))
        self.assertIn("corrupt", str(ctx.exception).lower())

    def test_load_non_dict_top_level_raises(self) -> None:
        self._write(["a", "b"])
        with self.assertRaises(TokenStoreError) as ctx:
            self.store.load()
        self.assertIn(str(self.path), str(ctx.exception))

    def test_load_chains_original_exception(self) -> None:
        self._write("{not json")
        with self.assertRaises(TokenStoreError) as ctx:
            self.store.load()
        self.assertIsNotNone(ctx.exception.__cause__)

    # -- token_for ---------------------------------------------------------

    def test_token_for_missing_file_is_none(self) -> None:
        self.assertIsNone(self.store.token_for("nobody"))

    def test_token_for_missing_entry_is_none(self) -> None:
        self._write({"other": {"token": "tok-other"}})
        self.assertIsNone(self.store.token_for("nobody"))

    def test_token_for_tier_only_entry_is_none(self) -> None:
        self._write({"prof": {"rateLimitTier": "default_claude_pro"}})
        self.assertIsNone(self.store.token_for("prof"))

    def test_token_for_bare_string_entry(self) -> None:
        self._write({"prof": "tok-legacy"})
        self.assertEqual(self.store.token_for("prof"), "tok-legacy")

    def test_token_for_dict_entry(self) -> None:
        self._write({"prof": {"token": "tok-dict", "created": "2025-01-01"}})
        self.assertEqual(self.store.token_for("prof"), "tok-dict")

    def test_token_for_corrupt_raises(self) -> None:
        self._write("{not json")
        with self.assertRaises(TokenStoreError):
            self.store.token_for("prof")

    # -- names -------------------------------------------------------------

    def test_names_missing_file_empty(self) -> None:
        self.assertEqual(self.store.names(), set())

    def test_names_returns_keys(self) -> None:
        self._write({"a": {"token": "t1"}, "b": "t2"})
        self.assertEqual(self.store.names(), {"a", "b"})

    # -- expiry_for --------------------------------------------------------

    def test_expiry_for_missing_entry_is_none(self) -> None:
        self._write({"other": {"token": "t"}})
        self.assertIsNone(self.store.expiry_for("prof"))

    def test_expiry_for_missing_file_is_none(self) -> None:
        self.assertIsNone(self.store.expiry_for("prof"))

    def test_expiry_for_dict_entry(self) -> None:
        self._write(
            {
                "prof": {
                    "token": "t",
                    "created": "2026-01-01",
                    "expires_at": "2026-12-31",
                }
            }
        )
        result = self.store.expiry_for("prof")
        self.assertIsNotNone(result)
        self.assertEqual(result.expires, date(2026, 12, 31))

    def test_expiry_for_legacy_uses_file_mtime(self) -> None:
        self._write({"prof": "tok-legacy"})
        ten_days_ago = time.time() - 10 * 86400
        os.utime(self.path, (ten_days_ago, ten_days_ago))
        result = self.store.expiry_for("prof")
        self.assertIsNotNone(result)
        self.assertEqual(result.created, date.fromtimestamp(ten_days_ago))
        self.assertAlmostEqual(result.remaining_days, TOKEN_TTL_DAYS - 10, delta=0.5)

    # -- add / set_tier ----------------------------------------------------

    def test_add_round_trip(self) -> None:
        self.store.add("prof", "tok-add")
        self.assertEqual(self.store.token_for("prof"), "tok-add")

    def test_add_with_tier(self) -> None:
        self.store.add(
            "prof", "tok-x", tier="default_claude_pro", subscription="claude_pro"
        )
        entry = self.store.load()["prof"]
        self.assertEqual(entry["rateLimitTier"], "default_claude_pro")
        self.assertEqual(entry["subscriptionType"], "claude_pro")

    def test_set_tier_creates_tier_only_entry(self) -> None:
        self.store.set_tier("prof", tier="default_claude_max_5x")
        entry = self.store.load()["prof"]
        self.assertEqual(entry["rateLimitTier"], "default_claude_max_5x")
        self.assertIsNone(self.store.token_for("prof"))

    def test_set_tier_round_trip_noop_when_both_none(self) -> None:
        self.store.set_tier("prof", tier=None, subscription=None)
        self.assertFalse(self.path.exists())

    def test_add_leaves_file_0600(self) -> None:
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self.store.add("prof", "tok-sec")
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_set_tier_leaves_file_0600(self) -> None:
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self.store.set_tier("prof", tier="default_claude_pro")
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_add_corrupt_file_raises_oserror_not_tokenstoreerror(self) -> None:
        """Write path preserves the historical OSError contract."""
        self._write("{not json")
        with self.assertRaises(OSError) as ctx:
            self.store.add("prof", "tok-x")
        self.assertNotIsInstance(ctx.exception, TokenStoreError)
        self.assertEqual(self.path.read_text(), "{not json")

    # -- rename ------------------------------------------------------------

    def test_rename_present_returns_true(self) -> None:
        self._write({"old": {"token": "tok-old"}})
        self.assertTrue(self.store.rename("old", "new"))
        tokens = self.store.load()
        self.assertNotIn("old", tokens)
        self.assertEqual(tokens["new"]["token"], "tok-old")

    def test_rename_absent_returns_false(self) -> None:
        self._write({"other": {"token": "t"}})
        self.assertFalse(self.store.rename("old", "new"))

    def test_rename_missing_file_returns_false(self) -> None:
        self.assertFalse(self.store.rename("old", "new"))

    def test_rename_leaves_file_0600(self) -> None:
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self._write({"old": {"token": "tok-old"}})
        self.path.chmod(0o600)
        self.store.rename("old", "new")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    # -- remove ------------------------------------------------------------

    def test_remove_present_returns_true(self) -> None:
        self._write({"prof": {"token": "t"}, "keep": "k"})
        self.assertTrue(self.store.remove("prof"))
        tokens = self.store.load()
        self.assertNotIn("prof", tokens)
        self.assertIn("keep", tokens)

    def test_remove_absent_returns_false(self) -> None:
        self._write({"keep": "k"})
        self.assertFalse(self.store.remove("prof"))

    def test_remove_missing_file_returns_false(self) -> None:
        self.assertFalse(self.store.remove("prof"))

    def test_remove_leaves_file_0600(self) -> None:
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self._write({"prof": {"token": "t"}, "keep": "k"})
        self.path.chmod(0o600)
        self.store.remove("prof")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
