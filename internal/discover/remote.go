package discover

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/auth"
	"github.com/stricttools/claudewheel/internal/effects"
)

// The npm version list and the model list are cached in state.json for an
// hour: a fresh cache skips the network, and a stale one is still used when
// a refresh fails.
const (
	NpmCacheTTL       = time.Hour
	ModelListCacheTTL = time.Hour
)

// NpmPackage is the npm package Claude Code is published as.
const NpmPackage = "@anthropic-ai/claude-code"

const (
	npmTimeout = 10 * time.Second
	ghTimeout  = 5 * time.Second
	// The models endpoint's page size is far larger than the list it
	// serves, so pagination is a safety net; the page ceiling stops a far
	// side that always answers has_more.
	modelPageLimit    = 100
	modelMaxPages     = 20
	modelFetchTimeout = 5 * time.Second
)

// ghAccountPattern finds the accounts in `gh auth status` output.
var ghAccountPattern = regexp.MustCompile(`Logged in to github\.com account (\S+)`)

// unixSeconds is t as state.json's fetch times hold it.
func unixSeconds(t time.Time) float64 {
	return float64(t.UnixNano()) / 1e9
}

// fresh reports whether a cache fetched at fetchedAt (Unix seconds) is
// younger than ttl.
func fresh(fetchedAt float64, now time.Time, ttl time.Duration) bool {
	return unixSeconds(now)-fetchedAt < ttl.Seconds()
}

// lastN returns a copy of the last n entries of list.
func lastN(list []string, n int) []string {
	if n < len(list) {
		list = list[len(list)-n:]
	}
	return append([]string{}, list...)
}

// versionCount returns the declaration's count, which must be positive.
func versionCount(d appconfig.Discovery) (int, error) {
	if d.Count == nil {
		return 0, fmt.Errorf("discovery type %s needs a count", d.Type)
	}
	if *d.Count <= 0 {
		return 0, fmt.Errorf("discovery type %s: count must be positive, not %d", d.Type, *d.Count)
	}
	return *d.Count, nil
}

// npmAndLocal lists the latest count versions npm publishes plus every
// installed version, newest first, refreshing the npm list when its cache
// is stale.
func npmAndLocal(env Env, seg appconfig.OptionSegment) (Result, error) {
	return versions(env, seg, true)
}

// npmAndLocalWarm is npmAndLocal from a fresh cache only: with a stale or
// empty cache it lists the installed versions alone.
func npmAndLocalWarm(env Env, seg appconfig.OptionSegment) (Result, error) {
	return versions(env, seg, false)
}

// versions is npmAndLocal; refresh says whether a stale cache is refreshed
// from npm.
func versions(env Env, seg appconfig.OptionSegment, refresh bool) (Result, error) {
	d, err := declaration(seg)
	if err != nil {
		return Result{}, err
	}
	dir, err := requiredPath(env, d)
	if err != nil {
		return Result{}, err
	}
	count, err := versionCount(d)
	if err != nil {
		return Result{}, err
	}
	local, err := filesIn(dir)
	if err != nil {
		return Result{}, err
	}
	installed := make(map[string]bool, len(local))
	for _, v := range local {
		installed[v] = true
	}

	var result Result
	var cached []string
	var fetchedAt float64
	if c := env.State.NpmVersionsCache; c != nil {
		cached, fetchedAt = c.Versions, c.FetchedAt
	}
	var published []string
	switch {
	case len(cached) > 0 && fresh(fetchedAt, env.Now(), NpmCacheTTL):
		published = lastN(cached, count)
	case !refresh:
		published = []string{}
	default:
		all, err := fetchNpmVersions(env.FX)
		if err != nil {
			result.RefreshError = err
			published = lastN(cached, count)
		} else {
			result.NpmCache = &appconfig.NpmVersionsCache{FetchedAt: unixSeconds(env.Now()), Versions: all}
			published = lastN(all, count)
		}
	}

	all := published
	for _, v := range local {
		if !slices.Contains(all, v) {
			all = append(all, v)
		}
	}
	sortNewestFirst(all)
	result.Values = all
	result.Installed = installed
	return result, nil
}

