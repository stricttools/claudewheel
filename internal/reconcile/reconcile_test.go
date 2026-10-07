package reconcile

import (
	"encoding/json"
	"os"
	"slices"
	"testing"

	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// The Python's reconcile of the same settings trees, written by
// scripts/python-expectations reconcile-cases.
type pythonCases struct {
	ScriptsDir string `json:"scripts_dir"`
	Cases      []struct {
		Target  string   `json:"target"`
		Input   string   `json:"input"`
		Output  string   `json:"output"`
		Changes []string `json:"changes"`
	} `json:"cases"`
}

func TestReconcileMatchesThePython(t *testing.T) {
	data, err := os.ReadFile("testdata/python-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var py pythonCases
	if err := json.Unmarshal(data, &py); err != nil {
		t.Fatal(err)
	}
	hooksValue, _ := guardrail.CanonicalSharedSettings(py.ScriptsDir).Get("hooks")
	hooks := hooksValue.(*jsonfile.Object)
	for i, c := range py.Cases {
		tree, err := jsonfile.DecodeObject([]byte(c.Input))
		if err != nil {
			t.Fatal(err)
		}
		var changes []string
		if c.Target == "profile" {
			changes, err = ReconcileProfileSettings(tree, hooks)
		} else {
			changes, err = ReconcileSharedSettings(tree, hooks)
		}
		if err != nil {
			t.Fatalf("case %d: %v", i, err)
		}
		if !slices.Equal(changes, c.Changes) {
			t.Errorf("case %d (%s) changes:\n got: %q\nwant: %q", i, c.Target, changes, c.Changes)
		}
		out, err := jsonfile.MarshalIndented(tree)
		if err != nil {
			t.Fatal(err)
		}
		if string(out) != c.Output {
			t.Errorf("case %d (%s) result:\n got: %s\nwant: %s", i, c.Target, out, c.Output)
		}
		again, err := jsonfile.DecodeObject(out)
		if err != nil {
			t.Fatal(err)
		}
		var second []string
		if c.Target == "profile" {
			second, err = ReconcileProfileSettings(again, hooks)
		} else {
			second, err = ReconcileSharedSettings(again, hooks)
		}
		if err != nil || len(second) != 0 {
			t.Errorf("case %d: a canonical tree was changed again: %q %v", i, second, err)
		}
	}
}

func decodeObject(text string) (*jsonfile.Object, error) { return jsonfile.DecodeObject([]byte(text)) }
