package appconfig

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/testkit"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// pythonWorkspace copies the workspace the Python's first run wrote,
// recorded by a generator deleted with the Python, under a fresh home.
func pythonWorkspace(t *testing.T) workspace.Workspace {
	t.Helper()
	ws := testkit.Workspace(t)
	testkit.CopyTree(t, "testdata/python-workspace/.claudewheel", ws.Root())
	return ws
}

func changeStrings(changes []Change) []string {
	out := make([]string, len(changes))
	for i, c := range changes {
		out[i] = c.String()
	}
	return out
}

func TestUpgradeConvertsTheWorkspaceThePythonWrites(t *testing.T) {
	ws := pythonWorkspace(t)
	_, err := Load(ws)
	if !errors.Is(err, ErrUpgradeNeeded) || !strings.Contains(err.Error(), "claudewheel upgrade-workspace") {
		t.Fatalf("Load before converting: %v", err)
	}
	before := map[string]string{}
	for _, f := range []string{ws.SegmentsFile(), ws.OptionsFile(), ws.StateFile()} {
		before[f] = testkit.ReadFile(t, f)
	}
	changes, err := Upgrade(testkit.FX(), ws)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{`config.json: removed the retired key "_schema_version"`}
	if !slices.Equal(changeStrings(changes), want) {
		t.Fatalf("changes %q, want %q", changeStrings(changes), want)
	}
	for f, text := range before {
		if testkit.ReadFile(t, f) != text {
			t.Errorf("%s changed", f)
		}
	}
	if _, err := Load(ws); err != nil {
		t.Fatalf("Load after converting: %v", err)
	}
	again, err := Upgrade(testkit.FX(), ws)
	if err != nil || len(again) != 0 {
		t.Fatalf("second Upgrade: %q %v", changeStrings(again), err)
	}
}

func TestUpgradeRemovesTheRetiredStateKeys(t *testing.T) {
	ws := pythonWorkspace(t)
	testkit.WriteFile(t, ws.StateFile(), `{
  "last_config": {"profile": "work"},
  "recent_dirs": ["/p"],
  "launch_count": 3,
  "scratchpad_snooze_until": "2026-08-27T09:33:46.933192+00:00",
  "vanilla_guardrails_opt_in": {"/p": true}
}
`)
	changes, err := Upgrade(testkit.FX(), ws)
	if err != nil {
		t.Fatal(err)
	}
	got := strings.Join(changeStrings(changes), "\n")
	for _, want := range []string{`removed the retired key "scratchpad_snooze_until"`, `removed "vanilla_guardrails_opt_in"`} {
		if !strings.Contains(got, want) {
			t.Errorf("changes lack %q:\n%s", want, got)
		}
	}
	st, err := ReadState(ws)
	if err != nil {
		t.Fatal(err)
	}
	if st.VanillaGuardrailsOptIn != nil || st.LaunchCount != 3 || st.LastConfig["profile"] != "work" {
		t.Fatalf("state %+v", st)
	}
}

func TestUpgradeKeepsABooleanOptInAndAddsMissingKeysWithoutChangingValues(t *testing.T) {
	ws := pythonWorkspace(t)
	testkit.WriteFile(t, ws.StateFile(), `{"last_config": {}, "launch_count": 0, "vanilla_guardrails_opt_in": false}`)
	testkit.WriteFile(t, ws.ConfigFile(), `{"theme": "light", "minimap": "always"}`)
	if _, err := Upgrade(testkit.FX(), ws); err != nil {
		t.Fatal(err)
	}
	st, err := ReadState(ws)
	if err != nil {
		t.Fatal(err)
	}
	if st.VanillaGuardrailsOptIn == nil || *st.VanillaGuardrailsOptIn || st.RecentDirs == nil {
		t.Fatalf("state %+v", st)
	}
	s, err := Load(ws)
	if err != nil {
		t.Fatal(err)
	}
	if s.Config.Theme != "light" || s.Config.Minimap != "always" || s.Config.DefaultClient != "claude" {
		t.Fatalf("config %+v", s.Config)
	}
}

func TestUpgradeRefusesAFileItCannotConvertAndWritesNothing(t *testing.T) {
	ws := pythonWorkspace(t)
	config := testkit.ReadFile(t, ws.ConfigFile())
	testkit.WriteFile(t, ws.StateFile(), `{"last_config": {}, "recent_dirs": [], "launch_count": 0, "session_memory_max": 5}`)
	if _, err := Upgrade(testkit.FX(), ws); err == nil {
		t.Fatal("a leftover key was converted")
	}
	if testkit.ReadFile(t, ws.ConfigFile()) != config {
		t.Fatal("config.json was written although another file could not be converted")
	}
}

