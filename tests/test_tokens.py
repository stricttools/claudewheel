"""Tests for the token entry format: parsing and expiry computation."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from claudewheel.tokens import (
    EXPIRY_UNKNOWN_FIELD,
    PLAN_TIERS,
    RATE_LIMIT_FIELD,
    RATE_LIMIT_TIERS,
    SUBSCRIPTION_FIELD,
    SUBSCRIPTION_TYPES,
    TOKEN_TTL_DAYS,
    PlanTier,
    TokenExpiryDisposition,
    apply_plan,
    build_entry,
    entry_declares_plan,
    entry_expiry,
    parse_entry,
    plan_by_key,
    plan_keys,
)


# ---------------------------------------------------------------------------
# parse_entry
# ---------------------------------------------------------------------------


class ParseEntryTests(unittest.TestCase):
    """Tests for parse_entry() covering the entry shape and garbage."""

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

    def test_bare_string_is_not_an_entry(self) -> None:
        self.assertIsNone(parse_entry("tok-abc"))

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


class EntryExpiryTests(unittest.TestCase):
    """Tests for entry_expiry() across every entry shape."""

    TODAY = date(2026, 7, 1)

    def test_expires_at_takes_precedence(self) -> None:
        """Explicit expires_at wins even when created is also present."""
        entry = {"token": "t", "created": "2026-01-01", "expires_at": "2026-12-31"}
        result = entry_expiry(entry, today=self.TODAY)
        self.assertEqual(result.created, date(2026, 1, 1))
        self.assertEqual(result.expires, date(2026, 12, 31))
        self.assertEqual(result.remaining_days, (date(2026, 12, 31) - self.TODAY).days)

    def test_expires_at_without_created(self) -> None:
        entry = {"token": "t", "expires_at": "2026-08-01"}
        result = entry_expiry(entry, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertEqual(result.expires, date(2026, 8, 1))
        self.assertEqual(result.remaining_days, 31)

    def test_created_only(self) -> None:
        entry = {"token": "t", "created": "2026-01-01"}
        result = entry_expiry(entry, today=self.TODAY)
        self.assertEqual(result.created, date(2026, 1, 1))
        self.assertEqual(
            result.expires, date(2026, 1, 1) + timedelta(days=TOKEN_TTL_DAYS)
        )
        elapsed = (self.TODAY - date(2026, 1, 1)).days
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS - elapsed)

    def test_invalid_expires_at_assumes_fresh(self) -> None:
        """Unparseable expires_at yields (None, None, TTL) -- historical behavior."""
        entry = {"token": "t", "created": "2026-01-01", "expires_at": "not-a-date"}
        result = entry_expiry(entry, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertIsNone(result.expires)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS)

    def test_invalid_created_assumes_fresh(self) -> None:
        entry = {"token": "t", "created": "garbage"}
        result = entry_expiry(entry, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertIsNone(result.expires)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS)

    def test_dict_without_dates_assumes_fresh(self) -> None:
        result = entry_expiry({"token": "t"}, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertIsNone(result.expires)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS)

    def test_expiry_unknown_marker_yields_all_none(self) -> None:
        """An entry marked expiry_unknown reports (None, None, None) --
        distinct from the 'assume fresh' branch which returns a concrete TTL."""
        entry = {"token": "t", "created": "2026-01-01", EXPIRY_UNKNOWN_FIELD: True}
        result = entry_expiry(entry, today=self.TODAY)
        self.assertIsNone(result.created)
        self.assertIsNone(result.expires)
        self.assertIsNone(result.remaining_days)

    def test_expiry_unknown_takes_precedence_over_dates(self) -> None:
        """The unknown marker wins even if expires_at/created are also present."""
        entry = {
            "token": "t",
            "created": "2026-01-01",
            "expires_at": "2026-12-31",
            EXPIRY_UNKNOWN_FIELD: True,
        }
        result = entry_expiry(entry, today=self.TODAY)
        self.assertIsNone(result.remaining_days)

    def test_default_today_is_today(self) -> None:
        """Omitting today uses date.today()."""
        created = date.today() - timedelta(days=100)
        entry = {"token": "t", "created": created.isoformat()}
        result = entry_expiry(entry)
        self.assertEqual(result.remaining_days, TOKEN_TTL_DAYS - 100)


# ---------------------------------------------------------------------------
# The plan tier
# ---------------------------------------------------------------------------


class PlanTierValidationTests(unittest.TestCase):
    """Both fields are closed enums, checked when the plan is constructed."""

    def test_an_unrecognized_subscription_type_is_a_hard_error(self) -> None:
        with self.assertRaises(ValueError) as cm:
            PlanTier("x", "X", "platinum")
        message = str(cm.exception)
        self.assertIn(SUBSCRIPTION_FIELD, message)
        self.assertIn("platinum", message)
        for value in SUBSCRIPTION_TYPES:
            self.assertIn(value, message)

    def test_an_unrecognized_rate_limit_tier_is_a_hard_error(self) -> None:
        with self.assertRaises(ValueError) as cm:
            PlanTier("x", "X", "max", "turbo")
        message = str(cm.exception)
        self.assertIn(RATE_LIMIT_FIELD, message)
        self.assertIn("turbo", message)
        for value in RATE_LIMIT_TIERS:
            self.assertIn(value, message)

    def test_no_rate_limit_tier_is_legal(self) -> None:
        """Pro and Enterprise have no rate-limit string in the client."""
        plan = PlanTier("x", "X", "pro")
        self.assertIsNone(plan.rate_limit_tier)
        self.assertEqual(plan.fields(), {SUBSCRIPTION_FIELD: "pro"})


class PlanCatalogTests(unittest.TestCase):
    """The closed list every writer resolves against."""

    def test_keys_are_unique_and_ordered(self) -> None:
        keys = plan_keys()
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, [plan.key for plan in PLAN_TIERS])

    def test_every_declared_plan_is_valid(self) -> None:
        for plan in PLAN_TIERS:
            self.assertIn(plan.subscription_type, SUBSCRIPTION_TYPES)
            if plan.rate_limit_tier is not None:
                self.assertIn(plan.rate_limit_tier, RATE_LIMIT_TIERS)

    def test_lookup_by_key(self) -> None:
        self.assertEqual(
            plan_by_key("max-20x").rate_limit_tier, "default_claude_max_20x"
        )
        self.assertEqual(plan_by_key("team").subscription_type, "team")

    def test_unknown_key_is_a_hard_error_naming_the_valid_ones(self) -> None:
        with self.assertRaises(ValueError) as cm:
            plan_by_key("max")
        message = str(cm.exception)
        for key in plan_keys():
            self.assertIn(key, message)


class ApplyPlanTests(unittest.TestCase):
    """Writing a plan into an entry replaces BOTH fields."""

    def test_sets_both_fields(self) -> None:
        entry: dict[str, object] = {"token": "t"}
        apply_plan(entry, plan_by_key("max-5x"))
        self.assertEqual(entry[SUBSCRIPTION_FIELD], "max")
        self.assertEqual(entry[RATE_LIMIT_FIELD], "default_claude_max_5x")

    def test_clears_a_rate_limit_tier_the_new_plan_does_not_carry(self) -> None:
        entry: dict[str, object] = {"token": "t"}
        apply_plan(entry, plan_by_key("max-20x"))
        apply_plan(entry, plan_by_key("pro"))
        self.assertEqual(entry[SUBSCRIPTION_FIELD], "pro")
        self.assertNotIn(RATE_LIMIT_FIELD, entry)


class EntryDeclaresPlanTests(unittest.TestCase):
    """A declaration is keyed on the subscription type."""

    def test_a_built_entry_declares_one(self) -> None:
        entry = build_entry(
            "t", expiry=TokenExpiryDisposition.TTL, plan=plan_by_key("pro")
        )
        self.assertTrue(entry_declares_plan(entry))

    def test_an_empty_entry_declares_none(self) -> None:
        self.assertFalse(entry_declares_plan({}))
        self.assertFalse(entry_declares_plan({"token": "t"}))

    def test_a_rate_limit_tier_alone_declares_none(self) -> None:
        self.assertFalse(
            entry_declares_plan({"token": "t", RATE_LIMIT_FIELD: "default_claude_zero"})
        )


if __name__ == "__main__":
    unittest.main()
