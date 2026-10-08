package guardrail

import (
	"os"
	"strings"
	"testing"
)

const guardrailsPage = "../../.strictmetadata/docs/guardrails.md"

// The guardrails page's generated tables must equal what the model renders.
func TestGuardrailsPageTablesMatchTheModel(t *testing.T) {
	data, err := os.ReadFile(guardrailsPage)
	if err != nil {
		t.Fatal(err)
	}
	want, err := RewriteDocTables(string(data))
	if err != nil {
		t.Fatal(err)
	}
	if want != string(data) {
		t.Fatalf("%s: the generated tables differ from the guardrail model; run scripts/gen-guardrail-docs", guardrailsPage)
	}
}

func TestRewriteDocTablesRefusesAMissingMarker(t *testing.T) {
	if _, err := RewriteDocTables("no markers here\n"); err == nil || !strings.Contains(err.Error(), "found 0") {
		t.Fatalf("err = %v", err)
	}
}

func TestRuleTableEscapesPipes(t *testing.T) {
	got := markdownTable([]string{"A"}, [][]string{{"x|y\nz"}})
	if got != "| A |\n| --- |\n| x\\|y z |" {
		t.Fatalf("got %q", got)
	}
}
