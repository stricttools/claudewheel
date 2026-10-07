package appconfig

import (
	"fmt"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Keys an older claudewheel wrote that no longer exist.
const (
	// retiredSchemaVersionKey numbered config.json for a migration engine
	// that no longer exists.
	retiredSchemaVersionKey = "_schema_version"
	// retiredScratchpadSnoozeKey silenced the scratchpad-cleanup step until
	// a date; dismissal is now per directory (State.ScratchpadDismissed).
	retiredScratchpadSnoozeKey = "scratchpad_snooze_until"
)

// Change is one change Upgrade makes, or that a file needs before it can be
// read. File is the file's path relative to the workspace root.
type Change struct {
	File        string
	Description string
}

func (c Change) String() string {
	return c.File + ": " + c.Description
}

// describeChanges joins the descriptions of changes to one file.
func describeChanges(changes []Change) string {
	parts := make([]string, len(changes))
	for i, c := range changes {
		parts[i] = c.Description
	}
	return strings.Join(parts, "; ")
}

// A planner applies to a decoded file the changes Upgrade makes to it, in
// place, and describes them. It ignores a document of the wrong shape: the
// strict decode that follows reports that.
type planner func(tree jsonfile.Value) []Change

// The files Upgrade converts, in the order it reports them. The theme files
// come last: their missing keys are also filled on every read (see
// LoadTheme), so a theme file never blocks a read.
func upgradeFiles(ws workspace.Workspace) []upgradeFile {
	files := []upgradeFile{
		{path: ws.ConfigFile(), plan: planConfig, check: decodesAs[Config]},
		{path: ws.SegmentsFile(), plan: planSegments, check: decodesAs[[]Segment]},
		{path: ws.OptionsFile(), plan: planOptions, check: decodesAs[Options]},
		{path: ws.StateFile(), plan: planState, check: decodesAs[State]},
	}
	for _, name := range []string{ThemeDark, ThemeLight} {
		path, err := ThemeFile(ws, name)
		if err != nil {
			panic(fmt.Sprintf("appconfig: built-in theme name %q refused: %v", name, err))
		}
		files = append(files, upgradeFile{path: path, plan: planTheme(name), check: decodesAs[Theme]})
	}
	return files
}

type upgradeFile struct {
	path  string
	plan  planner
	check func(data []byte) error
}

func decodesAs[T any](data []byte) error {
	var v T
	return jsonfile.DecodeStrict(data, &v)
}

// Upgrade converts a workspace an older claudewheel wrote so this one can
// read it, and returns what it changed (nothing for a current workspace):
// config.json loses _schema_version, state.json loses scratchpad_snooze_until
// and a vanilla_guardrails_opt_in that is not a boolean, and every file gains
// the keys the defaults declare that it lacks. Values already present are
// never changed. Every converted file is checked to decode before any file
// is written, so a file this cannot convert leaves the workspace unchanged
// and is an error naming what is still wrong. A missing file is an error
// naming `claudewheel launch`.
func Upgrade(fx *effects.FX, ws workspace.Workspace) ([]Change, error) {
	type pending struct {
		path string
		data []byte
	}
	var writes []pending
	var all []Change
	for _, f := range upgradeFiles(ws) {
		data, err := readOwnedFile(f.path)
		if err != nil {
			return nil, err
		}
		tree, err := jsonfile.Decode(data)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", f.path, err)
		}
		changes := f.plan(tree)
		if len(changes) == 0 {
			if err := f.check(data); err != nil {
				return nil, fmt.Errorf("%s: %w", f.path, err)
			}
			continue
		}
		out, err := jsonfile.MarshalIndented(tree)
		if err != nil {
			return nil, fmt.Errorf("encoding %s: %w", f.path, err)
		}
		if err := f.check(out); err != nil {
			return nil, fmt.Errorf("%s is still not readable after converting it (%s): %w", f.path, describeChanges(changes), err)
		}
		name := fileName(ws, f.path)
		for _, c := range changes {
			all = append(all, Change{File: name, Description: c.Description})
		}
		writes = append(writes, pending{path: f.path, data: out})
	}
	for _, w := range writes {
		if err := fx.WriteFileAtomic(w.path, w.data); err != nil {
			return nil, err
		}
	}
	return all, nil
}

// defaultTree returns v, a default, as an ordered tree.
func defaultTree(v any) jsonfile.Value {
	tree, err := jsonfile.Normalize(v)
	if err != nil {
		panic(fmt.Sprintf("appconfig: a default is not representable as JSON: %v", err))
	}
	return tree
}

