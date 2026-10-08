package workspace

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The Python's store-dir names for the same paths, recorded by a generator deleted with the Python.
type encodeCase struct {
	PathB64     string `json:"path_b64"`
	Untruncated string `json:"untruncated"`
	Encoded     string `json:"encoded"`
}

func TestEncodePathMatchesThePython(t *testing.T) {
	data, err := os.ReadFile("testdata/python-encode-path.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []encodeCase
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("no cases")
	}
	for _, c := range cases {
		raw, err := base64.StdEncoding.DecodeString(c.PathB64)
		if err != nil {
			t.Fatal(err)
		}
		p := string(raw)
		if got := EncodePathUntruncated(p); got != c.Untruncated {
			t.Errorf("EncodePathUntruncated(%q) = %q, Python %q", p, got, c.Untruncated)
		}
		if got := EncodePath(p); got != c.Encoded {
			t.Errorf("EncodePath(%q) = %q, Python %q", p, got, c.Encoded)
		}
	}
}

func TestEncodePathDistributesOverJoins(t *testing.T) {
	a, b := "/home/m/Projects", "sub dir.x"
	if got, want := EncodePathUntruncated(a+"/"+b), EncodePathUntruncated(a)+"-"+EncodePathUntruncated(b); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestDefaultRefusesAMissingOrRelativeHome(t *testing.T) {
	for _, home := range []string{"", "relative/home"} {
		t.Setenv("HOME", home)
		if _, err := Default(); err == nil {
			t.Errorf("Default() with HOME=%q succeeded", home)
		}
	}
}

func TestFromHomeBuildsEveryPathUnderHome(t *testing.T) {
	home := t.TempDir()
	ws, err := FromHome(home)
	if err != nil {
		t.Fatal(err)
	}
	root := filepath.Join(home, ".claudewheel")
	checks := map[string]string{
		ws.Root():                 root,
		ws.ClaudeDir():            filepath.Join(home, ".claude"),
		ws.BinDir():               filepath.Join(home, ".local", "bin"),
		ws.SystemdUserDir():       filepath.Join(home, ".config", "systemd", "user"),
		ws.ProfilesDir():          filepath.Join(root, "profiles"),
		ws.SharedSettingsFile():   filepath.Join(root, "shared-settings.json"),
		ws.InodesFile():           filepath.Join(root, "shared", "inodes.json"),
		ws.Shared().ProjectsDir(): filepath.Join(root, "shared", "projects"),
	}
	for got, want := range checks {
		if got != want {
			t.Errorf("got %q, want %q", got, want)
		}
	}
}
