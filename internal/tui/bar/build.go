package bar

import (
	"errors"
	"fmt"
	"maps"
	"os"
	"slices"
	"strings"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/discover"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/profiles"
)

// mergeSpec returns how segment key merges and orders its collections.
// The model list accumulates every model ever seen (options.json feeds the
// defaults collection) next to what the API serves now, newest release
// first.
func mergeSpec(key string) ([]Source, OptionSort) {
	switch key {
	case appconfig.SegmentKeyVersion:
		return DefaultOrder(), SortVersionDesc
	case appconfig.SegmentKeyProfile, appconfig.SegmentKeyGitHub:
		return []Source{SourcePinned, SourceDiscovered}, SortNone
	case appconfig.SegmentKeyModel:
		return []Source{SourcePinned, SourceDiscovered, SourceDefaults}, SortReleaseDateDesc
	case appconfig.SegmentKeyMCP, appconfig.SegmentKeyPermissions:
		return []Source{SourcePinned, SourceDefaults}, SortNone
	}
	return DefaultOrder(), SortNone
}

// defaultsFor returns segment key's defaults collection: for the model
// segment the list options.json has accumulated, used as it is (an empty
// list shows an empty picker, never another list); for every other segment
// the shipped default values.
func defaultsFor(key string, opt appconfig.OptionSegment) []string {
	if key == appconfig.SegmentKeyModel {
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
	if def.Key == appconfig.SegmentKeyModel {
		seg.OptionRequires = discover.ModelOptionRequires()
	}
	return seg, nil
}

// seededSegment builds the segment one segments.json entry describes with
// what options.json holds for it (opt): its defaults, its pinned options,
// and the recorded metadata. Discovery has not run on it.
func seededSegment(def appconfig.Segment, opt appconfig.OptionSegment) (*Segment, error) {
	seg, err := newSegment(def)
	if err != nil {
		return nil, err
	}
	seg.State.SetDefaults(defaultsFor(def.Key, opt))
	for _, v := range opt.Pinned {
		seg.State.AddPinned(v)
	}
	seg.State.SetMetadata(recordedMetadata(opt))
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
		seg, err := seededSegment(def, opt)
		if err != nil {
			return Built{}, err
		}

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
			if def.Key == appconfig.SegmentKeyModel {
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
			if seg.Key == appconfig.SegmentKeyDirectory && seg.SelectedIndex() < 0 {
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

// Offered is what a segment lists once its discovery has run to completion.
type Offered struct {
	Values []string
	// RefreshError is the network or subprocess failure the discovery
	// reported; Values then come from a cache or the segment's own values.
	RefreshError error
}

// CheckPreset refuses value, given for segment key with -s, when offered
// (what the segment lists once its discovery ran to completion) does not
// hold it. The refusal names the values the segment does list, and the
// discovery's refresh failure, which may be why value is missing.
func CheckPreset(key, value string, offered Offered) error {
	if slices.Contains(offered.Values, value) {
		return nil
	}
	listed := "nothing"
	if len(offered.Values) > 0 {
		listed = strings.Join(offered.Values, ", ")
	}
	msg := fmt.Sprintf("-s %s=%s: the %s segment does not offer %q; it offers: %s", key, value, key, value, listed)
	if offered.RefreshError != nil {
		msg += fmt.Sprintf(" (its discovery could not refresh: %v)", offered.RefreshError)
	}
	return errors.New(msg)
}

// OfferedOptions returns what segment def offers once its discovery has run
// to completion, network and subprocesses included, merged and ordered as
// the bar merges them, for checking a command-line value when the bar is
// skipped. It writes nothing.
func OfferedOptions(env discover.Env, store *appconfig.Store, def appconfig.Segment) (Offered, error) {
	opt := store.Options[def.Key]
	seg, err := seededSegment(def, opt)
	if err != nil {
		return Offered{}, err
	}
	if opt.Discovery == nil {
		return Offered{Values: slices.Clone(seg.Options())}, nil
	}
	result, err := discover.Run(env, opt)
	if err != nil {
		return Offered{}, fmt.Errorf("discovering the %s segment's options: %w", def.Key, err)
	}
	if err := seg.State.applyResult(result, nil); err != nil {
		return Offered{}, fmt.Errorf("discovering the %s segment's options: %w", def.Key, err)
	}
	return Offered{Values: slices.Clone(seg.Options()), RefreshError: result.RefreshError}, nil
}

// discoverPresets runs to completion the slow discovery of each segment on
// b that is not freeform and is given a value on the command line, so
// ApplyOverrides checks that value against everything the segment offers (a
// GitHub account appears only once gh has answered). The results are kept
// as the background discovery's are: merged into the segment, models
// recorded in options.json, and the caches saved to state.json. It returns
// the keys it ran, which the background discovery then skips, each with
// its refresh failure (nil when the refresh succeeded).
func discoverPresets(fx *effects.FX, env discover.Env, store *appconfig.Store, b *Bar, overrides map[string]string) (map[string]error, error) {
	ran := map[string]error{}
	for _, key := range slices.Sorted(maps.Keys(overrides)) {
		seg, ok := b.Segment(key)
		if !ok || seg.Freeform || overrides[key] == "" {
			continue
		}
		opt := store.Options[key]
		if opt.Discovery == nil {
			continue
		}
		slow, err := discover.IsSlow(opt.Discovery.Type)
		if err != nil {
			return nil, err
		}
		if !slow {
			// Startup discovery already ran it in full.
			continue
		}
		result, err := discover.Run(env, opt)
		if err != nil {
			return nil, fmt.Errorf("discovering the %s segment's options: %w", key, err)
		}
		verify, err := verifierFor(env, opt)
		if err != nil {
			return nil, err
		}
		if err := seg.State.applyResult(result, verify); err != nil {
			return nil, fmt.Errorf("discovering the %s segment's options: %w", key, err)
		}
		result.ApplyToState(&store.State)
		if key == appconfig.SegmentKeyModel {
			if err := store.RecordDiscoveredModels(fx, result.Values, result.ModelRecord()); err != nil {
				return nil, err
			}
		}
		ran[key] = result.RefreshError
	}
	if len(ran) > 0 {
		if err := store.SaveState(fx); err != nil {
			return nil, err
		}
	}
	return ran, nil
}

// ApplyOverrides selects each segment's value given on the command line
// (segment key -> value). A freeform segment takes a value it does not list
// as a launch-only option. On any other segment a value it does not list is
// refused by CheckPreset, naming the refresh failure refreshFailures holds
// for that segment (see discoverPresets). An empty value, which a launch
// that skips the bar reads as no value, is an error here: the bar would
// select the last launch's value again when discovery arrives. A key no
// segment on the bar has is an error naming the segments.
func ApplyOverrides(b *Bar, overrides map[string]string, refreshFailures map[string]error) error {
	names := slices.Sorted(maps.Keys(overrides))
	for _, key := range names {
		value := overrides[key]
		seg, ok := b.Segment(key)
		if !ok {
			held := make([]string, len(b.Segments))
			for i, s := range b.Segments {
				held[i] = s.Key
			}
			return fmt.Errorf("unknown segment %q (the bar has: %v)", key, held)
		}
		if value == "" {
			return fmt.Errorf("-s %s= gives the %s segment no value, which only a launch that skips the bar takes; clear it on the bar instead", key, key)
		}
		if seg.SelectValue(value) {
			continue
		}
		if !seg.Freeform {
			return CheckPreset(key, value, Offered{Values: seg.Options(), RefreshError: refreshFailures[key]})
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
// discover.EvaluateRequires, whose error it returns, leaving the segments
// as they were.
func EvaluateRequires(b *Bar, locator install.Locator) error {
	requires := map[string]discover.Requires{}
	for _, seg := range b.Segments {
		requires[seg.Key] = seg.OptionRequires
	}
	unavailable, err := discover.EvaluateRequires(requires, b.Selections(), locator)
	if err != nil {
		return err
	}
	for _, seg := range b.Segments {
		seg.Unavailable = unavailable[seg.Key]
		if seg.Unavailable == nil {
			seg.Unavailable = map[string]bool{}
		}
	}
	return nil
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
