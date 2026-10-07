package hookscripts

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/pathstat"
)

// Action is what deploying one script, link, or unit did. The values are the
// words the deploy-hooks command prints.
type Action string

const (
	// Created: the file did not exist and was written.
	Created Action = "created"
	// Overwritten: the file existed and was replaced.
	Overwritten Action = "overwritten"
	// Exists: the file existed and was left alone, or the link already
	// pointed at its target.
	Exists Action = "exists"
	// Linked: the link did not exist and was created.
	Linked Action = "linked"
	// Relinked: something else stood at the link and was replaced.
	Relinked Action = "relinked"
	// Foreign: something else stands at the link and was left alone.
	Foreign Action = "foreign"
)

// scriptMode is the mode of every deployed script.
const scriptMode = 0o755

// ScriptResult is what DeployScripts did with one script.
type ScriptResult struct {
	Name   string
	Path   string
	Action Action
}

// LinkResult is what LinkPathCommands did with one PATH command.
type LinkResult struct {
	Link   string
	Target string
	Action Action
}

// DeployScripts writes the named scripts into scriptsDir, mode 0755. A
// script already there is left alone (Exists) unless forceOverwrite is set.
// Every name is checked before anything is written: an unknown name is an
// error and nothing is deployed.
func DeployScripts(fx *effects.FX, names []string, scriptsDir string, forceOverwrite bool) ([]ScriptResult, error) {
	texts := make([]string, len(names))
	for i, name := range names {
		text, err := Script(name)
		if err != nil {
			return nil, err
		}
		texts[i] = text
	}
	if err := fx.MkdirAll(scriptsDir); err != nil {
		return nil, err
	}
	results := make([]ScriptResult, 0, len(names))
	for i, name := range names {
		dest := filepath.Join(scriptsDir, name)
		present, err := pathstat.Exists(dest)
		if err != nil {
			return results, err
		}
		if present && !forceOverwrite {
			results = append(results, ScriptResult{Name: name, Path: dest, Action: Exists})
			continue
		}
		action := Created
		if present {
			action = Overwritten
		}
		// A new file renamed over the old one, never a rewrite in place: bash
		// reads a script as it runs it, so a running copy (a heavy waiting on
		// its command, a hook mid-run) must keep the file it started from.
		if err := fx.WriteFileAtomic(dest, []byte(texts[i])); err != nil {
			return results, err
		}
		if err := fx.Chmod(dest, scriptMode); err != nil {
			return results, err
		}
		results = append(results, ScriptResult{Name: name, Path: dest, Action: action})
	}
	return results, nil
}

// MissingScripts returns, in order, the names that are scripts claudewheel
// deploys and are not in scriptsDir. Names outside the registry are skipped.
func MissingScripts(names []string, scriptsDir string) ([]string, error) {
	var missing []string
	for _, name := range names {
		if !IsScript(name) {
			continue
		}
		present, err := pathstat.Exists(filepath.Join(scriptsDir, name))
		if err != nil {
			return nil, err
		}
		if !present {
			missing = append(missing, name)
		}
	}
	return missing, nil
}

// LinkPathCommands links each PathCommands member of names from binDir to
// its deployed copy in scriptsDir; other names are skipped. A link already
// pointing at its target is Exists. Anything else standing at the link is
// Foreign and left alone, unless forceOverwrite is set: then a new symbolic
// link is renamed over it (Relinked), so a running copy of the old command
// keeps its file and no reader ever finds the name missing.
func LinkPathCommands(fx *effects.FX, names []string, scriptsDir, binDir string, forceOverwrite bool) ([]LinkResult, error) {
	var results []LinkResult
	for _, name := range names {
		if !IsPathCommand(name) {
			continue
		}
		link := filepath.Join(binDir, name)
		target := filepath.Join(scriptsDir, name)
		info, err := os.Lstat(link)
		if errors.Is(err, fs.ErrNotExist) {
			if err := fx.MkdirAll(binDir); err != nil {
				return results, err
			}
			if err := fx.Symlink(link, target); err != nil {
				return results, err
			}
			results = append(results, LinkResult{Link: link, Target: target, Action: Linked})
			continue
		}
		if err != nil {
			return results, err
		}
		if info.Mode()&fs.ModeSymlink != 0 {
			dest, err := os.Readlink(link)
			if err != nil {
				return results, err
			}
			if dest == target {
				results = append(results, LinkResult{Link: link, Target: target, Action: Exists})
				continue
			}
		}
		if !forceOverwrite {
			results = append(results, LinkResult{Link: link, Target: target, Action: Foreign})
			continue
		}
		staged := filepath.Join(binDir, fmt.Sprintf(".%s.claudewheel-%d.tmp", name, os.Getpid()))
		if err := fx.Symlink(staged, target); err != nil {
			return results, err
		}
		if err := fx.Rename(staged, link); err != nil {
			return results, err
		}
		results = append(results, LinkResult{Link: link, Target: target, Action: Relinked})
	}
	return results, nil
}

// DeployedState is how a script in the scripts directory compares with the
// registry.
type DeployedState int

const (
	// DeployedAbsent: the script is not deployed; absence is not drift.
	DeployedAbsent DeployedState = iota
	// DeployedCurrent: the deployed bytes are the registry's text.
	DeployedCurrent
	// DeployedDiffers: the deployed bytes differ from the registry's text.
	DeployedDiffers
	// DeployedUnreadable: the deployed copy could not be read; Err says why.
	DeployedUnreadable
)

// DeployedScript is one registry script's state in a scripts directory.
type DeployedScript struct {
	Name  string
	State DeployedState
	// Err is set for DeployedUnreadable only.
	Err error
}

// CheckDeployed compares every registry script, in name order, with its copy
// in scriptsDir byte for byte. It only reads; the health check decides what
// the states mean.
func CheckDeployed(scriptsDir string) ([]DeployedScript, error) {
	names := Names()
	states := make([]DeployedScript, 0, len(names))
	for _, name := range names {
		want, err := Script(name)
		if err != nil {
			return nil, err
		}
		path := filepath.Join(scriptsDir, name)
		present, err := pathstat.Exists(path)
		if err != nil {
			states = append(states, DeployedScript{Name: name, State: DeployedUnreadable, Err: err})
			continue
		}
		if !present {
			states = append(states, DeployedScript{Name: name, State: DeployedAbsent})
			continue
		}
		disk, err := os.ReadFile(path)
		switch {
		case err != nil:
			states = append(states, DeployedScript{Name: name, State: DeployedUnreadable, Err: err})
		case string(disk) == want:
			states = append(states, DeployedScript{Name: name, State: DeployedCurrent})
		default:
			states = append(states, DeployedScript{Name: name, State: DeployedDiffers})
		}
	}
	return states, nil
}
