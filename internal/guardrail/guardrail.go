// Package guardrail is the canonical guardrail model: which commands are
// hard-denied, which escalate to the user when a subagent tries them, which
// merely advise, and which prompt through settings; the allow entries that
// conflict with them; the tools stripped from every launched session; the hook
// wiring every profile carries; the bash sources of the blocker and advise
// hooks; and the canonical settings trees derived from all of it.
//
// The hook patterns are plain ERE text. They only ever go into generated bash
// (grep -qE), so they follow the syntax grep accepts, not Go's regexp.
package guardrail

import (
	"strings"
	"unicode"
)

// Tier is a guardrail enforcement tier.
//
//   - TierHardDeny: denied for everyone by the PreToolUse hook, which is the
//     authoritative enforcer; a settings deny glob is best-effort cover for the
//     plain command form only.
//   - TierEscalate: denied only for subagents; the main agent falls through so
//     the settings ask rule prompts the user.
//   - TierAdvise: PostToolUse advice only; the command runs.
//   - TierAsk: a settings ask rule only, no hook.
type Tier int

const (
	TierHardDeny Tier = iota + 1
	TierEscalate
	TierAdvise
	TierAsk
)

// Name is the tier's upper-case name, as the generated hooks and the docs
// table spell it.
func (t Tier) Name() string {
	switch t {
	case TierHardDeny:
		return "HARD_DENY"
	case TierEscalate:
		return "ESCALATE"
	case TierAdvise:
		return "ADVISE"
	case TierAsk:
		return "ASK"
	}
	panic("guardrail: unknown tier")
}

// SettingsCoverage says how completely a rule's settings deny or ask globs
// cover its hook surface. It applies to the hard-deny and escalate tiers only;
// advise and ask rules carry CoverageNotApplicable.
type SettingsCoverage int

const (
	CoverageNotApplicable SettingsCoverage = iota
	// CoverageFull: the globs cover the rule's entire hook surface.
	CoverageFull
	// CoveragePartial: the globs cover part of it; the rule's coverage reason
	// names what they miss.
	CoveragePartial
	// CoverageNone: the rule owns no settings glob; the coverage reason names
	// what covers it instead.
	CoverageNone
)

// Name is the coverage's upper-case name ("FULL", "PARTIAL", "NONE"), or ""
// when the coverage does not apply.
func (c SettingsCoverage) Name() string {
	switch c {
	case CoverageFull:
		return "FULL"
	case CoveragePartial:
		return "PARTIAL"
	case CoverageNone:
		return "NONE"
	}
	return ""
}

// Rule is one guardrail rule.
//
// HookPatterns are plain ERE strings the hook matches against the command
// (empty for ask rules). DenyRules and AskRules are the settings
// permissions.deny and permissions.ask entries the rule contributes.
// MainAdvice is what the main agent is shown (empty for escalate and ask
// rules); SubagentAdvice is what a subagent is shown (empty for ask rules).
type Rule struct {
	Key            string
	Tier           Tier
	HookPatterns   []string
	DenyRules      []string
	AskRules       []string
	MainAdvice     string
	SubagentAdvice string
	Coverage       SettingsCoverage
	CoverageReason string
}

// The separator every command matcher starts with: start of string or a shell
// command separator, then optional whitespace. Keeps "git add" from matching
// inside "mygit add".
const sep = `(^|[;&|]|&&|\|\|)\s*`

// Like sep, plus an opening parenthesis, so a command opening a subshell is
// matched too. Kept apart from sep so widening it stays a per-rule decision.
const subshellSep = `(^|[;&|(]|&&|\|\|)\s*`

// SubagentHardDenySuffix ends a subagent's hard-deny advice.
const SubagentHardDenySuffix = "You are a subagent: report to your parent agent why you attempted this command."

// EscalateTail follows an escalate rule's lead sentence in the message a
// denied subagent sees.
const EscalateTail = "Only your parent agent may run this command (the user will be asked to " +
	"approve it). Explain in detail to your parent agent why you wanted to run " +
	"this command."

// asSentence strips trailing whitespace and appends a period unless the text
// already ends in '.', '!', or '?'.
func asSentence(text string) string {
	text = strings.TrimRightFunc(text, unicode.IsSpace)
	if text != "" && !strings.ContainsAny(text[len(text)-1:], ".!?") {
		text += "."
	}
	return text
}

func hardDeny(key string, patterns, denyRules []string, advice string, coverage SettingsCoverage, reason string) Rule {
	main := asSentence(advice)
	return Rule{
		Key:            key,
		Tier:           TierHardDeny,
		HookPatterns:   patterns,
		DenyRules:      denyRules,
		MainAdvice:     main,
		SubagentAdvice: main + " " + SubagentHardDenySuffix,
		Coverage:       coverage,
		CoverageReason: reason,
	}
}

func escalate(key string, patterns, askRules []string, lead string, coverage SettingsCoverage, reason string) Rule {
	return Rule{
		Key:            key,
		Tier:           TierEscalate,
		HookPatterns:   patterns,
		AskRules:       askRules,
		SubagentAdvice: asSentence(lead) + " " + EscalateTail,
		Coverage:       coverage,
		CoverageReason: reason,
	}
}

