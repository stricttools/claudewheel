package profiles

import (
	"fmt"
	"slices"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/tokens"
)

// The variables a launch gives Claude Code for a named profile.
const (
	ConfigDirVar  = "CLAUDE_CONFIG_DIR"
	OAuthTokenVar = "CLAUDE_CODE_OAUTH_TOKEN"
	// MarketplaceAutoinstallVar stops Claude Code cloning the official
	// plugin marketplace into a profile on first launch. No settings key
	// does this (the marketplace keys are managed-policy only), and once the
	// client recorded the install as blocked it never retries, so the
	// suppression is one-way per profile. Undocumented client surface.
	MarketplaceAutoinstallVar = "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"
	// TerminalTitleVar stops the client generating a session title (which
	// it would promote into the session name on the prompt bar) and writing
	// the terminal title. Names set with --name or /rename are unaffected.
	// Undocumented client surface.
	TerminalTitleVar = "CLAUDE_CODE_DISABLE_TERMINAL_TITLE"
	// AutoupdaterVar stops the client updating itself over the versions
	// directory claudewheel owns; the autoUpdates setting is overridden on a
	// native install. Undocumented client surface.
	AutoupdaterVar = "DISABLE_AUTOUPDATER"
	// GrowthbookVar turns off feature-flag evaluation, the only switch for
	// the startup model-upsell tip; it also disables Remote Control, which
	// is wanted. Undocumented client surface.
	GrowthbookVar = "DISABLE_GROWTHBOOK"
)

// switchOn is the value of every off-switch variable: the plainest of the
// truthy strings the client accepts.
const switchOn = "1"

// ProfileEnvKeys returns every variable LaunchEnv can set: a named profile's
// launch sets them, and a launch of the default profile removes them all, so
// an ambient value never stands in for an injection claudewheel declined.
func ProfileEnvKeys() []string {
	return []string{
		ConfigDirVar,
		OAuthTokenVar,
		tokens.SubscriptionEnvVar,
		tokens.RateLimitEnvVar,
		MarketplaceAutoinstallVar,
		TerminalTitleVar,
		AutoupdaterVar,
		GrowthbookVar,
	}
}

// LaunchEnv is the environment change a launch of one profile applies to
// the environment it inherits.
type LaunchEnv struct {
	// Set holds the variables set, the OAuth token among them when the
	// profile stores one.
	Set map[string]string
	// Unset lists the variables removed.
	Unset []string
	// Token is the OAuth token Set carries, empty when none: the value
	// callers redact from every output, error, and dry-run record.
	Token string
}

// Apply returns environ (os.Environ form) with e applied: removed variables
// dropped, set variables replacing inherited ones, the set ones appended in
// name order.
func (e LaunchEnv) Apply(environ []string) []string {
	out := make([]string, 0, len(environ)+len(e.Set))
	for _, kv := range environ {
		key, _, _ := strings.Cut(kv, "=")
		if slices.Contains(e.Unset, key) {
			continue
		}
		if _, replaced := e.Set[key]; replaced {
			continue
		}
		out = append(out, kv)
	}
	keys := make([]string, 0, len(e.Set))
	for k := range e.Set {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		out = append(out, k+"="+e.Set[k])
	}
	return out
}

// UnknownProfileError is a profile name nothing answers to.
type UnknownProfileError struct {
	Name      string
	Available []string
}

func (e *UnknownProfileError) Error() string {
	list := "<none>"
	if len(e.Available) > 0 {
		list = strings.Join(e.Available, ", ")
	}
	return fmt.Sprintf("Profile '%s' not found. Available profiles: %s", e.Name, list)
}

// LaunchEnv resolves profile name to the environment change a launch and
// `profile exec` apply. The default profile is Claude Code's own ~/.claude:
// nothing is set and every ProfileEnvKeys variable is removed. A named
// profile sets its config directory, its stored OAuth token when there is
// one, its declared plan tier (validated: an unrecognized value is an
// error), and the off-switches. Only name's own token file is read, so a
// corrupt token file elsewhere cannot break this; an unknown name is an
// *UnknownProfileError listing the profiles.
func (s Store) LaunchEnv(name string) (LaunchEnv, error) {
	if err := s.CheckPendingRenames(); err != nil {
		return LaunchEnv{}, err
	}
	if name == DefaultName {
		return LaunchEnv{Set: map[string]string{}, Unset: ProfileEnvKeys()}, nil
	}
	_, found, err := s.recordFor(name)
	if err != nil {
		return LaunchEnv{}, err
	}
	if !found {
		names, err := s.Names()
		if err != nil {
			return LaunchEnv{}, err
		}
		return LaunchEnv{}, &UnknownProfileError{Name: name, Available: names}
	}
	env := LaunchEnv{Set: map[string]string{
		ConfigDirVar:              s.PathFor(name),
		MarketplaceAutoinstallVar: switchOn,
		TerminalTitleVar:          switchOn,
		AutoupdaterVar:            switchOn,
		GrowthbookVar:             switchOn,
	}}
	data := s.Data(name)
	entry, _, err := data.Load()
	if err != nil {
		return LaunchEnv{}, err
	}
	if token, ok := entry.TokenValue(); ok {
		env.Set[OAuthTokenVar] = token
		env.Token = token
	}
	plan, err := entry.PlanEnv(data.TokenFile())
	if err != nil {
		return LaunchEnv{}, err
	}
	for k, v := range plan {
		env.Set[k] = v
	}
	return env, nil
}