func TestLoadOfAMissingWorkspaceNamesLaunch(t *testing.T) {
	ws := testkit.Workspace(t)
	_, err := Load(ws)
	if !errors.Is(err, ErrNotSetUp) || !strings.Contains(err.Error(), "claudewheel launch") {
		t.Fatalf("Load: %v", err)
	}
	if _, statErr := os.Stat(ws.Root()); !os.IsNotExist(statErr) {
		t.Fatal("Load created the workspace")
	}
}

func TestLoadRefusesACorruptFile(t *testing.T) {
	ws := testkit.Workspace(t)
	if _, err := Ensure(testkit.FX(), ws); err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, ws.OptionsFile(), "{not json")
	if _, err := Load(ws); err == nil || !strings.Contains(err.Error(), "options.json") {
		t.Fatalf("Load: %v", err)
	}
}

func TestEnsureNeverChangesExistingFiles(t *testing.T) {
	ws := testkit.Workspace(t)
	if _, err := Ensure(testkit.FX(), ws); err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, ws.SharedSettingsFile(), "{}\n")
	if _, err := Ensure(testkit.FX(), ws); err != nil {
		t.Fatal(err)
	}
	if testkit.ReadFile(t, ws.SharedSettingsFile()) != "{}\n" {
		t.Fatal("Ensure replaced shared-settings.json")
	}
}

func TestOptionMutators(t *testing.T) {
	ws := testkit.Workspace(t)
	fx := testkit.FX()
	if _, err := Ensure(fx, ws); err != nil {
		t.Fatal(err)
	}
	if _, err := AddPinned(fx, ws, SegmentKeyDirectory, "/p"); err != nil {
		t.Fatal(err)
	}
	created := "2026-01-01"
	opts, err := RecordDiscovered(fx, ws, SegmentKeyProfile, []string{"work", "hn", "work"}, map[string]ValueMetadata{"work": {CreatedAt: &created}})
	if err != nil {
		t.Fatal(err)
	}
	if got := opts[SegmentKeyProfile].Values; !slices.Equal(got, []string{"work", "hn"}) {
		t.Fatalf("values %v", got)
	}
	if _, err := RenameOptionValue(fx, ws, SegmentKeyProfile, "work", "job"); err != nil {
		t.Fatal(err)
	}
	opts, err = RemoveOptionValue(fx, ws, SegmentKeyProfile, "hn")
	if err != nil {
		t.Fatal(err)
	}
	seg := opts[SegmentKeyProfile]
	if !slices.Equal(seg.Values, []string{"job"}) {
		t.Fatalf("values %v", seg.Values)
	}
	if m, ok := seg.MetadataFor("job"); !ok || m.CreatedAt == nil || *m.CreatedAt != created {
		t.Fatalf("metadata did not move with the rename: %+v", seg.Metadata)
	}
	read, err := ReadOptions(ws)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(read[SegmentKeyDirectory].Pinned, []string{"/p"}) {
		t.Fatalf("pinned %v", read[SegmentKeyDirectory].Pinned)
	}
}