func advise(key string, patterns []string, advice string) Rule {
	advice = asSentence(advice)
	return Rule{
		Key:            key,
		Tier:           TierAdvise,
		HookPatterns:   patterns,
		MainAdvice:     advice,
		SubagentAdvice: advice,
	}
}

func ask(key string, askRules []string) Rule {
	return Rule{
		Key:      key,
		Tier:     TierAsk,
		AskRules: askRules,
	}
}

// cmd anchors a command-matcher literal at the start of a shell command.
func cmd(literal string) string {
	return sep + literal
}

// subshellCmd anchors a literal at any command-word position, a subshell's
// opening included, and refuses a longer word that begins with it.
func subshellCmd(literal string) string {
	return subshellSep + literal + `(\s|$)`
}

// wrappedMatcher matches the ERE fragment c invoked directly, through
// sudo, env, or xargs (with leading flags), or through find's -exec family.
// Each branch requires c to be followed by whitespace or the end.
func wrappedMatcher(c string) string {
	tail := `(\s|$)`
	return sep +
		c +
		tail +
		`|(^|\s)(sudo|env|xargs)\s+(-\S+\s+)*` +
		c +
		tail +
		`|(^|\s)-(exec|execdir|ok|okdir)\s+` +
		c +
		tail
}

// Rules returns every guardrail rule in canonical order, built fresh.
//
// The order fixes the order of the deny and ask arrays, and a more specific
// rule precedes the general rule it overlaps so its advice fires first:
// git-checkout-file before git-checkout, git-push-delete before push.
func Rules() []Rule {
	return []Rule{
		// Hard deny.
		hardDeny(
			"rm",
			[]string{wrappedMatcher("rm")},
			[]string{"Bash(rm:*)"},
			"Use 'saferm delete --description \"why\" file1 file2' instead of 'rm'",
			CoveragePartial,
			"Bash(rm:*) matches only the bare 'rm' command; the hook also "+
				"reaches rm via sudo/env/xargs and find -exec, which no deny glob "+
				"covers.",
		),
		hardDeny(
			"git-add-bulk",
			// git add -A/-u/-U/--all/. but not a plain "git add file".
			[]string{cmd(`git\s+add\s+(-[AuU]|--all|\.)`)},
			[]string{
				"Bash(git add .)",
				"Bash(git add -A*)",
				"Bash(git add --all*)",
				"Bash(git add -u*)",
			},
			"Use 'safegit commit -m \"msg\" -- file1 file2' instead of 'git add'",
			CoveragePartial,
			"the hook's -[AuU] flag class also matches 'git add -U', which no "+
				"deny glob covers (globs cover -A/-u/--all/. only).",
		),
		hardDeny(
			"git-stash",
			[]string{cmd(`git\s+stash(\s|$)`)},
			[]string{"Bash(git stash:*)"},
			"Never 'git stash'. Commit the work in progress on the current branch "+
				"with 'safegit commit' instead.",
			CoverageFull,
			"",
		),
		hardDeny(
			"git-restore",
			[]string{cmd(`git\s+restore(\s|$)`)},
			[]string{"Bash(git restore:*)"},
			"Use the Edit tool to revert specific lines instead of 'git restore'",
			CoverageFull,
			"",
		),
		hardDeny(
			"git-checkout-file",
			// "git checkout -- <path>"; owns no settings rule of its own.
			[]string{cmd(`git\s+checkout\s+--\s`)},
			[]string{},
			"Use the Edit tool to revert specific lines instead of 'git checkout -- file'",
			CoverageNone,
			"owns no deny glob of its own; the sibling git-checkout rule's "+
				"Bash(git checkout:*) is the settings backstop for this form.",
		),
		hardDeny(
			"git-checkout",
			[]string{cmd(`git\s+checkout(\s|$)`)},
			[]string{"Bash(git checkout:*)"},
			"'git checkout' is deprecated here; use 'git switch' for branches "+
				"(plain git switch is allowed) or the Edit tool to revert files",
			CoverageFull,
			"",
		),
		hardDeny(
			"git-push-delete",
			// Branch deletion in any form: --delete, -d, and an empty-source
			// refspec (":branch" or "+:branch"), bounded to one shell segment.
			[]string{cmd(`git\s+push\b[^;&|]*(--delete|\s-d(\s|$)|\s\+?:)`)},
			[]string{"Bash(git push origin --delete*)"},
			"Deleting remote branches is destructive; ask the user to do this deliberately.",
			CoveragePartial,
			"the deny glob covers only origin + --delete; the hook also matches "+
				"the empty-source colon refspec (:b / +:b), the -d short form, and "+
				"any other remote.",
		),
		hardDeny(
			"sleep",
			[]string{subshellCmd("sleep")},
			[]string{"Bash(sleep:*)"},
			"Never 'sleep' to wait: the harness notifies you when background work "+
				"finishes, so read the state you are waiting on or do other work "+
				"instead of padding the turn with a wait.",
			CoveragePartial,
			"Bash(sleep:*) matches only a command line that starts with "+
				"'sleep'; the hook also matches it after a separator and inside a "+
				"subshell, which no deny glob covers.",
		),
		// Escalate.
		escalate(
			"push",
			[]string{cmd(`(git|safegit|\./safegit)\s+push(\s|$)`)},
			[]string{
				"Bash(git push:*)",
				"Bash(safegit push:*)",
				"Bash(./safegit push:*)",
			},
			"Pushes happen only via rlsbl release run.",
			CoverageFull,
			"",
		),
		escalate(
			"git-reset",
			[]string{cmd(`git\s+reset(\s|$)`)},
			[]string{"Bash(git reset *)"},
			"git reset is destructive in shared worktrees.",
			CoveragePartial,
			"ask glob Bash(git reset *) requires a trailing argument and misses "+
				"the bare 'git reset' (no args) that the hook matches.",
		),
		escalate(
			"git-switch-force",
			[]string{cmd(`git\s+switch\s+(-f|--force)(\s|$)`)},
			[]string{
				"Bash(git switch -f*)",
				"Bash(git switch --force*)",
			},
			"Forced switch destroys uncommitted work in shared worktrees.",
			CoverageFull,
			"",
		),
		escalate(
			"gh-workflow-run",
			[]string{cmd(`gh\s+workflow\s+run(\s|$)`)},
			[]string{"Bash(gh workflow run*)"},
			"Triggering CI workflows is an outward-facing action.",
			CoverageFull,
			"",
		),
		escalate(
			"saferm-purge",
			[]string{cmd(`saferm\s+purge(\s|$)`)},
			[]string{"Bash(saferm purge:*)"},
			"saferm purge permanently destroys archived files.",
			CoverageFull,
			"",
		),
		escalate(
			"git-rebase",
			[]string{cmd(`git\s+rebase(\s|$)`)},
			[]string{"Bash(git rebase *)"},
			"Rebase rewrites history in shared worktrees.",
			CoveragePartial,
			"ask glob Bash(git rebase *) requires a trailing argument and "+
				"misses the bare 'git rebase' (no args) that the hook matches.",
		),
		escalate(
			"safegit-author-rewrite",
			// The old spelling "safegit rewrite-author" always exits 1, so it
			// is not guarded.
			[]string{cmd(`(safegit|\./safegit)\s+author\s+rewrite(\s|$)`)},
			[]string{
				"Bash(safegit author rewrite:*)",
				"Bash(./safegit author rewrite:*)",
			},
			"Author rewriting is history rewriting.",
			CoverageFull,
			"",
		),
		// Advise.
		advise(
			"kill",
			[]string{wrappedMatcher("p?kill")},
			"This kill/pkill ran, but prefer building graceful stop commands or "+
				"PID-file-based stop scripts into your tooling instead of killing "+
				"processes directly.",
		),
		// Ask.
		ask("sudo", []string{"Bash(sudo:*)"}),
	}
}

