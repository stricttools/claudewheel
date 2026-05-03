"""Smoke tests for fuzzy matching in claudewheel.fuzzy."""

from __future__ import annotations

import unittest

from claudewheel.fuzzy import (
    fuzzy_match_positions,
    fuzzy_rank,
    fuzzy_score,
)


class FuzzyScoreTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        """Exact equality scores 100 and marks every position as matched."""
        score, positions = fuzzy_score("abc", "abc")
        self.assertEqual(score, 100)
        self.assertEqual(positions, [0, 1, 2])

    def test_prefix_match(self) -> None:
        """A query that prefixes the candidate scores 80 with prefix positions."""
        score, positions = fuzzy_score("ab", "abcdef")
        self.assertEqual(score, 80)
        self.assertEqual(positions, [0, 1])

    def test_contains_match(self) -> None:
        """A query found contiguously inside the candidate scores 60."""
        score, positions = fuzzy_score("cd", "abcdef")
        self.assertEqual(score, 60)
        self.assertEqual(positions, [2, 3])

    def test_subsequence_match(self) -> None:
        """Non-contiguous in-order matches score 40 with their walk positions."""
        score, positions = fuzzy_score("ace", "abcdef")
        self.assertEqual(score, 40)
        self.assertEqual(positions, [0, 2, 4])

    def test_no_match(self) -> None:
        """Queries that cannot be matched at all score 0 with no positions."""
        score, positions = fuzzy_score("xyz", "abc")
        self.assertEqual(score, 0)
        self.assertEqual(positions, [])

    def test_case_insensitive(self) -> None:
        """Matching ignores case on both sides of the comparison."""
        cases = [
            ("AB", "abcdef", 80),
            ("ab", "ABCDEF", 80),
            ("CD", "abcdef", 60),
            ("ACE", "abcdef", 40),
            ("ABC", "abc", 100),
        ]
        for query, candidate, expected_score in cases:
            with self.subTest(query=query, candidate=candidate):
                score, _ = fuzzy_score(query, candidate)
                self.assertEqual(score, expected_score)

    def test_empty_query_against_nonempty(self) -> None:
        """Empty query is treated as a (degenerate) prefix match: score 80, no positions."""
        score, positions = fuzzy_score("", "abc")
        # "abc".startswith("") is True, so we take the prefix branch
        # range(len("")) is empty, so positions is []
        self.assertEqual(score, 80)
        self.assertEqual(positions, [])

    def test_empty_query_against_empty(self) -> None:
        """Empty query against empty candidate is treated as exact match."""
        score, positions = fuzzy_score("", "")
        self.assertEqual(score, 100)
        self.assertEqual(positions, [])


class FuzzyMatchPositionsTests(unittest.TestCase):
    def test_returns_only_positions(self) -> None:
        """fuzzy_match_positions is a thin wrapper that drops the score."""
        self.assertEqual(fuzzy_match_positions("ab", "abcdef"), [0, 1])
        self.assertEqual(fuzzy_match_positions("cd", "abcdef"), [2, 3])
        self.assertEqual(fuzzy_match_positions("ace", "abcdef"), [0, 2, 4])
        self.assertEqual(fuzzy_match_positions("xyz", "abc"), [])


class FuzzyRankTests(unittest.TestCase):
    def test_excludes_non_matches(self) -> None:
        """Candidates that score 0 are dropped entirely."""
        result = fuzzy_rank("ab", ["abc", "xyz", "abz"])
        self.assertNotIn("xyz", result)
        # both abc (prefix=80) and abz (prefix=80) survive
        self.assertEqual(set(result), {"abc", "abz"})

    def test_sorted_by_score_descending(self) -> None:
        """Higher-scoring matches come first."""
        # "abc" -> exact (100)
        # "abcdef" -> prefix (80)
        # "xabc" -> contains (60)
        # "axbxc" -> subsequence (40)
        # "zzz" -> no match (0)
        result = fuzzy_rank("abc", ["axbxc", "xabc", "abc", "abcdef", "zzz"])
        self.assertEqual(result, ["abc", "abcdef", "xabc", "axbxc"])

    def test_ties_preserve_relative_order(self) -> None:
        """When two candidates tie, Python's stable sort preserves input order."""
        # All three are prefix matches for "ab" (score 80), so order from input
        # is preserved by the stable sort.
        result = fuzzy_rank("ab", ["abc", "abd", "abe"])
        self.assertEqual(result, ["abc", "abd", "abe"])

    def test_empty_candidates(self) -> None:
        """No candidates yields no results."""
        self.assertEqual(fuzzy_rank("abc", []), [])


if __name__ == "__main__":
    unittest.main()