// fetchNpmVersions asks npm for every published Claude Code version, oldest
// first.
func fetchNpmVersions(fx *effects.FX) ([]string, error) {
	res, err := fx.Run(effects.Cmd{
		Argv:    []string{"npm", "view", NpmPackage, "versions", "--json"},
		Timeout: npmTimeout,
		Capture: true,
		Read:    true,
		// An empty stdin, so the child never reads the terminal's keys.
		Stdin: []byte{},
	})
	if err != nil {
		return nil, err
	}
	if res.ExitCode() != 0 {
		return nil, fmt.Errorf("npm view %s versions exited with status %d: %s", NpmPackage, res.ExitCode(), strings.TrimSpace(res.Stderr()))
	}
	var all []string
	if err := json.Unmarshal([]byte(res.Stdout()), &all); err != nil {
		return nil, fmt.Errorf("npm view %s versions: not a list of versions: %w", NpmPackage, err)
	}
	if all == nil {
		return nil, fmt.Errorf("npm view %s versions printed no list", NpmPackage)
	}
	return all, nil
}

// anthropicModels lists the models the Anthropic API serves, refreshing the
// cached list when it is stale.
func anthropicModels(env Env, _ appconfig.OptionSegment) (Result, error) {
	var cached []appconfig.CachedModel
	var fetchedAt float64
	if c := env.State.ModelListCache; c != nil {
		cached, fetchedAt = c.Models, c.FetchedAt
	}
	if len(cached) > 0 && fresh(fetchedAt, env.Now(), ModelListCacheTTL) {
		return modelsResult(cached), nil
	}
	models, failure := fetchAvailableModels(env)
	if failure != nil || models == nil {
		r := modelsResult(cached)
		r.RefreshError = failure
		return r, nil
	}
	r := modelsResult(models)
	r.ModelCache = &appconfig.ModelListCache{FetchedAt: unixSeconds(env.Now()), Models: models}
	return r, nil
}

// anthropicModelsWarm is the cached model list while it is fresh, and
// nothing otherwise: every model ever discovered is already in options.json,
// which the segment offers anyway.
func anthropicModelsWarm(env Env, _ appconfig.OptionSegment) (Result, error) {
	if c := env.State.ModelListCache; c != nil && len(c.Models) > 0 && fresh(c.FetchedAt, env.Now(), ModelListCacheTTL) {
		return modelsResult(c.Models), nil
	}
	return Result{Values: []string{}}, nil
}

// modelsResult turns a model list into values plus release dates.
func modelsResult(models []appconfig.CachedModel) Result {
	r := Result{Values: []string{}, Metadata: map[string]Metadata{}}
	for _, m := range models {
		if m.ID == "" {
			continue
		}
		r.Values = append(r.Values, m.ID)
		if m.CreatedAt != nil && *m.CreatedAt != "" {
			r.Metadata[m.ID] = Metadata{CreatedAt: *m.CreatedAt}
		}
	}
	return r
}

// tokenCandidates returns the profiles holding a token, the last launched
// one first and the rest in enumeration order.
func tokenCandidates(env Env) ([]string, error) {
	found, err := env.Profiles.Enumerate()
	if err != nil {
		return nil, err
	}
	var names []string
	for _, p := range found {
		if p.HasToken {
			names = append(names, p.Name)
		}
	}
	last := env.State.LastConfig[appconfig.SegmentKeyProfile]
	if i := slices.Index(names, last); i > 0 {
		names = append([]string{last}, slices.Delete(names, i, i+1)...)
	}
	return names, nil
}

// fetchAvailableModels lists the models with the first profile token that
// answers. A 401 (the token cannot list models) or a 429 (the account is
// rate limited) moves on to the next profile's token; any other failure ends
// the refresh, since the next token would fare the same. It returns nil
// models, and the failures joined, when no token answered; nil models and no
// error when no profile holds a token.
func fetchAvailableModels(env Env) ([]appconfig.CachedModel, error) {
	names, err := tokenCandidates(env)
	if err != nil {
		return nil, fmt.Errorf("listing profiles for a token: %w", err)
	}
	var failures []error
	for _, name := range names {
		token, ok, err := env.Profiles.Data(name).Token()
		if err != nil {
			failures = append(failures, fmt.Errorf("profile %s: %w", name, err))
			continue
		}
		if !ok || token == "" {
			continue
		}
		models, err := fetchModelsWithToken(env.FX, token)
		if err == nil {
			return models, nil
		}
		failures = append(failures, fmt.Errorf("listing models with profile %s's token: %w", name, err))
		var status *effects.StatusError
		if errors.As(err, &status) && (status.Status == 401 || status.Status == 429) {
			continue
		}
		break
	}
	return nil, errors.Join(failures...)
}