// RulesByTier returns the rules of tier t, in canonical order.
func RulesByTier(t Tier) []Rule {
	var out []Rule
	for _, r := range Rules() {
		if r.Tier == t {
			out = append(out, r)
		}
	}
	return out
}

// CanonicalDenyRules returns the ordered settings permissions.deny array:
// every rule's deny entries, in rule order.
func CanonicalDenyRules() []string {
	out := []string{}
	for _, r := range Rules() {
		out = append(out, r.DenyRules...)
	}
	return out
}

// CanonicalAskRules returns the ordered settings permissions.ask array:
// every rule's ask entries, in rule order.
func CanonicalAskRules() []string {
	out := []string{}
	for _, r := range Rules() {
		out = append(out, r.AskRules...)
	}
	return out
}

// AllowConflicts returns the allow-array entries reconcile removes because they
// are dead or conflict with the deny and ask rules. Entries not listed stay
// allowed on purpose, such as Bash(git rm:*) and Bash(npm run kill:*).
func AllowConflicts() []string {
	return []string{
		"Bash(git add:*)",
		"Bash(git checkout:*)",
		"Bash(git stash:*)",
		"Bash(git stash push:*)",
		"Bash(sudo npm install:*)",
		"Bash(sudo -S npm install:*)",
		"Bash(sudo -S dnf install:*)",
		"Bash(sudo -S dnf install -y chromium)",
		"Bash(sudo adb start-server:*)",
		"Bash(sudo -n iptables -L INPUT -n)",
		// The stored entry carries a literal backslash before each parenthesis;
		// membership is exact, so this must match it byte for byte.
		`Bash(sudo -n ufw status || echo "\(need sudo for ufw\)")`,
		// Skill is always stripped through --disallowedTools, so an allow entry
		// for it can never fire.
		"Skill(gsd:discuss-phase)",
	}
}

// IsAllowConflict reports whether entry is one of AllowConflicts.
func IsAllowConflict(entry string) bool {
	for _, c := range AllowConflicts() {
		if c == entry {
			return true
		}
	}
	return false
}
