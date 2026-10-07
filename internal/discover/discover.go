// Package discover finds the options each segment of the launch bar offers:
// installed and published Claude Code versions, the models the Anthropic API
// serves, profiles, GitHub accounts, project directories, and values kept in
// state.json. It also holds the cross-segment constraints that mark an option
// unavailable given the other segments' selections.
//
// Discovery reads state.json's copy in memory and never changes it: what a
// discovery wants written back (a refreshed cache, pruned recent
// directories) comes back on its Result, and ApplyToState writes it into a
// State the caller then saves. The segment and bar state that consume the
// results are the TUI's.
package discover

import (
	"fmt"
	"slices"
	"sort"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/profiles"
)

// The discovery types an options.json segment's discovery declaration names.
const (
	// TypeDirectoryListing lists the files in a directory (installed
	// versions), newest version first.
	TypeDirectoryListing = "directory_listing"
	// TypeNpmAndLocal lists the versions npm publishes plus the installed
	// ones, newest first.
	TypeNpmAndLocal = "npm_and_local"
	// TypeAnthropicModels lists the models the Anthropic models endpoint
	// serves, with their release dates.
	TypeAnthropicModels = "anthropic_models"
	// TypeDirectoryScan lists the recent directories that still exist, then
	// the subdirectories of the declared parents.
	TypeDirectoryScan = "directory_scan"
	// TypeClaudeConfigScan lists the profiles with their authentication
	// state.
	TypeClaudeConfigScan = "claude_config_scan"
	// TypeGhAuth lists the segment's own values plus the accounts gh is
	// logged in to.
	TypeGhAuth = "gh_auth"
	// TypeStateField lists a list kept in state.json, then the segment's own
	// values.
	TypeStateField = "state_field"
)

// source is one discovery type's behavior.
type source struct {
	// run is the full discovery.
	run func(env Env, seg appconfig.OptionSegment) (Result, error)
	// slow marks a discovery that may wait on the network or a subprocess;
	// the bar runs it in the background.
	slow bool
	// warm is a slow discovery's startup counterpart: it reads only what is
	// already on hand (a fresh cache, the local filesystem) and never waits.
	// A slow discovery without one gives nothing at startup.
	warm func(env Env, seg appconfig.OptionSegment) (Result, error)
	// verify, when set, checks a value a new discovery no longer lists
	// before it is dropped: a value that still exists is kept.
	verify func(env Env, d appconfig.Discovery) (func(string) (bool, error), error)
}

// sources maps each discovery type to its behavior; built per call, so no
// caller can change it.
func sources() map[string]source {
	return map[string]source{
		TypeDirectoryListing: {run: directoryListing, verify: verifyFileIn},
		TypeNpmAndLocal:      {run: npmAndLocal, slow: true, warm: npmAndLocalWarm, verify: verifyFileIn},
		TypeAnthropicModels:  {run: anthropicModels, slow: true, warm: anthropicModelsWarm},
		TypeDirectoryScan:    {run: directoryScan, verify: verifyDirectory},
		TypeClaudeConfigScan: {run: profileScan},
		TypeGhAuth:           {run: ghAccounts, slow: true},
		TypeStateField:       {run: stateField},
	}
}

