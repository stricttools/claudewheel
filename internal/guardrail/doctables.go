package guardrail

import (
	"fmt"
	"strings"
)

// The guardrails documentation page carries two tables generated from the
// model: the rule table and the stripped-tool table. Each sits between a begin
// and an end marker line; scripts/gen-guardrail-docs rewrites what is between
// them, and a test refuses a page whose tables differ from the model.
const (
	ruleTableName = "rule-table"
	toolTableName = "stripped-tool-table"
)

func tableBeginMarker(name string) string {
	return "<!-- " + name + ": generated from the guardrail model by scripts/gen-guardrail-docs; do not edit -->"
}

func tableEndMarker(name string) string {
	return "<!-- end of " + name + " -->"
}

// markdownTable renders a pipe table, escaping pipes and turning newlines
// into spaces in every cell.
func markdownTable(headers []string, rows [][]string) string {
	esc := func(cell string) string {
		return strings.ReplaceAll(strings.ReplaceAll(cell, "|", `\|`), "\n", " ")
	}
	line := func(cells []string) string {
		escaped := make([]string, len(cells))
		for i, c := range cells {
			escaped[i] = esc(c)
		}
		return "| " + strings.Join(escaped, " | ") + " |"
	}
	sep := make([]string, len(headers))
	for i := range sep {
		sep[i] = "---"
	}
	out := []string{line(headers), "| " + strings.Join(sep, " | ") + " |"}
	for _, r := range rows {
		out = append(out, line(r))
	}
	return strings.Join(out, "\n")
}

// ruleAdvice is the advice column of a rule: the main agent's advice, or for
// an escalate rule the lead sentence of the subagent's message, or for an
// ask rule a note that the settings ask rule prompts.
func ruleAdvice(r Rule) string {
	if r.MainAdvice != "" {
		return r.MainAdvice
	}
	switch r.Tier {
	case TierEscalate:
		lead := strings.TrimSpace(strings.TrimSuffix(r.SubagentAdvice, EscalateTail))
		if lead == "" {
			return "Subagents are denied; the main agent is prompted via settings."
		}
		return lead
	case TierAsk:
		return "Prompted via the settings ask rule (no hook)."
	}
	return ""
}

// RuleTableMarkdown renders every rule as a row: its key, tier, settings
// coverage ("n/a" where coverage does not apply), and advice.
func RuleTableMarkdown() string {
	var rows [][]string
	for _, r := range Rules() {
		coverage := r.Coverage.Name()
		if coverage == "" {
			coverage = "n/a"
		}
		rows = append(rows, []string{"`" + r.Key + "`", r.Tier.Name(), coverage, ruleAdvice(r)})
	}
	return markdownTable([]string{"Key", "Tier", "Settings coverage", "Advice"}, rows)
}

// DisallowedToolTableMarkdown renders every stripped tool and why.
func DisallowedToolTableMarkdown() string {
	var rows [][]string
	for _, t := range DisallowedToolEntries() {
		rows = append(rows, []string{"`" + t.Name + "`", t.Why})
	}
	return markdownTable([]string{"Tool", "Why"}, rows)
}

// RewriteDocTables returns page with the text between each table's markers
// replaced by the table the model renders. A page that lacks a marker, or
// holds one more than once, is refused.
func RewriteDocTables(page string) (string, error) {
	tables := []struct {
		name string
		body string
	}{
		{ruleTableName, RuleTableMarkdown()},
		{toolTableName, DisallowedToolTableMarkdown()},
	}
	for _, t := range tables {
		begin, end := tableBeginMarker(t.name), tableEndMarker(t.name)
		if n := strings.Count(page, begin); n != 1 {
			return "", fmt.Errorf("the page must hold the line %q once, found %d", begin, n)
		}
		if n := strings.Count(page, end); n != 1 {
			return "", fmt.Errorf("the page must hold the line %q once, found %d", end, n)
		}
		head, rest, _ := strings.Cut(page, begin)
		_, tail, found := strings.Cut(rest, end)
		if !found {
			return "", fmt.Errorf("%q comes before %q", end, begin)
		}
		page = head + begin + "\n\n" + t.body + "\n\n" + end + tail
	}
	return page, nil
}
