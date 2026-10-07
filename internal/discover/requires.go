package discover

import (
	"strings"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/install"
)

// Context1MSuffix is claudewheel's suffix on a model id selecting the 1M
// token context window. No id the API serves carries it, so a suffixed
// entry takes its release date and its version requirement from the base
// model.
const Context1MSuffix = "[1m]"

// BaseModel returns the model id a value derives from: the value without
// Context1MSuffix.
func BaseModel(value string) string {
	return strings.TrimSuffix(value, Context1MSuffix)
}

// Requires holds one segment's cross-segment requirements: option value ->
// segment key -> constraint (">=V", "<=V", ">V", "<V", or a value the other
// segment's selection must equal).
type Requires map[string]map[string]string

// ModelOptionRequires derives the model segment's version requirements from
// appconfig's ModelMinCLIVersion, the table the pre-launch model-version
// guard also reads, so the picker and the guard agree. A Context1MSuffix
// entry inherits its base model's requirement; a model not in the table is
// unrestricted.
func ModelOptionRequires() Requires {
	requires := Requires{}
	for model, minVersion := range appconfig.ModelMinCLIVersion() {
		requires[model] = map[string]string{appconfig.SegmentKeyVersion: ">=" + minVersion}
		requires[model+Context1MSuffix] = map[string]string{appconfig.SegmentKeyVersion: ">=" + minVersion}
	}
	return requires
}

// EvaluateRequires returns, for each segment key in requires, the set of its
// options whose requirements the selections do not satisfy. selections maps
// a segment key to its selected value; a segment without a selection is
// absent. A requirement on appconfig.SegmentKeyVersion is checked against
// install.EffectiveCLIVersion (the selection, else the claude link's
// target), resolved at most once and only when some option needs it; when
// that version is unknown, the requirement restricts nothing, as the
// pre-launch guard does. A requirement on any other segment with no
// selection is unsatisfied. A claude link that cannot be checked or resolved
// is an error.
func EvaluateRequires(requires map[string]Requires, selections map[string]string, locator install.Locator) (map[string]map[string]bool, error) {
	type answer struct {
		value string
		ok    bool
		err   error
	}
	resolved := map[string]answer{}
	resolve := func(segment string) (string, bool, error) {
		if a, done := resolved[segment]; done {
			return a.value, a.ok, a.err
		}
		value, ok := selections[segment]
		var err error
		if segment == appconfig.SegmentKeyVersion {
			value, ok, err = install.EffectiveCLIVersion(value, locator)
		}
		resolved[segment] = answer{value, ok, err}
		return value, ok, err
	}

	out := make(map[string]map[string]bool, len(requires))
	for key, segRequires := range requires {
		unavailable := map[string]bool{}
		for option, reqs := range segRequires {
			for segment, constraint := range reqs {
				value, ok, err := resolve(segment)
				if err != nil {
					return nil, err
				}
				if !ok && segment == appconfig.SegmentKeyVersion {
					continue
				}
				if !ok || !SatisfiesConstraint(value, constraint) {
					unavailable[option] = true
					break
				}
			}
		}
		out[key] = unavailable
	}
	return out, nil
}

// SatisfiesConstraint reports whether value meets constraint: a version
// comparison by install.CompareVersions for ">=", "<=", ">", and "<", and
// equality otherwise.
func SatisfiesConstraint(value, constraint string) bool {
	switch {
	case strings.HasPrefix(constraint, ">="):
		return install.CompareVersions(value, constraint[2:]) >= 0
	case strings.HasPrefix(constraint, "<="):
		return install.CompareVersions(value, constraint[2:]) <= 0
	case strings.HasPrefix(constraint, ">"):
		return install.CompareVersions(value, constraint[1:]) > 0
	case strings.HasPrefix(constraint, "<"):
		return install.CompareVersions(value, constraint[1:]) < 0
	default:
		return value == constraint
	}
}
