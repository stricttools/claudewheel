package profiles

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/tokens"
)

// shadowKey is the session-credentials block in .credentials.json that
// shadows a profile's stored long-lived token.
const shadowKey = "claudeAiOauth"

// FixAuthOutcome is what FixAuth did.
type FixAuthOutcome string

const (
	// FixAuthRemoved means the shadowing credentials were removed.
	FixAuthRemoved FixAuthOutcome = "removed"
	// FixAuthNoToken means the profile stores no token, so there is nothing
	// a shadow could hide.
	FixAuthNoToken FixAuthOutcome = "no-token"
	// FixAuthNoShadow means no session credentials shadow the token.
	FixAuthNoShadow FixAuthOutcome = "no-shadow"
)

// readCredentials reads a .credentials.json as an ordered tree, reporting
// false when it does not exist. Errors never quote the file's content.
func readCredentials(path string) (*jsonfile.Object, bool, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("cannot read %s: %w", path, err)
	}
	creds, err := jsonfile.DecodeObject(data)
	if err != nil {
		return nil, false, fmt.Errorf("cannot read %s: it is not a JSON object", path)
	}
	return creds, true, nil
}

// FixAuth strips the session credentials (claudeAiOauth) from profile
// name's .credentials.json, so its stored long-lived token is used again.
// Plan fields in that block go with it: the declared plan lives in the
// token entry. A file left empty is removed rather than kept, since an
// empty file would still read as "this profile has credentials". A name
// with no profile directory, a corrupt token entry, and an unreadable
// credentials file are errors.
func (s Store) FixAuth(fx *effects.FX, name string) (FixAuthOutcome, error) {
	if err := s.CheckPendingRenames(); err != nil {
		return "", err
	}
	dir := s.PathFor(name)
	isDirectory, err := isDir(dir)
	if err != nil {
		return "", err
	}
	if !isDirectory {
		return "", fmt.Errorf("No profile '%s'", name)
	}
	_, hasToken, err := s.Data(name).Token()
	if err != nil {
		return "", err
	}
	if !hasToken {
		return FixAuthNoToken, nil
	}
	path := filepath.Join(dir, CredentialsFileName)
	creds, found, err := readCredentials(path)
	if err != nil {
		return "", err
	}
	if !found || !creds.Delete(shadowKey) {
		return FixAuthNoShadow, nil
	}
	if creds.Len() == 0 {
		return FixAuthRemoved, fx.RemoveIfExists(path)
	}
	data, err := jsonfile.MarshalIndented(creds)
	if err != nil {
		return "", fmt.Errorf("encoding %s: %w", path, err)
	}
	return FixAuthRemoved, fx.WriteSecretAtomic(path, data)
}

// DetectAuthShadow reports whether profile name has session credentials
// shadowing a stored token: a token is stored and .credentials.json holds
// claudeAiOauth. It answers false for anything it cannot read, since the
// health checks that use it report an unreadable token entry themselves.
func (s Store) DetectAuthShadow(name string) bool {
	_, hasToken, err := s.Data(name).Token()
	if err != nil || !hasToken {
		return false
	}
	creds, found, err := readCredentials(filepath.Join(s.PathFor(name), CredentialsFileName))
	if err != nil || !found {
		return false
	}
	_, shadowed := creds.Get(shadowKey)
	return shadowed
}

// SetPlan declares the plan (by key, see tokens.PlanKeys) of profile name's
// account in its token entry, leaving the token alone, and returns the plan.
func (s Store) SetPlan(fx *effects.FX, name, planKey string) (tokens.PlanTier, error) {
	if err := s.CheckPendingRenames(); err != nil {
		return tokens.PlanTier{}, err
	}
	isDirectory, err := isDir(s.PathFor(name))
	if err != nil {
		return tokens.PlanTier{}, err
	}
	if !isDirectory {
		return tokens.PlanTier{}, fmt.Errorf("No profile '%s'", name)
	}
	plan, err := tokens.PlanByKey(planKey)
	if err != nil {
		return tokens.PlanTier{}, err
	}
	return plan, s.Data(name).SetPlan(fx, plan)
}

// StoredToken is one profile's stored token, for check-tokens; Token is
// empty when the profile stores none.
type StoredToken struct {
	Profile string
	Token   string
}

// StoredTokens returns every profile's stored token, in name order. An
// unreadable token file is an error naming it.
func (s Store) StoredTokens() ([]StoredToken, error) {
	all, err := s.Enumerate()
	if err != nil {
		return nil, err
	}
	out := make([]StoredToken, 0, len(all))
	for _, p := range all {
		token, _, err := tokens.NewStore(p.Path).Token()
		if err != nil {
			return nil, err
		}
		out = append(out, StoredToken{Profile: p.Name, Token: token})
	}
	return out, nil
}

// TokenPreviewLength is how many leading characters of a token a preview
// shows.
const TokenPreviewLength = 20

// TokenPreview is the first TokenPreviewLength characters of token followed
// by "...".
func TokenPreview(token string) string {
	n := 0
	for i := range token {
		if n == TokenPreviewLength {
			return token[:i] + "..."
		}
		n++
	}
	return token + "..."
}
