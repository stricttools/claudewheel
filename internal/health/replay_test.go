package health

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/testkit"
	"github.com/stricttools/claudewheel/internal/workspace"
)

type snapEntry struct {
	Path    string `json:"path"`
	Type    string `json:"type"`
	Content string `json:"content"`
	Target  string `json:"target"`
	Mode    uint32 `json:"mode"`
	MtimeNS int64  `json:"mtime_ns"`
}

// healthCall is one check the Python's health tests ran, the home directory
// it ran on, and its result (scripts/python-expectations health-calls).
type healthCall struct {
	Fn     string      `json:"fn"`
	Today  string      `json:"today"`
	Before []snapEntry `json:"before"`
	Label  string      `json:"label"`
	OK     bool        `json:"ok"`
	Detail string      `json:"detail"`
}

func materialize(t *testing.T, root string, entries []snapEntry) {
	t.Helper()
	var dirs []snapEntry
	for _, e := range entries {
		path := filepath.Join(root, e.Path)
		switch e.Type {
		case "dir":
			if err := os.MkdirAll(path, 0o755); err != nil {
				t.Fatal(err)
			}
			dirs = append(dirs, e)
		case "symlink":
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(strings.ReplaceAll(e.Target, "@ROOT@", root), path); err != nil {
				t.Fatal(err)
			}
		case "file":
			testkit.WriteFile(t, path, strings.ReplaceAll(e.Content, "@ROOT@", root))
			if err := os.Chmod(path, os.FileMode(e.Mode)); err != nil {
				t.Fatal(err)
			}
		}
	}
	sort.Slice(dirs, func(i, j int) bool { return len(dirs[i].Path) > len(dirs[j].Path) })
	for _, d := range dirs {
		if err := os.Chmod(filepath.Join(root, d.Path), os.FileMode(d.Mode)); err != nil {
			t.Fatal(err)
		}
	}
}

func TestChecksMatchThePython(t *testing.T) {
	data, err := os.ReadFile("testdata/python-calls.json")
	if err != nil {
		t.Fatal(err)
	}
	var calls []healthCall
	if err := json.Unmarshal(data, &calls); err != nil {
		t.Fatal(err)
	}
	for i, c := range calls {
		switch {
		case strings.Contains(c.Detail, "optioned:"):
			continue // the Python test patched the guardrail model itself
		case c.Detail == "missing tokens: numeric":
			continue // a token that is not a string: the strict decode calls the file corrupt (deviations log)
		case c.Label == "orphan-profiles" && !hasPath(c.Before, ".claudewheel/options.json"):
			continue // no options.json: a missing owned file is an error, not an empty one (deviations log)
		}
		t.Run(c.Fn, func(t *testing.T) {
			home := testkit.Isolate(t)
			stubs := testkit.StubPath(t)
			testkit.Stub(t, stubs, "systemctl", "exit 1")
			testkit.Stub(t, stubs, "df", "exit 1")
			root := filepath.Join(home, "h")
			if err := os.MkdirAll(root, 0o755); err != nil {
				t.Fatal(err)
			}
			materialize(t, root, c.Before)
			ws, err := workspace.FromHome(root)
			if err != nil {
				t.Fatal(err)
			}
			completeOptions(t, ws)
			today, err := time.ParseInLocation("2006-01-02", c.Today, time.Local)
			if err != nil {
				t.Fatal(err)
			}
			results := Run(Inputs{FX: testkit.FX(), Workspace: ws, Executable: "/opt/claudewheel", Today: today})
			for _, r := range results {
				if r.Label != c.Label {
					continue
				}
				detail := pythonFix.ReplaceAllString(strings.ReplaceAll(r.Detail, root, "@ROOT@"), "patch-profiles")
				if r.OK != c.OK || unreadable.ReplaceAllString(detail, "corrupt or unreadable (…)$1") != unreadable.ReplaceAllString(c.Detail, "corrupt or unreadable (…)$1") {
					t.Errorf("call %d %s:\n got: ok=%v %q\nwant: ok=%v %q", i, c.Label, r.OK, detail, c.OK, c.Detail)
				}
				return
			}
			t.Fatalf("call %d: no %s check", i, c.Label)
		})
	}
}

// pythonFix matches the fix the Go checks name, patch-profiles with its
// required selection, where the Python named the bare command (see the
// deviations log).
var pythonFix = regexp.MustCompile(`patch-profiles (--all-profiles|--profile [^')]+)`)

// completeOptions gives an options.json the Python tests wrote with only
// some segments the keys the strict reader requires: the defaults, with the
// file's own values and pinned lists.
func completeOptions(t *testing.T, ws workspace.Workspace) {
	t.Helper()
	data, err := os.ReadFile(ws.OptionsFile())
	if os.IsNotExist(err) {
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	var partial map[string]struct {
		Values []string `json:"values"`
		Pinned []string `json:"pinned"`
	}
	if err := json.Unmarshal(data, &partial); err != nil {
		return
	}
	opts := appconfig.DefaultOptions()
	for key, seg := range partial {
		full := opts[key]
		if seg.Values != nil {
			full.Values = seg.Values
		}
		if seg.Pinned != nil {
			full.Pinned = seg.Pinned
		}
		opts[key] = full
	}
	out, err := jsonfile.MarshalIndented(opts)
	if err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, ws.OptionsFile(), string(out))
}

func hasPath(entries []snapEntry, path string) bool {
	for _, e := range entries {
		if e.Path == path {
			return true
		}
	}
	return false
}

// unreadable matches the reason a token file could not be read: the JSON
// parsers word it differently, and the Python ended the sentence with a
// period where Go's errors do not.
var unreadable = regexp.MustCompile(`corrupt or unreadable \(.*?\)(; token resolution cannot proceed\. Fix or remove the file, then retry)\.?`)
