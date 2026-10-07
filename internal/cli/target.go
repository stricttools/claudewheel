package cli

import (
	"github.com/stricttools/strictcli/go/strictcli"
)

// profileTarget is the selection of the commands acting on profiles: one
// named profile (--profile <name>) or every profile (--all-profiles). The
// framework refuses naming neither or both. Build a fresh one per command:
// the member declarations identify the elected member.
type profileTarget struct {
	one  *strictcli.ChoiceDecl
	flag strictcli.Flag
}

// targetFlagName is the selection's name, the key its election is read by.
const targetFlagName = "target"

// newProfileTarget declares the selection; allHelp says what --all-profiles
// covers for this command.
func newProfileTarget(allHelp string) profileTarget {
	one := strictcli.MemberChoice(
		strictcli.StringFlag("profile", "name of the profile to target (e.g. work, personal, research)", strictcli.Required()),
		"target one profile, by name")
	all := strictcli.MemberChoice(
		strictcli.BoolFlag("all-profiles", allHelp, strictcli.Required()),
		allHelp)
	return profileTarget{
		one: one,
		flag: strictcli.MemberChoiceFlag(targetFlagName, "which profiles the operation applies to", strictcli.Required(),
			one, all),
	}
}

// read returns the elected selection as (profile, all): the named profile
// with all false, or "" with all true. An empty --profile value elects the
// named member with an empty name, which the profile packages refuse rather
// than read as every profile.
func (p profileTarget) read(kw map[string]interface{}) (profile string, all bool) {
	chosen := strictcli.GetElected(kw, targetFlagName)
	if chosen.Is(p.one) {
		return strictcli.Get[string](chosen.Fields, "value"), false
	}
	return "", true
}
