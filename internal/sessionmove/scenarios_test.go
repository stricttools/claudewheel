package sessionmove

import (
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/testkit"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// treeEntry is one file, directory, or link of a scenario tree; "@ROOT@"
// stands for the scenario's root and "@EROOT@" for its store-dir encoding.
type treeEntry struct {
	Path    string `json:"path"`
	Type    string `json:"type"`
	Content string `json:"content,omitempty"`
	Target  string `json:"target,omitempty"`
}

// scenario is one session operation the Python ran on a tree, and the tree
// it left (scripts/python-expectations session-scenarios).
type scenario struct {
	Name   string            `json:"name"`
	Tree   []treeEntry       `json:"tree"`
	Op     map[string]any    `json:"op"`
	Error  *string           `json:"error"`
	Result []treeEntry       `json:"result"`
	Extra  map[string]string `json:"-"`
}

func materialize(t *testing.T, root string, tree []treeEntry) {
	t.Helper()
	eroot := workspace.EncodePathUntruncated(root)
	for _, e := range tree {
		path := filepath.Join(root, strings.ReplaceAll(e.Path, "@EROOT@", eroot))
		switch e.Type {
		case "dir":
			if err := os.MkdirAll(path, 0o755); err != nil {
				t.Fatal(err)
			}
		case "file":
			content := strings.ReplaceAll(strings.ReplaceAll(e.Content, "@ROOT@", root), "@EROOT@", eroot)
			testkit.WriteFile(t, path, content)
		case "symlink":
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(strings.ReplaceAll(e.Target, "@ROOT@", root), path); err != nil {
				t.Fatal(err)
			}
		default:
			t.Fatalf("entry type %q", e.Type)
		}
	}
}

// Lifecycle lines carry a fresh id and time on every run.
var volatile = regexp.MustCompile(`"(id|at)":"[^"]*"`)

func dump(t *testing.T, root string) []treeEntry {
	t.Helper()
	eroot := workspace.EncodePathUntruncated(root)
	norm := func(s string) string {
		return strings.ReplaceAll(strings.ReplaceAll(s, root, "@ROOT@"), eroot, "@EROOT@")
	}
	var out []treeEntry
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path == root {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		rel = norm(rel)
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			target, err := os.Readlink(path)
			if err != nil {
				return err
			}
			out = append(out, treeEntry{Path: rel, Type: "symlink", Target: norm(target)})
		case info.IsDir():
			entries, err := os.ReadDir(path)
			if err != nil {
				return err
			}
			if len(entries) == 0 {
				out = append(out, treeEntry{Path: rel, Type: "dir"})
			}
		default:
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			out = append(out, treeEntry{Path: rel, Type: "file", Content: norm(string(data))})
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out
}

func maskVolatile(entries []treeEntry) {
	for i := range entries {
		if strings.Contains(entries[i].Path, "/lifecycle/") {
			entries[i].Content = volatile.ReplaceAllString(entries[i].Content, `"$1":"*"`)
		}
	}
}

func runScenario(t *testing.T, root string, sc scenario) error {
	t.Helper()
	ws, err := workspace.FromHome(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", root)
	fx := testkit.FX()
	sub := func(key string) string { return strings.ReplaceAll(sc.Op[key].(string), "@ROOT@", root) }
	dirs, err := profiles.New(ws).ConfigDirs()
	if err != nil {
		t.Fatal(err)
	}
	switch sc.Op["name"] {
	case "mv":
		_, err := Mv(fx, ws, dirs, sub("old"), sub("new"), MvOptions{PostHoc: sc.Op["post_hoc"].(bool), Quiet: true})
		return err
	case "move-session":
		_, err := MoveSession(fx, ws, dirs, sc.Op["session"].(string), sub("directory"))
		return err
	case "import":
		var mappings []PathMapping
		for _, m := range sc.Op["mappings"].([]any) {
			pair := m.([]any)
			mappings = append(mappings, PathMapping{
				From: strings.ReplaceAll(pair[0].(string), "@ROOT@", root),
				To:   strings.ReplaceAll(pair[1].(string), "@ROOT@", root),
			})
		}
		res, err := Import(fx, ws.Shared(), sub("source"), mappings, ImportOptions{Warnings: io.Discard})
		if err == nil && len(res.Collisions) > 0 {
			return errors.New("collisions")
		}
		return err
	case "migrate":
		find := func(name string) sessions.ProfileConfigDir {
			for _, d := range dirs {
				if d.Name == name {
					return d
				}
			}
			t.Fatalf("no profile %s", name)
			return sessions.ProfileConfigDir{}
		}
		_, err := Migrate(fx, find(sc.Op["src"].(string)), find(sc.Op["dst"].(string)), SessionChoice{Session: sc.Op["session"].(string)})
		return err
	}
	t.Fatalf("operation %v", sc.Op["name"])
	return nil
}

func TestSessionOperationsMatchThePython(t *testing.T) {
	data, err := os.ReadFile("testdata/python-scenarios.json")
	if err != nil {
		t.Fatal(err)
	}
	var scenarios []scenario
	if err := json.Unmarshal(data, &scenarios); err != nil {
		t.Fatal(err)
	}
	for _, sc := range scenarios {
		t.Run(sc.Name, func(t *testing.T) {
			testkit.Isolate(t)
			root, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			materialize(t, root, sc.Tree)
			opErr := runScenario(t, root, sc)
			if (opErr != nil) != (sc.Error != nil) {
				t.Fatalf("error %v, the Python's %v", opErr, sc.Error)
			}
			got := dump(t, root)
			want := append([]treeEntry(nil), sc.Result...)
			maskVolatile(got)
			maskVolatile(want)
			gotByPath := map[string]treeEntry{}
			for _, e := range got {
				gotByPath[e.Path] = e
			}
			wantByPath := map[string]treeEntry{}
			for _, e := range want {
				wantByPath[e.Path] = e
			}
			for _, w := range want {
				g, ok := gotByPath[w.Path]
				if !ok {
					t.Errorf("missing %s %s", w.Type, w.Path)
					continue
				}
				if g != w {
					t.Errorf("%s differs:\n got: %+v\nwant: %+v", w.Path, g, w)
				}
			}
			for _, g := range got {
				if _, ok := wantByPath[g.Path]; !ok {
					t.Errorf("extra %s %s", g.Type, g.Path)
				}
			}
		})
	}
}
