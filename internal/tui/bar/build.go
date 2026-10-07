package bar

import (
	"fmt"
	"maps"
	"os"
	"slices"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/discover"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/profiles"
)

// The segment keys the bar treats specially.
const (
	keyProfile   = profiles.Segment
	keyModel     = "model"
	keyDirectory = "directory"
)

// mergeSpec returns how segment key merges and orders its collections.
// The model list accumulates every model ever seen (options.json feeds the
// defaults collection) next to what the API serves now, newest release
// first.
func mergeSpec(key string) ([]Source, OptionSort) {
	switch key {
	case discover.VersionSegmentKey:
		return DefaultOrder(), SortVersionDesc
	case keyProfile, "github":
		return []Source{SourcePinned, SourceDiscovered}, SortNone
	case keyModel:
		return []Source{SourcePinned, SourceDiscovered, SourceDefaults}, SortReleaseDateDesc
	case "mcp", "permissions":
		return []Source{SourcePinned, SourceDefaults}, SortNone
	}
	return DefaultOrder(), SortNone
}

// defaultsFor returns segment key's defaults collection: for the model
// segment the list options.json has accumulated, used as it is (an empty
// list shows an empty picker, never another list); for every other segment
// the shipped default values.
func defaultsFor(key string, opt appconfig.OptionSegment) []string {
	if key == keyModel {
		return slices.Clone(opt.Values)
	}
	def, ok := appconfig.DefaultOptions()[key]
	if !ok {
		return nil
	}
	return slices.Clone(def.Values)
}

// recordedMetadata converts options.json's metadata of one segment.
func recordedMetadata(opt appconfig.OptionSegment) map[string]ValueMetadata {
	out := map[string]ValueMetadata{}
	if opt.Metadata == nil {
		return out
	}
	for value, m := range *opt.Metadata {
		var vm ValueMetadata
		if m.ModelID != nil {
			vm.ModelID = *m.ModelID
		}
		if m.CreatedAt != nil {
			vm.CreatedAt = *m.CreatedAt
		}
		out[value] = vm
	}
	return out
}

// verifierFor returns the check that keeps a value a new discovery of opt
// no longer lists, or nil when opt has no discovery or drops such values.
func verifierFor(env discover.Env, opt appconfig.OptionSegment) (func(string) (bool, error), error) {
	if opt.Discovery == nil {
		return nil, nil
	}
	return discover.Verifier(env, *opt.Discovery)
}

// newSegment builds the empty segment one segments.json entry describes.
func newSegment(def appconfig.Segment) (*Segment, error) {
	order, sortBy := mergeSpec(def.Key)
	state, err := NewSegmentState(order, sortBy)
	if err != nil {
		return nil, err
	}
	seg := &Segment{
		Key:         def.Key,
		Label:       def.Label,
		State:       state,
		ShowOptions: def.ShowOptions,
		Wrap:        def.Wrap,
		MinWidth:    def.MinWidth,
		MaxWidth:    def.MaxWidth,
		Required:    def.Required,
		Searchable:  def.Searchable,
		TabAdvances: def.TabAdvances,
		Creatable:   def.Creatable != nil && *def.Creatable,
		Freeform:    def.Freeform != nil && *def.Freeform,
		Unavailable: map[string]bool{},
		Rejected:    map[string]string{},
	}
	if def.Key == keyModel {
		seg.OptionRequires = discover.ModelOptionRequires()
	}
	return seg, nil
}

// Built is a freshly built bar and the refresh failures its startup
// discovery reported, for the bar to show.
type Built struct {
	Bar           *Bar
	RefreshErrors []string
}

// BuildBar builds the bar from store: one segment per enabled entry of
// segments.json, in that order, with its defaults and pinned options from
// options.json and what startup discovery finds (discover.Startup: never
// the network). Discovered models are recorded in options.json, and the
// state discovery wants kept is saved to state.json. Each segment selects
// its value from the last launch; a directory segment left without a
// selection selects the working directory when it is under the home
// directory.
func BuildBar(fx *effects.FX, env discover.Env, store *appconfig.Store) (Built, error) {
	var built Built
	var segments []*Segment
	for _, def := range store.Segments {
		if !slices.Contains(store.Config.EnabledSegments, def.Key) {
			continue
		}
		opt := store.Options[def.Key]
		seg, err := newSegment(def)
		if err != nil {
			return Built{}, err
		}
		seg.State.SetDefaults(defaultsFor(def.Key, opt))
		for _, v := range opt.Pinned {
			seg.State.AddPinned(v)
		}
		seg.State.SetMetadata(recordedMetadata(opt))

		result, ran, err := discover.Startup(env, opt)
		if err != nil {
			return Built{}, fmt.Errorf("discovering the %s segment's options: %w", def.Key, err)
		}
		if ran {
			verify, err := verifierFor(env, opt)
			if err != nil {
				return Built{}, err
			}
			if err := seg.State.applyResult(result, verify); err != nil {
				return Built{}, fmt.Errorf("discovering the %s segment's options: %w", def.Key, err)
			}
			result.ApplyToState(&store.State)
			if result.RefreshError != nil {
				built.RefreshErrors = append(built.RefreshErrors, fmt.Sprintf("%s: %v", def.Label, result.RefreshError))
			}
			if def.Key == keyModel {
				if err := store.RecordDiscoveredModels(fx, result.Values, result.ModelRecord()); err != nil {
					return Built{}, err
				}
			}
		}
		if last, ok := store.State.LastConfig[def.Key]; ok {
			seg.SelectValue(last)
		}
		segments = append(segments, seg)
	}

	// Keep the refreshed caches and pruned directories even when the user
	// quits without launching.
	if err := store.SaveState(fx); err != nil {
		return Built{}, err
	}

	cwd, err := os.Getwd()
	if err != nil {
		return Built{}, fmt.Errorf("reading the working directory: %w", err)
	}
	if tilde, ok := discover.TildePath(env.Home, cwd); ok {
		for _, seg := range segments {
			if seg.Key == keyDirectory && seg.SelectedIndex() < 0 {
				seg.SelectValue(tilde)
			}
		}
	}

	b, err := NewBar(segments)
	if err != nil {
		return Built{}, err
	}
	built.Bar = b
	return built, nil
}

