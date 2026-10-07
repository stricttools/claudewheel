// Package health runs the diagnostic checks of `claudewheel health` and of
// the launch: /tmp usage, the profiles' shared-store links, hook wiring,
// settings defaults, permission and hook drift against the canonical
// guardrail model and shared-settings.json, deployed hook script drift,
// relocated hook paths, the probe runner's service, stored tokens and their
// expiry, auth shadowing, orphan profile directories, file permissions, and
// directory renames recorded in the inode map.
//
// Health only reads: it changes no file. Each check yields a Result; a check
// that cannot be carried out (an unreadable file it needs, a failed
// command) is not OK and says why.
package health

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Result is the outcome of one check.
type Result struct {
	OK     bool
	Label  string
	Detail string
}

// Line renders the result as the health report prints it.
func (r Result) Line() string {
	status := "OK"
	if !r.OK {
		status = "WARN"
	}
	return fmt.Sprintf("  [%s] %s: %s", status, r.Label, r.Detail)
}

// AllOK reports whether every result is OK; `claudewheel health` exits 1
// when it is not.
func AllOK(results []Result) bool {
	for _, r := range results {
		if !r.OK {
			return false
		}
	}
	return true
}

func ok(label, detail string) Result {
	return Result{OK: true, Label: label, Detail: detail}
}

func warn(label, detail string) Result {
	return Result{OK: false, Label: label, Detail: detail}
}

// failed is the result of a check that could not be carried out.
func failed(label string, err error) Result {
	return warn(label, "check failed: "+err.Error())
}

// Inputs are what the checks read beyond the workspace.
type Inputs struct {
	// FX runs the read-only commands (df, systemctl); it may be read-only.
	FX *effects.FX
	// Workspace is the claudewheel workspace checked.
	Workspace workspace.Workspace
	// Executable is the absolute path of the claudewheel binary the probe
	// runner's unit should start.
	Executable string
	// Today dates the token expiry check.
	Today time.Time
}

// checker holds the inputs and the profiles, enumerated once with an
// unreadable token file read as no token, so the token checks report that
// file by profile while every other check still runs.
type checker struct {
	in      Inputs
	store   profiles.Store
	all     []profiles.Profile
	enumErr error
}

// managed returns the discovered profiles other than the default profile,
// which Claude Code owns and which no guardrail or token check covers.
func (c *checker) managed() []profiles.Profile {
	var out []profiles.Profile
	for _, p := range c.all {
		if p.Name != profiles.DefaultName {
			out = append(out, p)
		}
	}
	return out
}

// Run runs every check in report order.
func Run(in Inputs) []Result {
	store := profiles.New(in.Workspace)
	all, err := store.Discover(profiles.CorruptTokenAsNone)
	c := &checker{in: in, store: store, all: all, enumErr: err}
	return []Result{
		c.tmpfsQuota(),
		c.tmpClaudeSize(),
		c.sharedSymlinks(),
		c.hooksWired(),
		c.settingsDefaults(),
		c.sharedSettingsDrift(),
		c.canonicalPermissionsDrift(),
		c.deployedHookDrift(),
		c.relocatedHookPaths(),
		c.probeRunner(),
		c.tokens(),
		c.tokenExpiry(),
		c.authShadow(),
		c.orphanProfiles(),
		c.filePermissions(),
		c.inodeRenames(),
	}
}

// readObject reads a JSON file whose top level must be an object, reporting
// false when it does not exist.
func readObject(path string) (*jsonfile.Object, bool, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	o, err := jsonfile.DecodeObject(data)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", path, err)
	}
	return o, true, nil
}

// objectOr returns o[key] when it is an object, an empty object otherwise.
func objectOr(o *jsonfile.Object, key string) *jsonfile.Object {
	v, _ := o.Get(key)
	if child, isObject := v.(*jsonfile.Object); isObject {
		return child
	}
	return jsonfile.NewObject()
}

// valueOr returns o[key], or def when the key is absent.
func valueOr(o *jsonfile.Object, key string, def jsonfile.Value) jsonfile.Value {
	if v, present := o.Get(key); present {
		return v
	}
	return def
}

// dumps writes v as Python's bare json.dumps; a decoded tree always encodes.
func dumps(v jsonfile.Value) string {
	text, err := jsonfile.MarshalSpacedASCII(v)
	if err != nil {
		panic(fmt.Sprintf("health: a decoded JSON value did not encode: %v", err))
	}
	return string(text)
}

// settingsPath is a profile's settings.json.
func settingsPath(p profiles.Profile) string {
	return filepath.Join(p.Path, profiles.SettingsFileName)
}

// numberValue reads v as a JSON number, reporting false for anything else.
func numberValue(v jsonfile.Value) (float64, bool) {
	n, isNumber := v.(json.Number)
	if !isNumber {
		return 0, false
	}
	// An out-of-range number is an infinity, as Python's float() makes it.
	f, err := strconv.ParseFloat(string(n), 64)
	if err != nil && !errors.Is(err, strconv.ErrRange) {
		return 0, false
	}
	return f, true
}