func TestRecordLaunch(t *testing.T) {
	ws := testkit.Workspace(t)
	fx := testkit.FX()
	s, err := Ensure(fx, ws)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < RecentDirsLimit+3; i++ {
		if err := s.RecordLaunch(fx, map[string]string{SegmentKeyDirectory: filepath.Join("/d", string(rune('a'+i)))}); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.RecordLaunch(fx, map[string]string{SegmentKeyDirectory: "/d/k", SegmentKeyProfile: "work"}); err != nil {
		t.Fatal(err)
	}
	st, err := ReadState(ws)
	if err != nil {
		t.Fatal(err)
	}
	if st.LaunchCount != RecentDirsLimit+4 || len(st.RecentDirs) != RecentDirsLimit || st.RecentDirs[0] != "/d/k" {
		t.Fatalf("state %+v", st)
	}
	if strings.Count(strings.Join(st.RecentDirs, ","), "/d/k") != 1 {
		t.Fatal("a recent directory is listed twice")
	}
	if st.LastConfig[SegmentKeyProfile] != "work" {
		t.Fatalf("last config %v", st.LastConfig)
	}
}

func TestSaveStateKeepsOutOfBandKeysWrittenMeanwhile(t *testing.T) {
	ws := testkit.Workspace(t)
	fx := testkit.FX()
	s, err := Ensure(fx, ws)
	if err != nil {
		t.Fatal(err)
	}
	if err := SetAuthBrowser(fx, ws, "copy"); err != nil {
		t.Fatal(err)
	}
	if err := DismissScratchpadDirs(fx, ws, []string{"/tmp/claude-1/x", "/tmp/claude-1/x"}); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordLaunch(fx, map[string]string{}); err != nil {
		t.Fatal(err)
	}
	st, err := ReadState(ws)
	if err != nil {
		t.Fatal(err)
	}
	if st.AuthBrowser == nil || *st.AuthBrowser != "copy" {
		t.Fatal("the auth browser written meanwhile was lost")
	}
	dismissed, err := DismissedScratchpadDirs(ws)
	if err != nil || !slices.Equal(dismissed, []string{"/tmp/claude-1/x"}) {
		t.Fatalf("dismissed %v %v", dismissed, err)
	}
	if err := DismissScratchpadDirs(fx, ws, []string{"relative"}); err == nil {
		t.Fatal("a relative directory was dismissed")
	}
}

func TestProjectHookApprovalIsKeyedByTheResolvedDirectory(t *testing.T) {
	ws := testkit.Workspace(t)
	fx := testkit.FX()
	if _, err := Ensure(fx, ws); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	if err := SetProjectHookApproval(fx, ws, link, "fp1"); err != nil {
		t.Fatal(err)
	}
	fp, ok, err := ProjectHookApproval(ws, dir)
	if err != nil || !ok || fp != "fp1" {
		t.Fatalf("approval %q %v %v", fp, ok, err)
	}
	if _, _, err := ProjectHookApproval(ws, filepath.Join(dir, "missing")); err == nil {
		t.Fatal("a missing directory has a key")
	}
}

func TestInodesRenamesAndStaleEntries(t *testing.T) {
	ws := testkit.Workspace(t)
	fx := testkit.FX()
	shared := ws.Shared()
	base := t.TempDir()
	oldDir := filepath.Join(base, "old")
	gone := filepath.Join(base, "gone")
	for _, d := range []string{oldDir, gone} {
		if err := os.Mkdir(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := RecordInode(fx, shared, d); err != nil {
			t.Fatal(err)
		}
	}
	newDir := filepath.Join(base, "new")
	if err := os.Rename(oldDir, newDir); err != nil {
		t.Fatal(err)
	}
	if err := RecordInode(fx, shared, newDir); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(gone); err != nil {
		t.Fatal(err)
	}
	fileParent := filepath.Join(base, "file")
	testkit.WriteFile(t, fileParent, "x")
	inodes, err := jsonfile.DecodeObject([]byte(testkit.ReadFile(t, shared.InodesFile())))
	if err != nil {
		t.Fatal(err)
	}
	inodes.Set(filepath.Join(fileParent, "under"), "999999999")
	out, _ := jsonfile.MarshalIndented(inodes)
	testkit.WriteFile(t, shared.InodesFile(), string(out))
	inodes, _ = jsonfile.DecodeObject(out)
	analysis, err := AnalyzeInodes(inodes)
	if err != nil {
		t.Fatal(err)
	}
	if len(analysis.Renames) != 1 || analysis.Renames[0] != (InodeRename{Old: oldDir, New: newDir}) {
		t.Fatalf("renames %+v", analysis.Renames)
	}
	if !slices.Equal(analysis.Stale, []string{gone, filepath.Join(fileParent, "under")}) {
		t.Fatalf("stale %v", analysis.Stale)
	}
	pruned, err := PruneStaleInodes(fx, shared)
	if err != nil || len(pruned) != 2 {
		t.Fatalf("pruned %v %v", pruned, err)
	}
	inodes, _ = jsonfile.DecodeObject([]byte(testkit.ReadFile(t, shared.InodesFile())))
	if !slices.Equal(inodes.Keys(), []string{oldDir, newDir}) {
		t.Fatalf("left %v", inodes.Keys())
	}
	if pruned, err := PruneStaleInodes(fx, shared); err != nil || pruned != nil {
		t.Fatalf("second prune %v %v", pruned, err)
	}
}

func TestThemes(t *testing.T) {
	ws := testkit.Workspace(t)
	if _, err := Ensure(testkit.FX(), ws); err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, filepath.Join(ws.ThemesDir(), "dark.json"), `{"global": {}}`)
	theme, err := LoadTheme(ws, ThemeDark)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(theme, DefaultTheme(ThemeDark)) {
		t.Fatal("a partial theme was not completed from the default")
	}
	if testkit.ReadFile(t, filepath.Join(ws.ThemesDir(), "dark.json")) != `{"global": {}}` {
		t.Fatal("LoadTheme wrote the theme file")
	}
	for _, name := range []string{"", ThemeAuto, "../x", "custom"} {
		if _, err := LoadTheme(ws, name); err == nil {
			t.Errorf("LoadTheme(%q) succeeded", name)
		}
	}
	resolve := func(answer string, err error) func() (string, error) {
		return func() (string, error) { return answer, err }
	}
	cases := []struct {
		name, answer, want string
	}{
		{ThemeAuto, "", ThemeDark},
		{ThemeAuto, ThemeLight, ThemeLight},
		{"custom", "ignored", "custom"},
	}
	for _, c := range cases {
		got, err := ResolveThemeName(c.name, resolve(c.answer, nil))
		if err != nil || got != c.want {
			t.Errorf("ResolveThemeName(%q) with %q = %q %v", c.name, c.answer, got, err)
		}
	}
	if _, err := ResolveThemeName(ThemeAuto, resolve("", errors.New("no tty"))); err == nil {
		t.Error("a failed terminal query was not an error")
	}
}
