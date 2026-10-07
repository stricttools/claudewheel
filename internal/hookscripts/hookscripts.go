// Package hookscripts is the registry of the scripts claudewheel deploys into
// its scripts directory: the hook scripts Claude Code runs (the blocker and
// advise hooks generated from the guardrail model), the heavy wrapper, and the
// claudewheel-tool-scope shell prefix. It also deploys them, links the
// command scripts onto PATH, compares deployed copies with the registry, and
// writes and starts the probe runner's user service.
//
// The bash sources are embedded from templates/*.bash, byte for byte what
// claudewheel has always deployed, with @NAME@ placeholders filled in from
// the constants of the packages that own them. A script's text is built only
// when it is asked for.
package hookscripts

import (
	"embed"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/probe"
)

//go:embed templates/*.bash
var templateFS embed.FS

// ToolScopeScript is the shell prefix every launched session runs its Bash
// commands through (CLAUDE_CODE_SHELL_PREFIX). It is deployed with the hooks
// but is not one.
const ToolScopeScript = "claudewheel-tool-scope"

// heavyScript is the heavy wrapper, a command agents run.
const heavyScript = "heavy"

// substitution replaces every occurrence of a template's placeholder.
type substitution struct {
	placeholder string
	value       string
}

// source says how one script's text is built: by generate when it is set,
// otherwise from the template named after the script with its substitutions
// applied in order.
type source struct {
	generate      func() string
	substitutions []substitution
}

func sessionUUIDSubstitution() substitution {
	return substitution{"@SESSION_UUID_RE@", lifecycle.SessionUUIDRE.String()}
}

// The kill messages of heavy and of the shell prefix end with the fix every
// OOM report shares, inside a bash double-quoted string.
func oomKillFixSubstitution() substitution {
	return substitution{"@OOM_KILL_FIX@", guardrail.BashDquoteBody(probe.OOMKillFix)}
}

// sources maps every deployed script name to how its text is built.
func sources() map[string]source {
	return map[string]source{
		"hook-timestamp":      {},
		"hook-block-worktree": {},
		"hook-session-start":  {substitutions: []substitution{sessionUUIDSubstitution()}},
		"hook-session-end":    {substitutions: []substitution{sessionUUIDSubstitution()}},
		"hook-block-unsafe-commands": {
			generate: guardrail.GenerateBlockerScript,
		},
		"hook-advise-commands": {
			generate: guardrail.GenerateAdviseScript,
		},
		"hook-wait-for-probe-reports": {substitutions: []substitution{sessionUUIDSubstitution()}},
		"hook-deliver-probe-reports": {substitutions: []substitution{
			sessionUUIDSubstitution(),
			// The sentence sits inside a bash single-quoted string.
			{"@OVERLAP_SENTENCE@", strings.ReplaceAll(probe.OverlapSentence, "'", `'\''`)},
			{"@HOOK_WAIT_SECONDS@", strconv.Itoa(probe.HookWaitSeconds)},
			{"@BIND_RE@", probe.BindLineRE.String()},
		}},
		heavyScript:     {substitutions: []substitution{oomKillFixSubstitution()}},
		ToolScopeScript: {substitutions: []substitution{oomKillFixSubstitution()}},
	}
}

// Names returns the name of every script claudewheel deploys, sorted.
func Names() []string {
	srcs := sources()
	names := make([]string, 0, len(srcs))
	for name := range srcs {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// IsScript reports whether name is a script claudewheel deploys.
func IsScript(name string) bool {
	_, ok := sources()[name]
	return ok
}

// Script returns the text claudewheel deploys as the script name. An unknown
// name is an error listing the known ones.
func Script(name string) (string, error) {
	src, ok := sources()[name]
	if !ok {
		return "", fmt.Errorf("unknown script %q: the scripts claudewheel deploys are %s",
			name, strings.Join(Names(), ", "))
	}
	if src.generate != nil {
		return src.generate(), nil
	}
	data, err := templateFS.ReadFile("templates/" + name + ".bash")
	if err != nil {
		return "", fmt.Errorf("the embedded template of %s: %w", name, err)
	}
	text := string(data)
	for _, s := range src.substitutions {
		if !strings.Contains(text, s.placeholder) {
			return "", fmt.Errorf("the template of %s holds no %s placeholder", name, s.placeholder)
		}
		text = strings.ReplaceAll(text, s.placeholder, s.value)
	}
	return text, nil
}

// PathCommands returns the deployed scripts that are commands an agent runs,
// not hooks Claude Code runs. Deploying one links it into the bin directory
// (~/.local/bin), so the command on PATH is always the copy claudewheel
// deployed.
func PathCommands() []string {
	return []string{heavyScript}
}

// IsPathCommand reports whether name is one of PathCommands.
func IsPathCommand(name string) bool {
	for _, c := range PathCommands() {
		if c == name {
			return true
		}
	}
	return false
}

// Exit2Hooks maps each hook that may exit 2 to why. It is the written
// exception to the rule that a claudewheel hook never blocks a session: every
// other hook exits 1 on failure, never 2.
func Exit2Hooks() map[string]string {
	return map[string]string{
		"hook-wait-for-probe-reports": "an asyncRewake hook exiting 2 blocks nothing: it is how a background " +
			"hook wakes an idle session with its output, and nothing else can",
	}
}
