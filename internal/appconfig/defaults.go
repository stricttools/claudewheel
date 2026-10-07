package appconfig

// The default contents of claudewheel's own files. Each function builds a
// fresh value, so a caller changing what it gets back cannot change the
// defaults.

// DefaultConfig returns the config.json a new workspace starts with.
func DefaultConfig() Config {
	return Config{
		Theme: ThemeAuto,
		EnabledSegments: []string{
			SegmentKeyProfile,
			SegmentKeyGitHub,
			SegmentKeyVersion,
			SegmentKeyModel,
			SegmentKeyDirectory,
			SegmentKeyMCP,
			SegmentKeyPermissions,
		},
		DefaultFlags:        []string{"--dangerously-skip-permissions"},
		HealthCheckOnLaunch: true,
		Minimap:             "auto",
		// Must name a client adapter; an unknown value is a hard error at launch.
		DefaultClient: "claude",
		// The memory cap a launched session's Bash commands, and everything
		// they start, share; Claude Code itself is not capped. A whole number
		// with a K, M, G, or T suffix, and for swap also "0".
		ToolMemoryMax:     "6G",
		ToolMemorySwapMax: "1G",
	}
}

func boolPtr(b bool) *bool { return &b }

func strPtr(s string) *string { return &s }

func intPtr(i int) *int { return &i }

// DefaultSegments returns the segments.json a new workspace starts with, in
// bar order.
func DefaultSegments() []Segment {
	return []Segment{
		{Key: SegmentKeyProfile, Label: "Profile", ShowOptions: true, Wrap: true, MinWidth: 8, MaxWidth: 16, Required: true, PrintMode: true, Searchable: false, TabAdvances: true, Creatable: boolPtr(true)},
		{Key: SegmentKeyGitHub, Label: "GH", ShowOptions: true, Wrap: true, MinWidth: 4, MaxWidth: 12, Required: false, PrintMode: false, Searchable: false, TabAdvances: true, Creatable: boolPtr(true)},
		{Key: SegmentKeyVersion, Label: "Ver", ShowOptions: true, Wrap: true, MinWidth: 6, MaxWidth: 10, Required: true, PrintMode: true, Searchable: false, TabAdvances: true},
		{Key: SegmentKeyModel, Label: "Model", ShowOptions: true, Wrap: true, MinWidth: 10, MaxWidth: 24, Required: false, PrintMode: true, Searchable: true, TabAdvances: true, Creatable: boolPtr(true)},
		{Key: SegmentKeyDirectory, Label: "Dir", ShowOptions: true, Wrap: false, MinWidth: 10, MaxWidth: 40, Required: true, PrintMode: true, Searchable: true, Freeform: boolPtr(true), TabAdvances: true},
		{Key: SegmentKeyMCP, Label: "MCP", ShowOptions: true, Wrap: true, MinWidth: 6, MaxWidth: 12, Required: false, PrintMode: false, Searchable: false, TabAdvances: true},
		{Key: SegmentKeyPermissions, Label: "Perms", ShowOptions: true, Wrap: true, MinWidth: 6, MaxWidth: 12, Required: false, PrintMode: false, Searchable: false, TabAdvances: true},
	}
}

// DefaultOptions returns the options.json a new workspace starts with, and
// the one `reset-options` writes.
func DefaultOptions() Options {
	return Options{
		SegmentKeyProfile: {
			Values: []string{},
			Pinned: []string{},
			Discovery: &Discovery{
				Type:    "claude_config_scan",
				BaseDir: strPtr("~"),
			},
		},
		SegmentKeyGitHub: {
			Values:    []string{},
			Pinned:    []string{},
			Discovery: &Discovery{Type: "gh_auth"},
		},
		SegmentKeyVersion: {
			Values: []string{},
			Pinned: []string{},
			Discovery: &Discovery{
				Type:  "npm_and_local",
				Path:  strPtr("~/.local/share/claude/versions"),
				Count: intPtr(15),
			},
		},
		SegmentKeyDirectory: {
			Values: []string{},
			Pinned: []string{},
			Discovery: &Discovery{
				Type: "directory_scan",
				Parents: &[]string{
					"~/Projects",
					"~/repos",
					"~/src",
					"~/code",
					"~/dev",
					"~/Work",
					"~/work",
				},
				StateField: strPtr("recent_dirs"),
			},
		},
		SegmentKeyModel: {
			// The first-run seed. options.json then accumulates every model
			// the Anthropic models endpoint reports, each with its release
			// date, and never drops one; a model shipped after this list was
			// written arrives through that discovery.
			//
			// The [1m] suffix selects the 1M token context window on models
			// with a 200K default. Fable 5 has no such entry: it always runs
			// at 1M, and the client discards the suffix on it.
			Values: []string{
				"claude-opus-5",
				"claude-opus-4-8",
				"claude-opus-4-8[1m]",
				"claude-fable-5",
				"claude-sonnet-5",
				"claude-opus-4-7",
				"claude-opus-4-7[1m]",
				"claude-opus-4-6",
				"claude-opus-4-6[1m]",
				"claude-sonnet-4-6",
				"claude-sonnet-4-6[1m]",
				"claude-haiku-4-5-20251001",
				"claude-sonnet-4-5-20241022",
			},
			Pinned:    []string{},
			Discovery: &Discovery{Type: "anthropic_models"},
		},
		SegmentKeyMCP: {
			Values: []string{"default", "strict"},
			Pinned: []string{},
		},
		SegmentKeyPermissions: {
			// The bar offers this list from the code, not from options.json,
			// so a value added here is offered in every workspace.
			Values: []string{"bypass", "default", "plan", "auto"},
			Pinned: []string{},
		},
	}
}

// DefaultState returns the state.json a new workspace starts with.
func DefaultState() State {
	return State{
		LastConfig:  map[string]string{},
		RecentDirs:  []string{},
		LaunchCount: 0,
	}
}

// ModelMinCLIVersion returns the minimum Claude Code version that can run
// each listed model; a model not listed runs on any installed binary. The
// launch preflight's abort and the model picker's dimming both read it, and
// both strip a trailing context-window suffix before the lookup.
func ModelMinCLIVersion() map[string]string {
	return map[string]string{
		"claude-opus-5": "2.1.219",
	}
}
