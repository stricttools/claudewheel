// Package tokens owns a profile's token entry: the OAuth token claudewheel
// stores for a profile, its dates, and the plan the profile's account is on.
//
// The entry is one JSON object in <profile dir>/.claudewheel/token.json, a
// secret: the file is always 0600 and its directory 0700. Store reads and
// writes one profile's entry; the rest of the package is the entry format
// and the closed list of plans.
package tokens

import (
	"fmt"
	"slices"
	"strings"
	"time"
)

// TokenTTLDays is the lifetime of a token `claude setup-token` issues.
const TokenTTLDays = 365

// The entry fields holding the declared plan, named as Claude Code names
// them, and the launch environment variables they become.
const (
	SubscriptionField = "subscriptionType"
	RateLimitField    = "rateLimitTier"

	SubscriptionEnvVar = "CLAUDE_CODE_SUBSCRIPTION_TYPE"
	RateLimitEnvVar    = "CLAUDE_CODE_RATE_LIMIT_TIER"
)

// SubscriptionTypes returns, sorted, the subscription types Claude Code's
// entitlement checks test for. Any other value acts there as no value at all,
// so it is refused.
func SubscriptionTypes() []string {
	return []string{"enterprise", "max", "pro", "team"}
}

// RateLimitTiers returns, sorted, the rate-limit tiers Claude Code knows.
// There is none for Pro, and Team accounts use the 5x Max value.
func RateLimitTiers() []string {
	return []string{"default_claude_max_20x", "default_claude_max_5x", "default_claude_zero"}
}

// PlanTier is one declarable plan: its key, its label, and the two fields it
// stores, always written together. RateLimitTier is empty for a plan that
// has none.
type PlanTier struct {
	Key              string
	Label            string
	SubscriptionType string
	RateLimitTier    string
}

// PlanTiers returns the declarable plans in picker order. The profile wizard,
// the pre-launch prompt, and `profile set-plan` all pick from this list. The
// pairings are measured from the Claude Code binary.
func PlanTiers() []PlanTier {
	return []PlanTier{
		{Key: "max-20x", Label: "Max 20x", SubscriptionType: "max", RateLimitTier: "default_claude_max_20x"},
		{Key: "max-5x", Label: "Max 5x", SubscriptionType: "max", RateLimitTier: "default_claude_max_5x"},
		{Key: "pro", Label: "Pro", SubscriptionType: "pro"},
		{Key: "team", Label: "Team", SubscriptionType: "team", RateLimitTier: "default_claude_max_5x"},
		{Key: "enterprise", Label: "Enterprise", SubscriptionType: "enterprise"},
	}
}

// PlanKeys returns the declarable plan keys in picker order.
func PlanKeys() []string {
	var keys []string
	for _, p := range PlanTiers() {
		keys = append(keys, p.Key)
	}
	return keys
}

// PlanByKey returns the plan named key; an unknown key is an error naming
// the valid ones.
func PlanByKey(key string) (PlanTier, error) {
	for _, p := range PlanTiers() {
		if p.Key == key {
			return p, nil
		}
	}
	return PlanTier{}, fmt.Errorf("unknown plan %q. Valid plans: %s", key, strings.Join(PlanKeys(), ", "))
}

// Entry is a token entry as token.json holds it. Every field is optional: an
// entry written only to declare a plan holds no token.
type Entry struct {
	Token *string `json:"token,omitempty"`
	// Created is the ISO date (YYYY-MM-DD) the token was written.
	Created *string `json:"created,omitempty"`
	// ExpiresAt is the ISO expiry date of a token with a known lifetime.
	ExpiresAt *string `json:"expires_at,omitempty"`
	// ExpiryUnknown marks a token issued elsewhere, whose lifetime is not
	// known; such an entry has no ExpiresAt.
	ExpiryUnknown    *bool   `json:"expiry_unknown,omitempty"`
	SubscriptionType *string `json:"subscriptionType,omitempty"`
	RateLimitTier    *string `json:"rateLimitTier,omitempty"`
}

// IsEmpty reports whether the entry holds no field at all.
func (e Entry) IsEmpty() bool {
	return e == Entry{}
}

// TokenValue returns the stored token, and false when there is none (absent
// or empty).
func (e Entry) TokenValue() (string, bool) {
	if e.Token == nil || *e.Token == "" {
		return "", false
	}
	return *e.Token, true
}

// ApplyPlan writes plan's fields into the entry. Both fields are replaced, so
// a plan without a rate-limit tier leaves none behind from an earlier plan.
func (e *Entry) ApplyPlan(plan PlanTier) {
	sub := plan.SubscriptionType
	e.SubscriptionType = &sub
	e.RateLimitTier = nil
	if plan.RateLimitTier != "" {
		tier := plan.RateLimitTier
		e.RateLimitTier = &tier
	}
}

// DeclaresPlan reports whether the entry declares a plan, which is whether
// it holds a non-empty subscription type: every plan carries one. An entry
// holding only a rate-limit tier declares none.
func (e Entry) DeclaresPlan() bool {
	return e.SubscriptionType != nil && *e.SubscriptionType != ""
}

