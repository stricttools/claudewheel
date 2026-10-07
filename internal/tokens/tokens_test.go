package tokens

import (
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/testkit"
)

var leapDay = time.Date(2024, 2, 29, 15, 0, 0, 0, time.Local)

// The entries the Python's build_entry writes for each plan, on 2024-02-29.
func TestBuildEntryWritesThePythonsEntries(t *testing.T) {
	want := map[string][2]string{
		"max-20x":    {`"subscriptionType": "max",   "rateLimitTier": "default_claude_max_20x"`},
		"max-5x":     {`"subscriptionType": "max",   "rateLimitTier": "default_claude_max_5x"`},
		"pro":        {`"subscriptionType": "pro"`},
		"team":       {`"subscriptionType": "team",   "rateLimitTier": "default_claude_max_5x"`},
		"enterprise": {`"subscriptionType": "enterprise"`},
	}
	if strings.Join(PlanKeys(), ",") != "max-20x,max-5x,pro,team,enterprise" {
		t.Fatalf("plan keys %v", PlanKeys())
	}
	for _, key := range PlanKeys() {
		plan, err := PlanByKey(key)
		if err != nil {
			t.Fatal(err)
		}
		for disposition, dates := range map[ExpiryDisposition]string{
			ExpiryTTL:      `"expires_at": "2025-02-28"`,
			ExpiryNotKnown: `"expiry_unknown": true`,
		} {
			entry, err := BuildEntry("tok", disposition, plan, leapDay)
			if err != nil {
				t.Fatal(err)
			}
			got, _ := jsonfile.MarshalIndented(entry)
			expected := `{   "token": "tok",   "created": "2024-02-29",   ` + dates + `,   ` + want[key][0] + ` }`
			if oneLine := strings.ReplaceAll(strings.TrimSuffix(string(got), "\n"), "\n", " "); oneLine != expected {
				t.Errorf("%s:\n got: %s\nwant: %s", key, oneLine, expected)
			}
		}
	}
	if _, err := PlanByKey("platinum"); err == nil {
		t.Error("an unknown plan was found")
	}
	if _, err := BuildEntry("", ExpiryTTL, PlanTiers()[0], leapDay); err == nil {
		t.Error("an empty token was accepted")
	}
	if _, err := BuildEntry("tok", 0, PlanTiers()[0], leapDay); err == nil {
		t.Error("the zero disposition was accepted")
	}
}

func strp(s string) *string { return &s }

func TestExpiryMatchesThePython(t *testing.T) {
	today := time.Date(2024, 12, 31, 23, 0, 0, 0, time.Local)
	unknown := true
	cases := []struct {
		entry     Entry
		remaining *int
		expires   string
	}{
		{Entry{Created: strp("2024-02-29")}, intp(59), "2025-02-28"},
		{Entry{Created: strp("2024-02-29"), ExpiresAt: strp("2025-01-01")}, intp(1), "2025-01-01"},
		{Entry{ExpiryUnknown: &unknown}, nil, ""},
		{Entry{}, intp(365), ""},
	}
	for i, c := range cases {
		exp, err := c.entry.Expiry(today)
		if err != nil {
			t.Fatal(err)
		}
		if (exp.RemainingDays == nil) != (c.remaining == nil) || (c.remaining != nil && *exp.RemainingDays != *c.remaining) {
			t.Errorf("case %d: remaining %v", i, exp.RemainingDays)
		}
		if c.expires != "" && (exp.Expires == nil || exp.Expires.Format("2006-01-02") != c.expires) {
			t.Errorf("case %d: expires %v", i, exp.Expires)
		}
	}
	if _, err := (Entry{Created: strp("soon")}).Expiry(today); err == nil {
		t.Error("an unparseable date was read")
	}
}

func intp(i int) *int { return &i }

func TestPlanEnv(t *testing.T) {
	env, err := Entry{SubscriptionType: strp("max"), RateLimitTier: strp("default_claude_max_20x")}.PlanEnv("x")
	if err != nil || env["CLAUDE_CODE_SUBSCRIPTION_TYPE"] != "max" || env["CLAUDE_CODE_RATE_LIMIT_TIER"] != "default_claude_max_20x" || len(env) != 2 {
		t.Fatalf("%v %v", env, err)
	}
	if env, err := (Entry{}).PlanEnv("x"); err != nil || len(env) != 0 {
		t.Fatalf("empty: %v %v", env, err)
	}
	if _, err := (Entry{SubscriptionType: strp("gold")}).PlanEnv("x"); err == nil {
		t.Fatal("an unknown subscription type was accepted")
	}
}

func TestStore(t *testing.T) {
	dir := t.TempDir()
	s := NewStore(dir)
	if _, found, err := s.Load(); err != nil || found {
		t.Fatalf("missing file: %v %v", found, err)
	}
	plan, _ := PlanByKey("pro")
	if err := s.WriteToken(testkit.FX(), "sk-test", ExpiryTTL, plan, leapDay); err != nil {
		t.Fatal(err)
	}
	if token, ok, err := s.Token(); err != nil || !ok || token != "sk-test" {
		t.Fatalf("token %q %v %v", token, ok, err)
	}
	info := testkit.Stat(t, s.TokenFile())
	if info.Mode().Perm() != TokenFileMode {
		t.Errorf("token file mode %v", info.Mode())
	}
	if info := testkit.Stat(t, filepath.Dir(s.TokenFile())); info.Mode().Perm() != 0o700 {
		t.Errorf("data dir mode %v", info.Mode())
	}
	max, _ := PlanByKey("max-5x")
	if err := s.SetPlan(testkit.FX(), max); err != nil {
		t.Fatal(err)
	}
	entry, _, _ := s.Load()
	if *entry.SubscriptionType != "max" || *entry.Token != "sk-test" {
		t.Fatalf("after SetPlan: %+v", entry)
	}
	testkit.WriteFile(t, s.TokenFile(), `{"token": "x", "surprise": 1}`)
	if _, _, err := s.Load(); err == nil {
		t.Fatal("an unknown key was read")
	}
}