// modelsPage is one page of the models endpoint's answer. Items and the
// paging fields are checked one by one, as the endpoint's other fields are
// not claudewheel's to require.
type modelsPage struct {
	Data    []json.RawMessage `json:"data"`
	HasMore any               `json:"has_more"`
	LastID  any               `json:"last_id"`
}

// modelsPageURL is the endpoint URL for the page after afterID ("" for the
// first page).
func modelsPageURL(afterID string) string {
	u := fmt.Sprintf("%s?limit=%d", auth.ModelsEndpoint, modelPageLimit)
	if afterID != "" {
		u += "&after_id=" + url.QueryEscape(afterID)
	}
	return u
}

// fetchModelsWithToken reads the whole model list with one token, following
// pagination. An item that is not an object or has no id is skipped.
func fetchModelsWithToken(fx *effects.FX, token string) ([]appconfig.CachedModel, error) {
	models := []appconfig.CachedModel{}
	afterID := ""
	for page := 0; page < modelMaxPages; page++ {
		resp, err := fx.HTTPRead(effects.Request{
			Method: "GET",
			URL:    modelsPageURL(afterID),
			Header: map[string]string{
				"Authorization":     "Bearer " + token,
				"anthropic-version": auth.AnthropicVersion,
			},
			Timeout: modelFetchTimeout,
			Redact:  []string{token},
		})
		if err != nil {
			return nil, err
		}
		var p modelsPage
		if err := json.Unmarshal(resp.Body, &p); err != nil {
			return nil, fmt.Errorf("the models endpoint answered something other than a model list: %w", err)
		}
		for _, raw := range p.Data {
			var item map[string]any
			if json.Unmarshal(raw, &item) != nil || item == nil {
				continue
			}
			id, _ := item["id"].(string)
			if id == "" {
				continue
			}
			m := appconfig.CachedModel{ID: id}
			if created, _ := item["created_at"].(string); created != "" {
				m.CreatedAt = &created
			}
			models = append(models, m)
		}
		hasMore, _ := p.HasMore.(bool)
		lastID, _ := p.LastID.(string)
		// A far side that does not advance would repeat the same page.
		if !hasMore || lastID == "" || lastID == afterID {
			break
		}
		afterID = lastID
	}
	return models, nil
}

// ghAccounts lists the segment's own values, then each account `gh auth
// status` reports logged in to github.com, without repeats. A gh that is
// not installed or does not answer in time leaves the segment's own values,
// with the failure as the RefreshError.
func ghAccounts(env Env, seg appconfig.OptionSegment) (Result, error) {
	values := slices.Clone(seg.Values)
	res, err := env.FX.Run(effects.Cmd{
		Argv:    []string{"gh", "auth", "status"},
		Timeout: ghTimeout,
		Capture: true,
		Read:    true,
		// An empty stdin, so the child never reads the terminal's keys.
		Stdin: []byte{},
	})
	if err != nil {
		if errors.Is(err, effects.ErrNotFound) || errors.Is(err, effects.ErrTimedOut) {
			return Result{Values: dedupe(values), RefreshError: err}, nil
		}
		return Result{}, err
	}
	// gh exits nonzero when any account has a problem, and still lists the
	// accounts; both streams carry them.
	for _, m := range ghAccountPattern.FindAllStringSubmatch(res.Stdout()+res.Stderr(), -1) {
		values = append(values, m[1])
	}
	return Result{Values: dedupe(values)}, nil
}

// GitHubToken returns the token gh holds for account. A gh that is missing,
// times out, exits nonzero, or prints nothing is an error; the token never
// appears in one.
func GitHubToken(fx *effects.FX, account string) (string, error) {
	if account == "" {
		return "", errors.New("no GitHub account given to fetch a token for")
	}
	res, err := fx.Run(effects.Cmd{
		Argv:    []string{"gh", "auth", "token", "--user", account},
		Timeout: ghTimeout,
		Capture: true,
		Read:    true,
		// An empty stdin, so the child never reads the terminal's keys.
		Stdin: []byte{},
	})
	if err != nil {
		return "", fmt.Errorf("fetching the GitHub token of %s: %w", account, err)
	}
	if res.ExitCode() != 0 {
		return "", fmt.Errorf("gh auth token --user %s exited with status %d: %s", account, res.ExitCode(), strings.TrimSpace(res.Stderr()))
	}
	token := strings.TrimSpace(res.Stdout())
	if token == "" {
		return "", fmt.Errorf("gh auth token --user %s printed no token", account)
	}
	return token, nil
}
