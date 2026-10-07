// Package testkit holds what the test suites share: an isolated environment
// with a throwaway home, a workspace under it, and stand-in programs on PATH.
// Only _test.go files import it.
package testkit

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/testisolation/go/hygiene"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Isolate gives the test a throwaway HOME, an isolated git configuration, and
// no ambient credentials, keeping the Go toolchain caches. It returns the
// home directory.
func Isolate(t *testing.T) string {
	t.Helper()
	hygiene.Isolate(t, hygiene.Preserve(hygiene.GoPath, hygiene.GoModCache, hygiene.GoCache))
	return os.Getenv("HOME")
}

// Workspace isolates the test and returns the workspace under its home.
func Workspace(t *testing.T) workspace.Workspace {
	t.Helper()
	home := Isolate(t)
	ws, err := workspace.FromHome(home)
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

// FX returns an effects handle that performs everything, its info lines
// discarded.
func FX() *effects.FX { return effects.Standalone(io.Discard) }

// StubPath puts a fresh directory first on PATH, keeping the system
// directories after it, and returns it. Stubs written there shadow the real
// programs.
func StubPath(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("PATH", dir+":/usr/bin:/bin")
	return dir
}

// Stub writes an executable bash script named name into dir with body after
// the shebang line. Every call is appended to <dir>/<name>.calls, one line
// per call with the arguments separated by spaces, before body runs.
func Stub(t *testing.T, dir, name, body string) {
	t.Helper()
	log := filepath.Join(dir, name+".calls")
	script := "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> '" + log + "'\n" + body + "\n"
	if err := os.WriteFile(filepath.Join(dir, name), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
}

// Calls returns the argument lines the stub name recorded, in order.
func Calls(t *testing.T, dir, name string) []string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, name+".calls"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
}

// WriteFile writes data to path, creating the parent directories.
func WriteFile(t *testing.T, path, data string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
		t.Fatal(err)
	}
}

// ReadFile returns the contents of path.
func ReadFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}
