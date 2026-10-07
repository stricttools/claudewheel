package proberunner

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/stricttools/claudewheel/internal/testkit"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// snapEntry is one entry of a workspace snapshot; "@ROOT@" stands for the
// directory the workspace lives in.
type snapEntry struct {
	Path    string `json:"path"`
	Type    string `json:"type"`
	Content string `json:"content,omitempty"`
	Target  string `json:"target,omitempty"`
	MtimeNS int64  `json:"mtime_ns,omitempty"`
}

// recordedCall is one call the Python's probe runner tests made, with the
// workspace before and after it (scripts/python-expectations
// probe-runner-calls).
type recordedCall struct {
	Fn        string             `json:"fn"`
	Before    []snapEntry        `json:"before"`
	After     []snapEntry        `json:"after"`
	Kwargs    map[string]any     `json:"kwargs"`
	Entry     map[string]any     `json:"entry"`
	Described map[string]*string `json:"described"`
	Error     *string            `json:"error"`
}

// The Python tests put the workspace root at cw/ and Claude Code's own
// directory at claude/; a workspace built from a home has them at
// .claudewheel/ and .claude/.
// "@PID@" is where the Python tests used their own process id for a live
// client; the replay uses its own.
var layout = strings.NewReplacer("@ROOT@/cw/", "@ROOT@/.claudewheel/", "@ROOT@/claude/", "@ROOT@/.claude/",
	"@PID@", strconv.Itoa(os.Getpid()))

func relPath(p string) string {
	switch {
	case p == "cw" || strings.HasPrefix(p, "cw/"):
		return ".claudewheel" + strings.TrimPrefix(p, "cw")
	case p == "claude" || strings.HasPrefix(p, "claude/"):
		return ".claude" + strings.TrimPrefix(p, "claude")
	}
	return p
}

func materialize(t *testing.T, root string, entries []snapEntry) {
	t.Helper()
	for _, e := range entries {
		path := filepath.Join(root, relPath(e.Path))
		switch e.Type {
		case "dir":
			if err := os.MkdirAll(path, 0o755); err != nil {
				t.Fatal(err)
			}
		case "fifo":
			if err := syscall.Mkfifo(path, 0o600); err != nil {
				t.Fatal(err)
			}
		case "symlink":
			if err := os.Symlink(strings.ReplaceAll(layout.Replace(e.Target), "@ROOT@", root), path); err != nil {
				t.Fatal(err)
			}
		case "file":
			content := strings.ReplaceAll(layout.Replace(e.Content), "@ROOT@", root)
			testkit.WriteFile(t, path, content)
			mtime := time.Unix(0, e.MtimeNS)
			if err := os.Chtimes(path, mtime, mtime); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// Fresh ids and times on every run.
var volatile = regexp.MustCompile(`"(id|at)": ?"[^"]*"`)

func normalized(root string, entries []snapEntry, fromPython bool) map[string]snapEntry {
	out := map[string]snapEntry{}
	for _, e := range entries {
		if fromPython {
			e.Path = relPath(e.Path)
			e.Content = layout.Replace(e.Content)
			e.Target = layout.Replace(e.Target)
		} else {
			e.Content = strings.ReplaceAll(e.Content, root, "@ROOT@")
			e.Target = strings.ReplaceAll(e.Target, root, "@ROOT@")
		}
		e.Content = volatile.ReplaceAllString(e.Content, `"$1":*`)
		e.MtimeNS = 0
		out[e.Path] = e
	}
	return out
}

func snapshot(t *testing.T, root string) []snapEntry {
	t.Helper()
	var out []snapEntry
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || path == root {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			target, _ := os.Readlink(path)
			out = append(out, snapEntry{Path: rel, Type: "symlink", Target: target})
		case info.IsDir():
			out = append(out, snapEntry{Path: rel, Type: "dir"})
		case info.Mode()&os.ModeNamedPipe != 0:
			out = append(out, snapEntry{Path: rel, Type: "fifo"})
		default:
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			out = append(out, snapEntry{Path: rel, Type: "file", Content: string(data)})
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func TestRunnerCallsMatchThePython(t *testing.T) {
	data, err := os.ReadFile("testdata/python-calls.json")
	if err != nil {
		t.Fatal(err)
	}
	var calls []recordedCall
	if err := json.Unmarshal(data, &calls); err != nil {
		t.Fatal(err)
	}
	for i, call := range calls {
		t.Run(call.Fn, func(t *testing.T) {
			testkit.Isolate(t)
			root, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			materialize(t, root, call.Before)
			ws, err := workspace.FromHome(root)
			if err != nil {
				t.Fatal(err)
			}
			fx := testkit.FX()
			var callErr error
			switch call.Fn {
			case "process_entry":
				line, _ := json.Marshal(call.Entry)
				entry, err := ParseEntry([]byte(layout.Replace(string(line))))
				if err != nil {
					t.Fatal(err)
				}
				describe := func(unit string) (string, bool, error) {
					d, ok := call.Described[unit]
					if !ok {
						t.Fatalf("call %d: the Python never described %s", i, unit)
					}
					if d == nil {
						return "", false, nil
					}
					return *d, true, nil
				}
				_, callErr = ProcessEntry(fx, ws, entry, describe, io.Discard)
			case "end_probes":
				_, callErr = EndProbes(fx, ws, int64(call.Kwargs["now_ms"].(float64)))
			case "settle_reports":
				states := []string{"pending", "handed"}
				if raw, ok := call.Kwargs["states"]; ok {
					states = nil
					for _, s := range raw.([]any) {
						states = append(states, s.(string))
					}
				}
				_, callErr = SettleReports(fx, ws, states, io.Discard)
			case "tick":
				callErr = Tick(fx, ws, call.Kwargs["expire"].(bool), io.Discard)
			default:
				t.Fatalf("function %s", call.Fn)
			}
			if (callErr != nil) != (call.Error != nil) {
				t.Fatalf("call %d: error %v, the Python's %v", i, callErr, call.Error)
			}
			got := normalized(root, snapshot(t, root), false)
			want := normalized(root, call.After, true)
			var paths []string
			for p := range want {
				paths = append(paths, p)
			}
			for p := range got {
				if _, ok := want[p]; !ok {
					paths = append(paths, p)
				}
			}
			sort.Strings(paths)
			for _, p := range paths {
				g, gok := got[p]
				w, wok := want[p]
				switch {
				case !gok:
					t.Errorf("call %d: missing %s %s", i, w.Type, p)
				case !wok:
					t.Errorf("call %d: extra %s %s", i, g.Type, p)
				case g != w:
					t.Errorf("call %d: %s differs:\n got: %q\nwant: %q", i, p, g.Content, w.Content)
				}
			}
		})
	}
}
