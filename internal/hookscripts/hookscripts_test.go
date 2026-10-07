package hookscripts

import (
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/stricttools/claudewheel/internal/guardrail"
)

// pythonDir holds every script the Python claudewheel generates, written by
// scripts/python-hook-scripts. The deployed scripts must stay byte-identical
// to them, so a Go deploy changes no deployed hook.
const pythonDir = "testdata/python"

func TestScriptsMatchThePythonGeneratedScripts(t *testing.T) {
	entries, err := os.ReadDir(pythonDir)
	if err != nil {
		t.Fatal(err)
	}
	var want []string
	for _, e := range entries {
		want = append(want, e.Name())
	}
	sort.Strings(want)
	got := Names()
	if len(got) != len(want) {
		t.Fatalf("Names() = %v, the Python generates %v", got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("Names() = %v, the Python generates %v", got, want)
		}
	}
	for _, name := range want {
		expected, err := os.ReadFile(filepath.Join(pythonDir, name))
		if err != nil {
			t.Fatal(err)
		}
		text, err := Script(name)
		if err != nil {
			t.Fatalf("Script(%q): %v", name, err)
		}
		if text != string(expected) {
			t.Errorf("Script(%q) differs from the Python's at byte %d", name, firstDifference(text, string(expected)))
		}
	}
}

func TestScriptRefusesAnUnknownName(t *testing.T) {
	if _, err := Script("hook-nonexistent"); err == nil {
		t.Fatal("Script accepted an unknown name")
	}
}

func firstDifference(a, b string) int {
	n := min(len(a), len(b))
	for i := 0; i < n; i++ {
		if a[i] != b[i] {
			return i
		}
	}
	return n
}

func TestEveryWiredScriptIsDeployed(t *testing.T) {
	for _, w := range guardrail.ExpectedHookWirings() {
		if !IsScript(w.Script) {
			t.Errorf("wiring %s/%s names %s, which is not a deployed script", w.Event, w.Matcher, w.Script)
		}
	}
}

func TestExit2HooksAreDeployedScripts(t *testing.T) {
	for name := range Exit2Hooks() {
		if !IsScript(name) {
			t.Errorf("%s is not a deployed script", name)
		}
	}
}
