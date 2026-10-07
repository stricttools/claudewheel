package widgets

import (
	"slices"
	"sort"
	"unicode"
)

// Fuzzy match scores, best first.
const (
	ScoreExact       = 100
	ScorePrefix      = 80
	ScoreContains    = 60
	ScoreSubsequence = 40
	ScoreNoMatch     = 0
)

// FuzzyScore scores candidate against query, case-insensitively: exact,
// prefix, contains, or subsequence match, or no match. It also returns the
// character (rune) indices of candidate that matched, for highlighting;
// none for no match.
func FuzzyScore(query, candidate string) (int, []int) {
	q := lowerRunes(query)
	c := lowerRunes(candidate)
	if slices.Equal(q, c) {
		return ScoreExact, indexRange(0, len(c))
	}
	if len(q) <= len(c) && slices.Equal(c[:len(q)], q) {
		return ScorePrefix, indexRange(0, len(q))
	}
	if start := indexRunes(c, q); start >= 0 {
		return ScoreContains, indexRange(start, start+len(q))
	}
	qi := 0
	var positions []int
	for i, ch := range c {
		if qi < len(q) && ch == q[qi] {
			positions = append(positions, i)
			qi++
		}
	}
	if qi == len(q) {
		return ScoreSubsequence, positions
	}
	return ScoreNoMatch, nil
}

// FuzzyRank returns the candidates that match query, best score first;
// candidates with equal scores keep their order.
func FuzzyRank(query string, candidates []string) []string {
	type scored struct {
		score     int
		candidate string
	}
	var matches []scored
	for _, c := range candidates {
		if s, _ := FuzzyScore(query, c); s > ScoreNoMatch {
			matches = append(matches, scored{s, c})
		}
	}
	sort.SliceStable(matches, func(i, j int) bool { return matches[i].score > matches[j].score })
	ranked := make([]string, len(matches))
	for i, m := range matches {
		ranked[i] = m.candidate
	}
	return ranked
}

// FuzzyMatchPositions returns the rune indices of candidate matched by
// query, or none when it does not match.
func FuzzyMatchPositions(query, candidate string) []int {
	_, positions := FuzzyScore(query, candidate)
	return positions
}

// lowerRunes lowercases rune by rune, so indices into the result are indices
// into the original's runes.
func lowerRunes(s string) []rune {
	r := []rune(s)
	for i, ch := range r {
		r[i] = unicode.ToLower(ch)
	}
	return r
}

// indexRunes returns the first index of needle in haystack, or -1.
func indexRunes(haystack, needle []rune) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if slices.Equal(haystack[i:i+len(needle)], needle) {
			return i
		}
	}
	return -1
}

func indexRange(start, end int) []int {
	out := make([]int, 0, end-start)
	for i := start; i < end; i++ {
		out = append(out, i)
	}
	return out
}