// addMissingKeys adds to target each top-level key of defaults it lacks, in
// the defaults' order, and describes each addition with prefix.
func addMissingKeys(target, defaults *jsonfile.Object, prefix string) []Change {
	var changes []Change
	for _, key := range defaults.Keys() {
		if _, present := target.Get(key); present {
			continue
		}
		def, _ := defaults.Get(key)
		target.Set(key, jsonfile.Clone(def))
		changes = append(changes, Change{Description: fmt.Sprintf("added %s%q", prefix, key)})
	}
	return changes
}

func planConfig(tree jsonfile.Value) []Change {
	obj, ok := tree.(*jsonfile.Object)
	if !ok {
		return nil
	}
	var changes []Change
	if obj.Delete(retiredSchemaVersionKey) {
		changes = append(changes, Change{Description: fmt.Sprintf("removed the retired key %q", retiredSchemaVersionKey)})
	}
	return append(changes, addMissingKeys(obj, defaultTree(DefaultConfig()).(*jsonfile.Object), "")...)
}

// planSegments completes each segment the defaults declare with the
// attributes it lacks. A default segment the file does not list stays
// absent: the user removed it.
func planSegments(tree jsonfile.Value) []Change {
	list, ok := tree.([]jsonfile.Value)
	if !ok {
		return nil
	}
	defaults := map[string]*jsonfile.Object{}
	for _, d := range defaultTree(DefaultSegments()).([]jsonfile.Value) {
		obj := d.(*jsonfile.Object)
		key, _ := obj.Get("key")
		defaults[key.(string)] = obj
	}
	var changes []Change
	for _, item := range list {
		obj, ok := item.(*jsonfile.Object)
		if !ok {
			continue
		}
		key, _ := obj.Get("key")
		name, ok := key.(string)
		if !ok {
			continue
		}
		def, ok := defaults[name]
		if !ok {
			continue
		}
		changes = append(changes, addMissingKeys(obj, def, fmt.Sprintf("to segment %q the attribute ", name))...)
	}
	return changes
}

// planOptions completes each segment entry the defaults declare with its
// values and pinned lists when it lacks them, and gives the model segment
// its entry and its discovery declaration when it has none, since model
// discovery runs only when options.json declares it. Other absent entries
// and discovery declarations stay absent.
func planOptions(tree jsonfile.Value) []Change {
	obj, ok := tree.(*jsonfile.Object)
	if !ok {
		return nil
	}
	defaults := defaultTree(DefaultOptions()).(*jsonfile.Object)
	var changes []Change
	if _, present := obj.Get(SegmentKeyModel); !present {
		obj.Set(SegmentKeyModel, jsonfile.NewObject())
		changes = append(changes, Change{Description: fmt.Sprintf("added the segment %q", SegmentKeyModel)})
	}
	for _, key := range defaults.Keys() {
		item, present := obj.Get(key)
		if !present {
			continue
		}
		entry, ok := item.(*jsonfile.Object)
		if !ok {
			continue
		}
		defValue, _ := defaults.Get(key)
		def := defValue.(*jsonfile.Object)
		wanted := jsonfile.NewObject()
		for _, field := range []string{"values", "pinned"} {
			v, _ := def.Get(field)
			wanted.Set(field, v)
		}
		if key == SegmentKeyModel {
			if v, ok := def.Get("discovery"); ok {
				wanted.Set("discovery", v)
			}
		}
		changes = append(changes, addMissingKeys(entry, wanted, fmt.Sprintf("to segment %q the key ", key))...)
	}
	return changes
}

func planState(tree jsonfile.Value) []Change {
	obj, ok := tree.(*jsonfile.Object)
	if !ok {
		return nil
	}
	var changes []Change
	if obj.Delete(retiredScratchpadSnoozeKey) {
		changes = append(changes, Change{Description: fmt.Sprintf("removed the retired key %q", retiredScratchpadSnoozeKey)})
	}
	// The older claudewheel read a null here, or the per-project object an
	// even older one wrote, as "not chosen yet", which absence now means.
	const optIn = "vanilla_guardrails_opt_in"
	if v, present := obj.Get(optIn); present {
		if _, isBool := v.(bool); !isBool {
			obj.Delete(optIn)
			changes = append(changes, Change{Description: fmt.Sprintf("removed %q, which held %s rather than a boolean (the choice is asked again)", optIn, jsonfile.Describe(v))})
		}
	}
	return append(changes, addMissingKeys(obj, defaultTree(DefaultState()).(*jsonfile.Object), "")...)
}

// planTheme fills the keys a theme file lacks from its default theme.
func planTheme(name string) planner {
	return func(tree jsonfile.Value) []Change {
		obj, ok := tree.(*jsonfile.Object)
		if !ok {
			return nil
		}
		var changes []Change
		for _, path := range mergeMissing(obj, defaultThemeTree(name)) {
			changes = append(changes, Change{Description: fmt.Sprintf("added %q", path)})
		}
		return changes
	}
}
