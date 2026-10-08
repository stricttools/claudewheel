package hookscripts

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// hookCase is one run of a hook the Python's hook execution tests made, and
// what the hook printed and exited with, recorded by a generator deleted with the Python.
type hookCase struct {
	Script string `json:"script"`
	Stdin  string `json:"stdin"`
	Exit   int    `json:"exit"`
	Stdout string `json:"stdout"`
}

// TestHooksDecideAsRecorded runs every recorded hook payload through the
// script Go deploys and expects the recorded output and exit status: the
// guardrail's deny, escalate, and advise decisions for every command the
// Python tests exercised.
func TestHooksDecideAsRecorded(t *testing.T) {
	data, err := os.ReadFile("testdata/python-hook-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []hookCase
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	paths := map[string]string{}
	for _, c := range cases {
		if _, ok := paths[c.Script]; ok {
			continue
		}
		text, err := Script(c.Script)
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(dir, c.Script)
		if err := os.WriteFile(path, []byte(text), 0o755); err != nil {
			t.Fatal(err)
		}
		paths[c.Script] = path
	}
	for i, c := range cases {
		cmd := exec.Command("bash", paths[c.Script])
		cmd.Stdin = strings.NewReader(c.Stdin)
		var out bytes.Buffer
		cmd.Stdout = &out
		err := cmd.Run()
		code := 0
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			code = exitErr.ExitCode()
		} else if err != nil {
			t.Fatal(err)
		}
		if code != c.Exit || out.String() != c.Stdout {
			t.Errorf("case %d (%s, stdin %s): exit %d, stdout %q; recorded exit %d, stdout %q",
				i, c.Script, c.Stdin, code, out.String(), c.Exit, c.Stdout)
		}
	}
}
