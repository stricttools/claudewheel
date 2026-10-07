package appconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/testkit"
)

// pythonDefaults are the Python's DEFAULT_* values, written by
// scripts/python-expectations app-defaults.
func pythonDefaults(t *testing.T) map[string]jsonfile.Value {
	t.Helper()
	data, err := os.ReadFile("testdata/python-defaults.json")
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatal(err)
	}
	out := map[string]jsonfile.Value{}
	for k, v := range raw {
		tree, err := jsonfile.Decode(v)
		if err != nil {
			t.Fatal(err)
		}
		out[k] = tree
	}
	return out
}

// Differences from the Python's defaults that the plan or a ruling made.
func adjustPythonDefaults(t *testing.T, d map[string]jsonfile.Value) {
	t.Helper()
	// Plan mode is not offered (the revision of ruling 12).
	perms, _ := d["DEFAULT_OPTIONS"].(*jsonfile.Object).Get(SegmentKeyPermissions)
	perms.(*jsonfile.Object).Set("values", jsonfile.StringArray([]string{"bypass", "default", "auto"}))
	// The retired schema version key (plan, upgrade-workspace).
	d["DEFAULT_CONFIG"].(*jsonfile.Object).Delete("_schema_version")
}

func TestEnsureWritesThePythonDefaults(t *testing.T) {
	ws := testkit.Workspace(t)
	if _, err := Ensure(testkit.FX(), ws); err != nil {
		t.Fatal(err)
	}
	want := pythonDefaults(t)
	adjustPythonDefaults(t, want)
	files := map[string]string{
		"DEFAULT_CONFIG":      ws.ConfigFile(),
		"DEFAULT_SEGMENTS":    ws.SegmentsFile(),
		"DEFAULT_OPTIONS":     ws.OptionsFile(),
		"DEFAULT_STATE":       ws.StateFile(),
		"DEFAULT_THEME_DARK":  filepath.Join(ws.ThemesDir(), "dark.json"),
		"DEFAULT_THEME_LIGHT": filepath.Join(ws.ThemesDir(), "light.json"),
	}
	for name, path := range files {
		got, err := jsonfile.Decode([]byte(testkit.ReadFile(t, path)))
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		if !jsonfile.Equal(got, want[name]) {
			g, _ := jsonfile.MarshalIndented(got)
			w, _ := jsonfile.MarshalIndented(want[name])
			t.Errorf("%s differs from the Python's %s:\n got: %s\nwant: %s", filepath.Base(path), name, g, w)
		}
	}
}