// Tier returns the stored rate-limit tier and subscription type as they are,
// unvalidated, each empty when absent.
func (e Entry) Tier() (rateLimitTier, subscriptionType string) {
	if e.RateLimitTier != nil {
		rateLimitTier = *e.RateLimitTier
	}
	if e.SubscriptionType != nil {
		subscriptionType = *e.SubscriptionType
	}
	return rateLimitTier, subscriptionType
}

// PlanEnv returns the declared plan as Claude Code's environment variables:
// each non-empty plan field becomes its variable. A value Claude Code does
// not recognize is an error naming the field, source (where the entry was
// read from), and the valid values, since Claude Code would treat it as no
// value at all.
func (e Entry) PlanEnv(source string) (map[string]string, error) {
	env := map[string]string{}
	for _, f := range []struct {
		field, envVar string
		value         *string
		allowed       []string
	}{
		{SubscriptionField, SubscriptionEnvVar, e.SubscriptionType, SubscriptionTypes()},
		{RateLimitField, RateLimitEnvVar, e.RateLimitTier, RateLimitTiers()},
	} {
		if f.value == nil || *f.value == "" {
			continue
		}
		if !slices.Contains(f.allowed, *f.value) {
			return nil, fmt.Errorf("%s declares %s=%q, which Claude Code does not recognize. Valid values: %s",
				source, f.field, *f.value, strings.Join(f.allowed, ", "))
		}
		env[f.envVar] = *f.value
	}
	return env, nil
}

// ExpiryDisposition says how a written token's lifetime is recorded. The
// zero value is invalid: every writer chooses one.
type ExpiryDisposition int

const (
	// ExpiryTTL is for a token from `claude setup-token`, valid for
	// TokenTTLDays: the entry gets an expiry date.
	ExpiryTTL ExpiryDisposition = iota + 1
	// ExpiryNotKnown is for a token issued elsewhere: the entry gets the
	// expiry_unknown marker and no expiry date.
	ExpiryNotKnown
)

// isoDate is the layout of an entry's dates.
const isoDate = "2006-01-02"

// calendarDay returns t's calendar date in t's location, as midnight UTC.
func calendarDay(t time.Time) time.Time {
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

// daysBetween returns the whole days from a to b, both calendar days.
func daysBetween(a, b time.Time) int {
	return int(b.Sub(a).Hours() / 24)
}

// BuildEntry returns a new entry for token written on today's date: the token,
// its creation date, an expiry date (ExpiryTTL) or the unknown-expiry marker
// (ExpiryNotKnown), and plan's fields. The plan is required because writing
// a token restates it: a replaced token never inherits the plan declared
// for the one before it.
func BuildEntry(token string, expiry ExpiryDisposition, plan PlanTier, today time.Time) (Entry, error) {
	if token == "" {
		return Entry{}, fmt.Errorf("the token to store is empty")
	}
	created := calendarDay(today)
	createdText := created.Format(isoDate)
	e := Entry{Token: &token, Created: &createdText}
	switch expiry {
	case ExpiryTTL:
		expires := created.AddDate(0, 0, TokenTTLDays).Format(isoDate)
		e.ExpiresAt = &expires
	case ExpiryNotKnown:
		unknown := true
		e.ExpiryUnknown = &unknown
	default:
		return Entry{}, fmt.Errorf("token expiry disposition %d is not one of ExpiryTTL, ExpiryNotKnown", expiry)
	}
	e.ApplyPlan(plan)
	return e, nil
}

// Expiry is a token's computed lifetime. Created and Expires are calendar
// days at midnight UTC, nil when not known. RemainingDays is nil only for a
// token whose expiry is marked unknown.
type Expiry struct {
	Created       *time.Time
	Expires       *time.Time
	RemainingDays *int
}

// Expiry computes the entry's lifetime as of today. An entry marked with an
// unknown expiry has none. Otherwise an expiry date wins; then the creation
// date plus TokenTTLDays. An entry with neither date is reported with no
// dates and TokenTTLDays remaining, as a fresh token. A date that does not
// parse is an error.
func (e Entry) Expiry(today time.Time) (Expiry, error) {
	day := calendarDay(today)
	if e.ExpiryUnknown != nil && *e.ExpiryUnknown {
		return Expiry{}, nil
	}
	created, err := parseDate("created", e.Created)
	if err != nil {
		return Expiry{}, err
	}
	if e.ExpiresAt != nil && *e.ExpiresAt != "" {
		expires, err := parseDate("expires_at", e.ExpiresAt)
		if err != nil {
			return Expiry{}, err
		}
		remaining := daysBetween(day, *expires)
		return Expiry{Created: created, Expires: expires, RemainingDays: &remaining}, nil
	}
	if created != nil {
		expires := created.AddDate(0, 0, TokenTTLDays)
		remaining := TokenTTLDays - daysBetween(*created, day)
		return Expiry{Created: created, Expires: &expires, RemainingDays: &remaining}, nil
	}
	remaining := TokenTTLDays
	return Expiry{RemainingDays: &remaining}, nil
}

// parseDate parses an entry date field, nil when absent or empty.
func parseDate(field string, value *string) (*time.Time, error) {
	if value == nil || *value == "" {
		return nil, nil
	}
	t, err := time.Parse(isoDate, *value)
	if err != nil {
		return nil, fmt.Errorf("the token entry's %s %q is not a YYYY-MM-DD date", field, *value)
	}
	return &t, nil
}
