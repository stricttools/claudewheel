package guardrail

import (
	"encoding/json"
	"os"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// pythonModel is the Python guardrail model, written by
// scripts/python-expectations guardrail-model. The Go model must equal it:
// a difference in canonical order would rewrite every profile on the first
// Go reconcile.
type pythonModel struct {
	Rules []struct {
		Key              string   `json:"key"`
		Tier             string   `json:"tier"`
		HookPatterns     []string `json:"hook_patterns"`
		DenyRules        []string `json:"deny_rules"`
		AskRules         []string `json:"ask_rules"`
		MainAdvice       string   `json:"main_advice"`
		SubagentAdvice   string   `json:"subagent_advice"`
		SettingsCoverage string   `json:"settings_coverage"`
		CoverageReason   string   `json:"coverage_reason"`
	} `json:"rules"`
	CanonicalDenyRules       []string        `json:"canonical_deny_rules"`
	CanonicalAskRules        []string        `json:"canonical_ask_rules"`
	AllowConflicts           []string        `json:"allow_conflicts"`
	DisallowedToolNames      []string        `json:"disallowed_tool_names"`
	CanonicalProfileSettings json.RawMessage `json:"canonical_profile_settings"`
	ScriptsDir               string          `json:"scripts_dir"`
	SharedSettingsText       string          `json:"canonical_shared_settings_text"`
}

func loadPythonModel(t *testing.T) pythonModel {
	t.Helper()
	data, err := os.ReadFile("testdata/python-model.json")
	if err != nil {
		t.Fatal(err)
	}
	var m pythonModel
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func orEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func TestRulesMatchThePython(t *testing.T) {
	want := loadPythonModel(t).Rules
	got := Rules()
	if len(got) != len(want) {
		t.Fatalf("%d rules, the Python has %d", len(got), len(want))
	}
	for i, r := range got {
		w := want[i]
		if r.Key != w.Key {
			t.Fatalf("rule %d is %q, the Python's is %q", i, r.Key, w.Key)
		}
		checks := []struct {
			field    string
			got, exp any
		}{
			{"tier", r.Tier.Name(), w.Tier},
			{"hook patterns", orEmpty(r.HookPatterns), w.HookPatterns},
			{"deny rules", orEmpty(r.DenyRules), w.DenyRules},
			{"ask rules", orEmpty(r.AskRules), w.AskRules},
			{"main advice", r.MainAdvice, w.MainAdvice},
			{"subagent advice", r.SubagentAdvice, w.SubagentAdvice},
			{"coverage", r.Coverage.Name(), w.SettingsCoverage},
			{"coverage reason", r.CoverageReason, w.CoverageReason},
		}
		for _, c := range checks {
			g, _ := json.Marshal(c.got)
			e, _ := json.Marshal(c.exp)
			if string(g) != string(e) {
				t.Errorf("rule %s %s:\n got: %s\nwant: %s", r.Key, c.field, g, e)
			}
		}
	}
}

func TestSettingsListsMatchThePython(t *testing.T) {
	m := loadPythonModel(t)
	lists := []struct {
		name      string
		got, want []string
	}{
		{"CanonicalDenyRules", CanonicalDenyRules(), m.CanonicalDenyRules},
		{"CanonicalAskRules", CanonicalAskRules(), m.CanonicalAskRules},
		{"AllowConflicts", AllowConflicts(), m.AllowConflicts},
		{"DisallowedToolNames", DisallowedToolNames(), m.DisallowedToolNames},
	}
	for _, l := range lists {
		if !slices.Equal(l.got, l.want) {
			t.Errorf("%s:\n got: %q\nwant: %q", l.name, l.got, l.want)
		}
	}
}

func TestCanonicalSharedSettingsMatchThePython(t *testing.T) {
	m := loadPythonModel(t)
	got, err := jsonfile.MarshalIndented(CanonicalSharedSettings(m.ScriptsDir))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != m.SharedSettingsText {
		t.Errorf("CanonicalSharedSettings differs from the Python's:\n got: %s\nwant: %s", got, m.SharedSettingsText)
	}
	profile, err := jsonfile.MarshalCompactASCII(CanonicalProfileSettings())
	if err != nil {
		t.Fatal(err)
	}
	want, err := jsonfile.Decode(m.CanonicalProfileSettings)
	if err != nil {
		t.Fatal(err)
	}
	wantText, _ := jsonfile.MarshalCompactASCII(want)
	if string(profile) != string(wantText) {
		t.Errorf("CanonicalProfileSettings = %s, Python %s", profile, wantText)
	}
}

func TestRuleKeysAreUnique(t *testing.T) {
	seen := map[string]bool{}
	for _, r := range Rules() {
		if seen[r.Key] {
			t.Errorf("duplicate rule key %q", r.Key)
		}
		seen[r.Key] = true
	}
}

func TestTierInvariants(t *testing.T) {
	for _, r := range Rules() {
		switch r.Tier {
		case TierHardDeny:
			if len(r.HookPatterns) == 0 || r.MainAdvice == "" || len(r.AskRules) != 0 {
				t.Errorf("hard-deny rule %s: patterns %v, main advice %q, ask rules %v", r.Key, r.HookPatterns, r.MainAdvice, r.AskRules)
			}
			if !strings.HasSuffix(r.SubagentAdvice, " "+SubagentHardDenySuffix) {
				t.Errorf("hard-deny rule %s: subagent advice does not end with the suffix: %q", r.Key, r.SubagentAdvice)
			}
		case TierEscalate:
			if len(r.HookPatterns) == 0 || r.MainAdvice != "" || len(r.DenyRules) != 0 {
				t.Errorf("escalate rule %s: patterns %v, main advice %q, deny rules %v", r.Key, r.HookPatterns, r.MainAdvice, r.DenyRules)
			}
			if !strings.HasSuffix(r.SubagentAdvice, EscalateTail) {
				t.Errorf("escalate rule %s: subagent advice does not end with the tail", r.Key)
			}
		case TierAdvise:
			if len(r.HookPatterns) == 0 || r.MainAdvice == "" || len(r.DenyRules)+len(r.AskRules) != 0 || r.Coverage != CoverageNotApplicable {
				t.Errorf("advise rule %s is malformed", r.Key)
			}
		case TierAsk:
			if len(r.HookPatterns) != 0 || len(r.AskRules) == 0 || r.MainAdvice != "" || r.SubagentAdvice != "" || r.Coverage != CoverageNotApplicable {
				t.Errorf("ask rule %s is malformed", r.Key)
			}
		}
		for _, text := range []string{r.MainAdvice, r.SubagentAdvice} {
			if strings.Contains(text, "\n") {
				t.Errorf("rule %s advice holds a newline", r.Key)
			}
		}
		hookBacked := r.Tier == TierHardDeny || r.Tier == TierEscalate
		if hookBacked && r.Coverage == CoverageNotApplicable {
			t.Errorf("rule %s has no settings coverage", r.Key)
		}
		if (r.Coverage == CoveragePartial || r.Coverage == CoverageNone) && r.CoverageReason == "" {
			t.Errorf("rule %s with partial or no coverage gives no reason", r.Key)
		}
		if hookBacked && (r.Coverage == CoverageNone) != (len(r.DenyRules)+len(r.AskRules) == 0) {
			t.Errorf("rule %s: coverage none must mean no settings rules", r.Key)
		}
	}
}

func TestDisallowedToolsAreUniqueSortedAndExplained(t *testing.T) {
	names := DisallowedToolNames()
	if !sort.StringsAreSorted(names) {
		t.Errorf("not alphabetical: %v", names)
	}
	seen := map[string]bool{}
	for _, e := range DisallowedToolEntries() {
		if e.Name == "" || e.Why == "" {
			t.Errorf("entry %+v lacks a name or a reason", e)
		}
		if seen[e.Name] {
			t.Errorf("duplicate %s", e.Name)
		}
		seen[e.Name] = true
	}
}

func TestModelFunctionsReturnFreshValues(t *testing.T) {
	Rules()[0].Key = "changed"
	if Rules()[0].Key == "changed" {
		t.Error("Rules shares its result")
	}
	deny := CanonicalDenyRules()
	deny[0] = "changed"
	if CanonicalDenyRules()[0] == "changed" {
		t.Error("CanonicalDenyRules shares its result")
	}
	s := CanonicalSharedSettings("/s")
	s.Set("hooks", nil)
	if v, _ := CanonicalSharedSettings("/s").Get("hooks"); v == nil {
		t.Error("CanonicalSharedSettings shares its result")
	}
}

func TestCanonicalHookEntryCarriesTheWiringsOptions(t *testing.T) {
	for _, w := range ExpectedHookWirings() {
		e := CanonicalHookEntry("/s", w)
		if v, _ := e.Get("command"); v != "/s/"+w.Script {
			t.Errorf("%s: command %v", w.Script, v)
		}
		_, async := e.Get("asyncRewake")
		if async != w.Options.AsyncRewake {
			t.Errorf("%s: asyncRewake present %v", w.Script, async)
		}
	}
}