// ApplyOverrides selects each segment's value given on the command line
// (segment key -> value). A freeform segment takes a value it does not list
// as a launch-only option. A value a segment that is not freeform does not
// list, and a key no segment on the bar has, are errors naming what the bar
// holds.
func ApplyOverrides(b *Bar, overrides map[string]string) error {
	keys := slices.Sorted(maps.Keys(overrides))
	for _, key := range keys {
		value := overrides[key]
		seg, ok := b.Segment(key)
		if !ok {
			held := make([]string, len(b.Segments))
			for i, s := range b.Segments {
				held[i] = s.Key
			}
			return fmt.Errorf("unknown segment %q (the bar has: %v)", key, held)
		}
		if seg.SelectValue(value) {
			continue
		}
		if !seg.Freeform {
			return fmt.Errorf("the %s segment has no option %q (it offers: %v)", key, value, seg.Options())
		}
		seg.State.AddEphemeral(value)
		seg.SelectValue(value)
	}
	return nil
}

// MergeResults merges background discovery results (segment key -> result)
// into the bar. Each segment keeps its selection when its value is still
// offered, and otherwise selects its value from the last launch (last).
// Values a new discovery no longer lists are kept when they still exist on
// disk, as opts' discovery declarations decide.
func MergeResults(b *Bar, results map[string]discover.Result, env discover.Env, opts appconfig.Options, last map[string]string) error {
	for _, seg := range b.Segments {
		r, ok := results[seg.Key]
		if !ok {
			continue
		}
		verify, err := verifierFor(env, opts[seg.Key])
		if err != nil {
			return err
		}
		current, had := seg.Selected()
		if err := seg.State.applyResult(r, verify); err != nil {
			return fmt.Errorf("merging the %s segment's options: %w", seg.Key, err)
		}
		if had && seg.SelectValue(current) {
			continue
		}
		if v, ok := last[seg.Key]; ok {
			seg.SelectValue(v)
		}
	}
	return nil
}

// refreshProfiles runs profile discovery again (after a profile was
// created, authenticated, or deleted) and updates the segment's options
// and authentication sets.
func refreshProfiles(seg *Segment, store profiles.Store) error {
	fresh, err := discover.Profiles(store)
	if err != nil {
		return err
	}
	return seg.State.applyResult(fresh, nil)
}

// EvaluateRequires recomputes every segment's unavailable options from the
// cross-segment requirements and the current selections; see
// discover.EvaluateRequires.
func EvaluateRequires(b *Bar, locator install.Locator) {
	requires := map[string]discover.Requires{}
	for _, seg := range b.Segments {
		requires[seg.Key] = seg.OptionRequires
	}
	unavailable := discover.EvaluateRequires(requires, b.Selections(), locator)
	for _, seg := range b.Segments {
		seg.Unavailable = unavailable[seg.Key]
		if seg.Unavailable == nil {
			seg.Unavailable = map[string]bool{}
		}
	}
}

// cloneState deep-copies a state, for the background discovery to read
// while the bar changes its own copy.
func cloneState(st appconfig.State) appconfig.State {
	out := st
	out.LastConfig = maps.Clone(st.LastConfig)
	out.RecentDirs = slices.Clone(st.RecentDirs)
	if st.NpmVersionsCache != nil {
		c := *st.NpmVersionsCache
		c.Versions = slices.Clone(c.Versions)
		out.NpmVersionsCache = &c
	}
	if st.ModelListCache != nil {
		c := *st.ModelListCache
		c.Models = slices.Clone(c.Models)
		out.ModelListCache = &c
	}
	if st.AuthBrowser != nil {
		v := *st.AuthBrowser
		out.AuthBrowser = &v
	}
	if st.ProjectHookApprovals != nil {
		m := maps.Clone(*st.ProjectHookApprovals)
		out.ProjectHookApprovals = &m
	}
	if st.VanillaGuardrailsOptIn != nil {
		v := *st.VanillaGuardrailsOptIn
		out.VanillaGuardrailsOptIn = &v
	}
	if st.ScratchpadDismissed != nil {
		d := slices.Clone(*st.ScratchpadDismissed)
		out.ScratchpadDismissed = &d
	}
	return out
}
