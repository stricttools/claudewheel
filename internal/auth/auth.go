// Package auth validates OAuth tokens against the Anthropic API and extracts
// them from captured terminal output. A token never appears in an error or
// an effect record: every request carrying one redacts it.
package auth

import (
	"bytes"
	"errors"
	"regexp"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
)

// Status is the outcome of ValidateToken.
type Status string

const (
	// Valid: the API accepted the token (HTTP 200).
	Valid Status = "valid"
	// Invalid: the API rejected the token (HTTP 401).
	Invalid Status = "invalid"
	// Unreachable: the request failed (DNS, timeout, refused connection).
	Unreachable Status = "unreachable"
	// Indeterminate: any other HTTP status (400, 429, 5xx, a 2xx other
	// than 200).
	Indeterminate Status = "indeterminate"
)

// ModelsEndpoint is the Anthropic models endpoint and AnthropicVersion the
// API version header, shared with model discovery so both are stated once.
const (
	ModelsEndpoint   = "https://api.anthropic.com/v1/models"
	AnthropicVersion = "2023-06-01"
)

// ValidateTimeout bounds one validation request.
const ValidateTimeout = 5 * time.Second

// modelsProbeURL asks for one model: validation reads only the status.
const modelsProbeURL = ModelsEndpoint + "?limit=1"

// ansiPattern matches the terminal escape sequences stripped before a token
// is searched for: OSC (OSC-8 hyperlinks included) ended by BEL or ST, CSI
// sequences, character set selections, keypad mode switches, and carriage
// returns.
var ansiPattern = regexp.MustCompile(
	`\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)` +
		`|\x1b\[[0-9;?]*[ -/]*[@-~]` +
		`|\x1b[()][A-Za-z0-9]` +
		`|\x1b[=>]` +
		`|\r`)

// tokenPattern matches a token in captured output. Only the stable "sk-ant-"
// prefix is required, not the current "oat01" infix: the live validation is
// what decides.
var tokenPattern = regexp.MustCompile(`sk-ant-[A-Za-z0-9_-]{30,}`)

// tokenFormatPattern is the offline shape of a pasted token: the "sk-ant-"
// prefix and at least one token character, with no length minimum.
var tokenFormatPattern = regexp.MustCompile(`^sk-ant-[A-Za-z0-9_-]+$`)

// tokenLabel follows the token in Claude Code's setup-token output.
const tokenLabel = "valid for 1 year"

// minTokenLen is the shortest candidate ExtractToken accepts.
const minTokenLen = 50

// LooksLikeToken reports whether token has the shape of an API token,
// without any network call, so garbage is refused before ValidateToken.
func LooksLikeToken(token string) bool {
	return tokenFormatPattern.MatchString(token)
}

// ValidateToken asks the Anthropic API whether token is accepted. The request
// changes nothing on the far side, so it runs in every mode, read-only and
// --dry-run included. A failed request is Unreachable, not an error; the
// error is for a token or request this package refuses to send (an empty
// token). The token is redacted from everything returned.
func ValidateToken(fx *effects.FX, token string) (Status, error) {
	if token == "" {
		return "", errors.New("cannot validate an empty token")
	}
	resp, err := fx.HTTPRead(effects.Request{
		Method: "GET",
		URL:    modelsProbeURL,
		Header: map[string]string{
			"Authorization":     "Bearer " + token,
			"anthropic-version": AnthropicVersion,
		},
		Timeout: ValidateTimeout,
		Redact:  []string{token},
	})
	if err == nil {
		// HTTPRead returns no error for any 2xx; only 200 is an acceptance.
		if resp.Status == 200 {
			return Valid, nil
		}
		return Indeterminate, nil
	}
	var statusErr *effects.StatusError
	if errors.As(err, &statusErr) {
		if statusErr.Status == 401 {
			return Invalid, nil
		}
		return Indeterminate, nil
	}
	return Unreachable, nil
}

// ExtractToken finds an OAuth token in raw output captured from a PTY. It
// strips terminal escape sequences, joins the lines (the terminal may wrap a
// token), searches after the last "valid for 1 year" label when there is one
// (Ink redraws frames, and the last frame wins), and falls back to the whole
// output when nothing follows the label, since the token may come before it.
func ExtractToken(captured []byte) (string, bool) {
	clean := ansiPattern.ReplaceAllLiteral(captured, nil)
	joined := bytes.ReplaceAll(clean, []byte("\n"), nil)
	if at := bytes.LastIndex(joined, []byte(tokenLabel)); at != -1 {
		if token, ok := bestCandidate(joined[at+len(tokenLabel):]); ok {
			return token, true
		}
	}
	return bestCandidate(joined)
}

// bestCandidate picks among the token matches of at least minTokenLen
// characters: the last one holding the "oat01" infix, or the last one when
// none does.
func bestCandidate(data []byte) (string, bool) {
	var matches [][]byte
	for _, m := range tokenPattern.FindAll(data, -1) {
		if len(m) >= minTokenLen {
			matches = append(matches, m)
		}
	}
	if len(matches) == 0 {
		return "", false
	}
	var preferred [][]byte
	for _, m := range matches {
		if bytes.Contains(m, []byte("oat01")) {
			preferred = append(preferred, m)
		}
	}
	pool := matches
	if len(preferred) > 0 {
		pool = preferred
	}
	return string(pool[len(pool)-1]), true
}
