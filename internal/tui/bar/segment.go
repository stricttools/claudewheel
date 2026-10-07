// Package bar is the launch bar: the row of segments (profile, version,
// model, directory, and the rest) the user picks a launch from. It holds
// the segments' option state and selection, draws the bar with its fan-out
// options, scroll arrows, and minimap, and runs the key loop that returns
// the chosen selections to the launch.
//
// Option discovery is the discover package's; this package orders what
// discovery found, merges what the background refresh brings while the bar
// is open, and decides what each key does.
package bar

import (
	"fmt"
	"maps"
	"slices"
	"sort"

	"github.com/stricttools/claudewheel/internal/discover"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// PlusEntry is the entry a creatable segment shows after its options:
// choosing it creates a new option. It is never an option itself.
const PlusEntry = "+"

// Source names the collection an option comes from.
type Source string

const (
	// SourcePinned options were added by the user and kept in options.json.
	SourcePinned Source = "pinned"
	// SourceDiscovered options were found by the segment's discovery.
	SourceDiscovered Source = "discovered"
	// SourceDefaults options are the segment's shipped (or, for the model
	// segment, accumulated) values.
	SourceDefaults Source = "defaults"
	// SourceEphemeral options were typed into a freeform segment or given
	// on the command line for this launch only.
	SourceEphemeral Source = "ephemeral"
)

// DefaultOrder is the collection order of a segment with no order of its
// own. Ephemeral options always follow, after any sort.
func DefaultOrder() []Source {
	return []Source{SourcePinned, SourceDiscovered, SourceDefaults}
}

// OptionSort is how a segment orders its options after merging its
// collections.
type OptionSort int

const (
	// SortNone keeps the merged order.
	SortNone OptionSort = iota
	// SortVersionDesc orders versions newest first (install.CompareVersions).
	SortVersionDesc
	// SortReleaseDateDesc orders models newest release first; see
	// SegmentState.Options.
	SortReleaseDateDesc
)

// ValueMetadata describes one option value: what options.json records for
// it and what discovery found.
type ValueMetadata struct {
	// ModelID is the model id a model option launches with, when it differs
	// from the option's own text; empty otherwise.
	ModelID string
	// CreatedAt is a model's release date; empty when unknown.
	CreatedAt string
	// Auth is a profile's authentication state; nil for other segments.
	Auth *discover.Auth
}

// SegmentState holds one segment's option collections and the sets that
// decide how each option is drawn. The merged option list is cached and
// rebuilt after any change that can reorder it.
type SegmentState struct {
	discovered []string
	pinned     []string
	defaults   []string
	ephemeral  []string

	installed     map[string]bool
	authenticated map[string]bool
	managed       map[string]bool
	authActive    bool

	metadata map[string]ValueMetadata

	order []Source
	sort  OptionSort

	options      []string
	optionsValid bool
}

// NewSegmentState returns an empty state merging its collections in order
// and sorting with sortBy. The ephemeral collection may not appear in order:
// it always comes last.
func NewSegmentState(order []Source, sortBy OptionSort) (*SegmentState, error) {
	for _, s := range order {
		switch s {
		case SourcePinned, SourceDiscovered, SourceDefaults:
		default:
			return nil, fmt.Errorf("collection %q cannot be ordered", s)
		}
	}
	return &SegmentState{
		installed:     map[string]bool{},
		authenticated: map[string]bool{},
		managed:       map[string]bool{},
		metadata:      map[string]ValueMetadata{},
		order:         slices.Clone(order),
		sort:          sortBy,
	}, nil
}

func (s *SegmentState) invalidate() { s.optionsValid = false }

func (s *SegmentState) collection(src Source) []string {
	switch src {
	case SourcePinned:
		return s.pinned
	case SourceDiscovered:
		return s.discovered
	case SourceDefaults:
		return s.defaults
	}
	return nil
}

// Options returns the merged option list: the collections in the segment's
// order without repeats, then sorted, then the ephemeral options not
// already listed. The caller must not change the returned slice.
func (s *SegmentState) Options() []string {
	if s.optionsValid {
		return s.options
	}
	var ordered []string
	for _, src := range s.order {
		ordered = append(ordered, s.collection(src)...)
	}
	merged := dedupe(ordered)
	switch s.sort {
	case SortVersionDesc:
		slices.SortStableFunc(merged, func(a, b string) int { return install.CompareVersions(b, a) })
	case SortReleaseDateDesc:
		merged = s.sortedByReleaseDate(merged)
	}
	s.options = dedupe(append(merged, s.ephemeral...))
	s.optionsValid = true
	return s.options
}

// releaseDate is the release date recorded for value, or "" when unknown. A
// Context1MSuffix entry takes its base model's date, and its own only when
// the base has none.
func (s *SegmentState) releaseDate(value string) string {
	for _, name := range []string{discover.BaseModel(value), value} {
		if m, ok := s.metadata[name]; ok && m.CreatedAt != "" {
			return m.CreatedAt
		}
	}
	return ""
}

// sortedByReleaseDate orders values newest release first. Pinned values
// keep their order at the front; values with no recorded date follow the
// dated ones in their merged order. A base model and its Context1MSuffix
// entry move together, the suffixed entry right after its base, so two
// bases sharing a date cannot separate a base from its own entry; bases
// sharing a date keep their merged order.
func (s *SegmentState) sortedByReleaseDate(values []string) []string {
	var pinnedPart, dated, undated []string
	for _, v := range values {
		switch {
		case slices.Contains(s.pinned, v):
			pinnedPart = append(pinnedPart, v)
		case s.releaseDate(v) != "":
			dated = append(dated, v)
		default:
			undated = append(undated, v)
		}
	}
	dateSet := map[string]bool{}
	for _, v := range dated {
		dateSet[s.releaseDate(v)] = true
	}
	dates := slices.Collect(maps.Keys(dateSet))
	sort.Sort(sort.Reverse(sort.StringSlice(dates)))
	dateRank := make(map[string]int, len(dates))
	for rank, d := range dates {
		dateRank[d] = rank
	}
	groupPosition := map[string]int{}
	for i, v := range dated {
		base := discover.BaseModel(v)
		if _, seen := groupPosition[base]; !seen {
			groupPosition[base] = i
		}
	}
	suffixed := func(v string) int {
		if discover.BaseModel(v) != v {
			return 1
		}
		return 0
	}
	slices.SortStableFunc(dated, func(a, b string) int {
		if c := dateRank[s.releaseDate(a)] - dateRank[s.releaseDate(b)]; c != 0 {
			return c
		}
		if c := groupPosition[discover.BaseModel(a)] - groupPosition[discover.BaseModel(b)]; c != 0 {
			return c
		}
		return suffixed(a) - suffixed(b)
	})
	out := make([]string, 0, len(values))
	out = append(out, pinnedPart...)
	out = append(out, dated...)
	return append(out, undated...)
}

// SetDiscovered replaces the discovered options with values. With verify
// set, an old value values no longer lists is kept (after the new ones)
// when verify says it still exists; without it, the new list replaces the
// old one outright.
func (s *SegmentState) SetDiscovered(values []string, verify func(string) (bool, error)) error {
	next := slices.Clone(values)
	if verify != nil {
		for _, old := range s.discovered {
			if slices.Contains(values, old) {
				continue
			}
			keep, err := verify(old)
			if err != nil {
				return err
			}
			if keep {
				next = append(next, old)
			}
		}
	}
	s.discovered = next
	s.invalidate()
	return nil
}

// AddPinned pins value unless it already is.
func (s *SegmentState) AddPinned(value string) {
	if !slices.Contains(s.pinned, value) {
		s.pinned = append(s.pinned, value)
		s.invalidate()
	}
}

// RemovePinned unpins value; a value that is not pinned is left alone.
func (s *SegmentState) RemovePinned(value string) {
	if i := slices.Index(s.pinned, value); i >= 0 {
		s.pinned = slices.Delete(s.pinned, i, i+1)
		s.invalidate()
	}
}

// SetDefaults replaces the defaults collection.
func (s *SegmentState) SetDefaults(values []string) {
	s.defaults = slices.Clone(values)
	s.invalidate()
}

// AddEphemeral adds a launch-only option unless it is already one.
func (s *SegmentState) AddEphemeral(value string) {
	if !slices.Contains(s.ephemeral, value) {
		s.ephemeral = append(s.ephemeral, value)
		s.invalidate()
	}
}

// SetInstalled replaces the installed set. Installation decides how an
// option is drawn, not where, so the option list stays as it is.
func (s *SegmentState) SetInstalled(values map[string]bool) {
	s.installed = maps.Clone(values)
}

// HasInstalled reports whether any value is marked installed: only then
// does a value outside the set count as not installed.
func (s *SegmentState) HasInstalled() bool { return len(s.installed) > 0 }

// MarkInstalled marks one value installed.
func (s *SegmentState) MarkInstalled(value string) { s.installed[value] = true }

// IsInstalled reports whether value is in the installed set.
func (s *SegmentState) IsInstalled(value string) bool { return s.installed[value] }

// NotInstalled reports whether value is drawn and launched as a version
// that has to be installed first.
func (s *SegmentState) NotInstalled(value string) bool {
	return s.HasInstalled() && !s.IsInstalled(value)
}

// SetMetadata replaces every value's metadata. Metadata can reorder the
// options (the release-date sort reads it).
func (s *SegmentState) SetMetadata(meta map[string]ValueMetadata) {
	s.metadata = maps.Clone(meta)
	s.invalidate()
}

// UpdateMetadata replaces the metadata of each value in partial, leaving
// other values' metadata alone.
func (s *SegmentState) UpdateMetadata(partial map[string]ValueMetadata) {
	maps.Copy(s.metadata, partial)
	s.invalidate()
}

// DeleteMetadata drops value's metadata.
func (s *SegmentState) DeleteMetadata(value string) {
	delete(s.metadata, value)
	s.invalidate()
}

// Metadata returns a copy of every value's metadata.
func (s *SegmentState) Metadata() map[string]ValueMetadata {
	return maps.Clone(s.metadata)
}

// SourceOf returns the collection holding value, in the order pinned,
// discovered, defaults, ephemeral, and false when none does.
func (s *SegmentState) SourceOf(value string) (Source, bool) {
	for _, src := range []Source{SourcePinned, SourceDiscovered, SourceDefaults} {
		if slices.Contains(s.collection(src), value) {
			return src, true
		}
	}
	if slices.Contains(s.ephemeral, value) {
		return SourceEphemeral, true
	}
	return "", false
}

// SetAuthenticated records the authenticated values and turns on
// authentication tracking: from then on a value outside the set (and not
// managed) is drawn as unauthenticated.
func (s *SegmentState) SetAuthenticated(values map[string]bool) {
	s.authenticated = maps.Clone(values)
	s.authActive = true
	s.invalidate()
}

// SetManaged records the values whose authentication Claude Code owns (the
// default profile, ~/.claude): neither authenticated nor unauthenticated.
func (s *SegmentState) SetManaged(values map[string]bool) {
	s.managed = maps.Clone(values)
}

// HasAuthStatus reports whether authentication tracking is on.
func (s *SegmentState) HasAuthStatus() bool { return s.authActive }

// IsAuthenticated reports whether value is in the authenticated set.
func (s *SegmentState) IsAuthenticated(value string) bool { return s.authenticated[value] }

// IsManaged reports whether value is externally managed.
func (s *SegmentState) IsManaged(value string) bool { return s.managed[value] }

// Unauthenticated reports whether value is drawn as a profile that needs
// authenticating: tracking is on and value is neither authenticated nor
// managed.
func (s *SegmentState) Unauthenticated(value string) bool {
	return s.authActive && !s.authenticated[value] && !s.managed[value]
}

// updateAuthFromMetadata derives the authenticated and managed sets from
// the metadata. A managed value goes in the managed set only; any other
// value with authentication metadata is authenticated when it has a token
// or credentials. Tracking turns on only when some value carries
// authentication metadata, so segments without it never show it.
func (s *SegmentState) updateAuthFromMetadata() {
	found := false
	authenticated := map[string]bool{}
	managed := map[string]bool{}
	for name, m := range s.metadata {
		if m.Auth == nil {
			continue
		}
		found = true
		if m.Auth.Managed {
			managed[name] = true
			continue
		}
		if m.Auth.HasToken || m.Auth.HasCredentials {
			authenticated[name] = true
		}
	}
	if found {
		s.SetAuthenticated(authenticated)
		s.SetManaged(managed)
	}
}

// applyResult writes a discovery result into the state: the discovered
// options (verified with verify, when set), the installed set when the
// result has one, the metadata it found, and the authentication sets.
func (s *SegmentState) applyResult(r discover.Result, verify func(string) (bool, error)) error {
	if err := s.SetDiscovered(r.Values, verify); err != nil {
		return err
	}
	if len(r.Installed) > 0 {
		s.SetInstalled(r.Installed)
	}
	if len(r.Metadata) > 0 {
		partial := make(map[string]ValueMetadata, len(r.Metadata))
		for value, m := range r.Metadata {
			var auth *discover.Auth
			if m.Auth != nil {
				a := *m.Auth
				auth = &a
			}
			partial[value] = ValueMetadata{CreatedAt: m.CreatedAt, Auth: auth}
		}
		s.UpdateMetadata(partial)
	}
	s.updateAuthFromMetadata()
	return nil
}

// Segment is one segment of the bar: its options, its selection, and what
// is being typed into it.
type Segment struct {
	Key   string
	Label string
	State *SegmentState

	// The selection: a display option (PlusEntry included), or none.
	selected    string
	hasSelected bool

	// SearchBuffer is the text typed into a searchable segment, or the
	// value being edited in a freeform segment.
	SearchBuffer string

	ShowOptions bool
	Wrap        bool
	MinWidth    int
	MaxWidth    int
	Required    bool
	Searchable  bool
	TabAdvances bool
	// Creatable segments offer PlusEntry, which creates an option.
	Creatable bool
	// Freeform segments take typed text as a value of its own.
	Freeform bool

	// OptionRequires are the option's requirements on other segments.
	OptionRequires discover.Requires
	// Unavailable holds the options whose requirements the other segments'
	// selections do not meet; recomputed before every draw.
	Unavailable map[string]bool
	// Rejected maps an option the chosen client does not take to the
	// client's name.
	Rejected map[string]string

	// freeformEditing is set while the search buffer holds an edit of the
	// selected value rather than a search.
	freeformEditing bool
	// Creating is set while the user types a new option (CreateBuffer).
	Creating     bool
	CreateBuffer string
	// HasPending is set while background discovery results for the segment
	// are held back because it has the focus.
	HasPending bool
}

// Options returns the segment's option list.
func (s *Segment) Options() []string { return s.State.Options() }

// DisplayOptions returns the options plus PlusEntry on a creatable segment.
func (s *Segment) DisplayOptions() []string {
	opts := s.Options()
	if s.Creatable {
		return append(slices.Clone(opts), PlusEntry)
	}
	return opts
}

// Selected returns the selection, and false when nothing is selected.
func (s *Segment) Selected() (string, bool) { return s.selected, s.hasSelected }

// ClearSelection selects nothing.
func (s *Segment) ClearSelection() { s.selected, s.hasSelected = "", false }

// SelectedIndex returns the selection's index in DisplayOptions, or -1.
func (s *Segment) SelectedIndex() int {
	if !s.hasSelected {
		return -1
	}
	return slices.Index(s.DisplayOptions(), s.selected)
}

// Value returns the selection when it is one of the options (not
// PlusEntry, and still listed), and false otherwise.
func (s *Segment) Value() (string, bool) {
	if !s.hasSelected || s.selected == PlusEntry {
		return "", false
	}
	if !slices.Contains(s.Options(), s.selected) {
		return "", false
	}
	return s.selected, true
}

// FilteredOptions returns the options matching the search buffer, best
// first, or every option when nothing is typed. PlusEntry never matches.
func (s *Segment) FilteredOptions() []string {
	if s.SearchBuffer == "" {
		return s.Options()
	}
	return widgets.FuzzyRank(s.SearchBuffer, s.Options())
}

// Cycle moves the selection down (+1) or up (-1) through the display
// options. The positions form a ring of the options plus "nothing
// selected". With Wrap the ring turns endlessly; without it, moving off
// either end selects nothing, and moving on from nothing stays there.
func (s *Segment) Cycle(direction int) {
	opts := s.DisplayOptions()
	n := len(opts)
	if n == 0 {
		return
	}
	pos := s.SelectedIndex() + 1 + direction
	if s.Wrap {
		pos = ((pos % (n + 1)) + n + 1) % (n + 1)
	} else if pos < 0 || pos > n {
		pos = 0
	}
	if pos == 0 {
		s.ClearSelection()
		return
	}
	s.selected, s.hasSelected = opts[pos-1], true
}

// IsOnPlus reports whether a creatable segment has PlusEntry selected.
func (s *Segment) IsOnPlus() bool {
	return s.Creatable && s.hasSelected && s.selected == PlusEntry
}

// SelectValue selects value when it is one of the options, reporting
// whether it was.
func (s *Segment) SelectValue(value string) bool {
	if slices.Contains(s.Options(), value) {
		s.selected, s.hasSelected = value, true
		return true
	}
	return false
}

// Bar is the ordered segments with the focus.
type Bar struct {
	Segments []*Segment
	Focus    int
}

// NewBar returns a bar over segments, focused on the first. A bar without
// segments is an error.
func NewBar(segments []*Segment) (*Bar, error) {
	if len(segments) == 0 {
		return nil, fmt.Errorf("no segments enabled -- check enabled_segments in config.json")
	}
	return &Bar{Segments: segments}, nil
}

// Focused returns the focused segment.
func (b *Bar) Focused() *Segment { return b.Segments[b.Focus] }

// Segment returns the segment key, and false when the bar has none.
func (b *Bar) Segment(key string) (*Segment, bool) {
	for _, s := range b.Segments {
		if s.Key == key {
			return s, true
		}
	}
	return nil, false
}

// MoveFocus moves the focus left (-1) or right (+1), wrapping around.
func (b *Bar) MoveFocus(direction int) {
	n := len(b.Segments)
	b.Focus = ((b.Focus+direction)%n + n) % n
}

// RemoveSegment takes segment key off the bar; the focus moves to the first
// segment when it was past the end. Removing the last segment is an error.
func (b *Bar) RemoveSegment(key string) error {
	i := slices.IndexFunc(b.Segments, func(s *Segment) bool { return s.Key == key })
	if i < 0 {
		return nil
	}
	if len(b.Segments) == 1 {
		return fmt.Errorf("removing segment %q would leave the bar empty", key)
	}
	b.Segments = slices.Delete(b.Segments, i, i+1)
	if b.Focus >= len(b.Segments) {
		b.Focus = 0
	}
	return nil
}

// Selections maps each segment that has a value to it.
func (b *Bar) Selections() map[string]string {
	out := map[string]string{}
	for _, s := range b.Segments {
		if v, ok := s.Value(); ok {
			out[s.Key] = v
		}
	}
	return out
}

// dedupe drops repeats, keeping first occurrences in order.
func dedupe(values []string) []string {
	seen := make(map[string]bool, len(values))
	out := make([]string, 0, len(values))
	for _, v := range values {
		if !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	return out
}
