package widgets

import (
	"encoding/json"
	"os"
	"slices"
	"testing"
)

// The fuzzy matching calls the Python's tests made, with their results
// (scripts/python-expectations tui-calls).
func TestFuzzyMatchesThePython(t *testing.T) {
	data, err := os.ReadFile("../testdata/python-tui-calls.json")
	if err != nil {
		t.Fatal(err)
	}
	var calls []struct {
		Fn     string            `json:"fn"`
		Args   []json.RawMessage `json:"args"`
		Result json.RawMessage   `json:"result"`
	}
	if err := json.Unmarshal(data, &calls); err != nil {
		t.Fatal(err)
	}
	checked := 0
	for i, c := range calls {
		var query string
		switch c.Fn {
		case "fuzzy_score":
			var candidate string
			_ = json.Unmarshal(c.Args[0], &query)
			_ = json.Unmarshal(c.Args[1], &candidate)
			var want []json.RawMessage
			_ = json.Unmarshal(c.Result, &want)
			var score int
			var positions []int
			_ = json.Unmarshal(want[0], &score)
			_ = json.Unmarshal(want[1], &positions)
			gotScore, gotPositions := FuzzyScore(query, candidate)
			if gotScore != score || !slices.Equal(orEmpty(gotPositions), orEmpty(positions)) {
				t.Errorf("call %d FuzzyScore(%q, %q) = %d %v, Python %d %v", i, query, candidate, gotScore, gotPositions, score, positions)
			}
		case "fuzzy_rank":
			var candidates, want []string
			_ = json.Unmarshal(c.Args[0], &query)
			_ = json.Unmarshal(c.Args[1], &candidates)
			_ = json.Unmarshal(c.Result, &want)
			if got := FuzzyRank(query, candidates); !slices.Equal(orEmptyS(got), orEmptyS(want)) {
				t.Errorf("call %d FuzzyRank(%q, %q) = %q, Python %q", i, query, candidates, got, want)
			}
		case "fuzzy_match_positions":
			var candidate string
			var want []int
			_ = json.Unmarshal(c.Args[0], &query)
			_ = json.Unmarshal(c.Args[1], &candidate)
			_ = json.Unmarshal(c.Result, &want)
			if got := FuzzyMatchPositions(query, candidate); !slices.Equal(orEmpty(got), orEmpty(want)) {
				t.Errorf("call %d FuzzyMatchPositions(%q, %q) = %v, Python %v", i, query, candidate, got, want)
			}
		default:
			continue
		}
		checked++
	}
	if checked == 0 {
		t.Fatal("no fuzzy call was checked")
	}
}

func orEmpty(s []int) []int {
	if s == nil {
		return []int{}
	}
	return s
}

func orEmptyS(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}