// Types returns every discovery type, sorted.
func Types() []string {
	all := sources()
	names := make([]string, 0, len(all))
	for name := range all {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// lookup returns the source of a declaration's type; an unknown type is an
// error listing the known ones.
func lookup(d appconfig.Discovery) (source, error) {
	src, ok := sources()[d.Type]
	if !ok {
		return source{}, fmt.Errorf("unknown discovery type %q (known: %v)", d.Type, Types())
	}
	return src, nil
}

// Env is what every discovery reads.
type Env struct {
	// FX runs the subprocesses (npm, gh) and network requests discovery
	// reads with; they are reads, so they run in every mode.
	FX *effects.FX
	// Home is the user's home directory: "~" in declarations and values
	// expands to it, and directories under it are listed as "~/…".
	Home string
	// Now gives the current time, for cache freshness and fetch times.
	Now func() time.Time
	// State is state.json as the caller holds it. Discovery only reads it;
	// a discovery run on another goroutine needs a State nobody changes
	// while it runs (a copy).
	State appconfig.State
	// Profiles is the profile store, enumerated by profile discovery and
	// for the tokens model discovery lists models with.
	Profiles profiles.Store
}

// check refuses an Env missing a field every discovery may need.
func (e Env) check() error {
	if e.FX == nil {
		return fmt.Errorf("discover: Env.FX is required")
	}
	if e.Home == "" {
		return fmt.Errorf("discover: Env.Home is required")
	}
	if e.Now == nil {
		return fmt.Errorf("discover: Env.Now is required")
	}
	return nil
}

// Metadata describes one discovered value.
type Metadata struct {
	// CreatedAt is a model's release date as the models endpoint gives it;
	// empty when unknown.
	CreatedAt string
	// Auth is a profile's authentication state; nil for values of every
	// other segment.
	Auth *Auth
}

// Auth is a profile's authentication state as profile discovery finds it.
type Auth struct {
	HasToken       bool
	HasCredentials bool
	// Managed marks the default profile (~/.claude): Claude Code owns its
	// authentication, so it counts as neither authenticated nor not.
	Managed bool
}

// Result is what one discovery found.
type Result struct {
	// Values are the discovered options in the order the segment offers
	// them before its own ordering applies.
	Values []string
	// Installed holds the values installed on this machine; empty when the
	// discovery does not tell.
	Installed map[string]bool
	// Metadata describes values, keyed by value; empty when the discovery
	// gives none.
	Metadata map[string]Metadata
	// RefreshError is set when a network or subprocess refresh failed and
	// Values came from a stale cache, or from the segment's own values
	// alone. It is the failure, for the caller to show; the result is still
	// usable.
	RefreshError error
	// NpmCache and ModelCache, when set, are refreshed caches to store in
	// state.json; RecentDirs, when set, is the recent directories with the
	// ones that no longer exist removed. ApplyToState writes them.
	NpmCache   *appconfig.NpmVersionsCache
	ModelCache *appconfig.ModelListCache
	RecentDirs *[]string
}

// ApplyToState writes what the result wants kept in state.json into st.
func (r Result) ApplyToState(st *appconfig.State) {
	if r.NpmCache != nil {
		c := *r.NpmCache
		c.Versions = slices.Clone(c.Versions)
		st.NpmVersionsCache = &c
	}
	if r.ModelCache != nil {
		c := *r.ModelCache
		c.Models = slices.Clone(c.Models)
		st.ModelListCache = &c
	}
	if r.RecentDirs != nil {
		st.RecentDirs = slices.Clone(*r.RecentDirs)
	}
}

// ModelRecord returns the result's release dates in the form options.json
// records discovered models in (appconfig's RecordDiscoveredModels).
func (r Result) ModelRecord() map[string]appconfig.ValueMetadata {
	out := map[string]appconfig.ValueMetadata{}
	for value, m := range r.Metadata {
		if m.CreatedAt == "" {
			continue
		}
		created := m.CreatedAt
		out[value] = appconfig.ValueMetadata{CreatedAt: &created}
	}
	return out
}

// declaration returns seg's discovery declaration; a segment without one is
// an error.
func declaration(seg appconfig.OptionSegment) (appconfig.Discovery, error) {
	if seg.Discovery == nil {
		return appconfig.Discovery{}, fmt.Errorf("the segment declares no discovery")
	}
	return *seg.Discovery, nil
}

// Run runs seg's discovery in full, network and subprocesses included.
func Run(env Env, seg appconfig.OptionSegment) (Result, error) {
	if err := env.check(); err != nil {
		return Result{}, err
	}
	d, err := declaration(seg)
	if err != nil {
		return Result{}, err
	}
	src, err := lookup(d)
	if err != nil {
		return Result{}, err
	}
	return src.run(env, seg)
}

// Startup runs what the bar shows when it first draws: a slow discovery's
// startup counterpart, or a fast discovery in full. It reports false when
// there is nothing to show yet: the segment declares no discovery, or its
// discovery is slow with no startup counterpart (the background run brings
// it).
func Startup(env Env, seg appconfig.OptionSegment) (Result, bool, error) {
	if seg.Discovery == nil {
		return Result{}, false, nil
	}
	if err := env.check(); err != nil {
		return Result{}, false, err
	}
	src, err := lookup(*seg.Discovery)
	if err != nil {
		return Result{}, false, err
	}
	if !src.slow {
		r, err := src.run(env, seg)
		return r, err == nil, err
	}
	if src.warm == nil {
		return Result{}, false, nil
	}
	r, err := src.warm(env, seg)
	return r, err == nil, err
}

// IsSlow reports whether discovery type t is run in the background.
func IsSlow(t string) (bool, error) {
	src, err := lookup(appconfig.Discovery{Type: t})
	if err != nil {
		return false, err
	}
	return src.slow, nil
}

// RunSlow runs every slow discovery declared in opts, keyed by segment key.
// It is the bar's background refresh; segments are visited in key order.
func RunSlow(env Env, opts appconfig.Options) (map[string]Result, error) {
	if err := env.check(); err != nil {
		return nil, err
	}
	keys := make([]string, 0, len(opts))
	for key := range opts {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	results := map[string]Result{}
	for _, key := range keys {
		seg := opts[key]
		if seg.Discovery == nil {
			continue
		}
		src, err := lookup(*seg.Discovery)
		if err != nil {
			return nil, fmt.Errorf("segment %s: %w", key, err)
		}
		if !src.slow {
			continue
		}
		r, err := src.run(env, seg)
		if err != nil {
			return nil, fmt.Errorf("discovering the %s segment's options: %w", key, err)
		}
		results[key] = r
	}
	return results, nil
}

// Verifier returns the check a value that a new discovery no longer lists
// must pass to be kept (it still exists on disk), or nil when d's type
// drops such values at once.
func Verifier(env Env, d appconfig.Discovery) (func(string) (bool, error), error) {
	src, err := lookup(d)
	if err != nil {
		return nil, err
	}
	if src.verify == nil {
		return nil, nil
	}
	if env.Home == "" {
		return nil, fmt.Errorf("discover: Env.Home is required")
	}
	return src.verify(env, d)
}
