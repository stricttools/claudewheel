package profiles

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/tokens"
)

// permissionCategories are the permission arrays of a settings.json, in the
// order they are shown.
func permissionCategories() []string {
	return []string{"allow", "deny", "ask"}
}

// Report is everything the inspection of one profile learns.
type Report struct {
	Name      string
	ConfigDir string
	// Exists reports whether ConfigDir is a directory.
	Exists     bool
	Registered bool
	Pinned     bool
	// HasCredentials reports whether .credentials.json is present.
	HasCredentials bool
	// HasToken reports whether the token entry holds a token; TokenExpiry
	// is set only then.
	HasToken      bool
	TokenExpiry   *tokens.Expiry
	HasAuthShadow bool
	// RateLimitTier and SubscriptionType are the token entry's plan fields
	// as stored, empty when absent.
	RateLimitTier    string
	SubscriptionType string
	SharedDirs       []SharedEntry
	// Danger reports real data at a shared-store name.
	Danger        bool
	SettingsFound bool
	// PermissionCounts holds the length of each permission array by
	// category, set when SettingsFound.
	PermissionCounts map[string]int
	// The three settings values shown, nil when absent or null.
	AwaySummaryEnabled  jsonfile.Value
	CleanupPeriodDays   jsonfile.Value
	AutoMemoryEnabled   jsonfile.Value
	ActiveSessions      int
	InteractiveSessions int
	DiskUsageBytes      int64
}

// GatherReport inspects profile name as of today. An unknown name is not an
// error: the caller decides from Exists, Registered, Pinned, and HasToken.
// A corrupt token entry or settings.json is an error.
func (s Store) GatherReport(name string, today time.Time) (Report, error) {
	if err := s.CheckPendingRenames(); err != nil {
		return Report{}, err
	}
	r := Report{Name: name, ConfigDir: s.PathFor(name)}
	var err error
	if r.Exists, err = pathstat.IsDir(r.ConfigDir); err != nil {
		return Report{}, err
	}
	opts, err := appconfig.ReadOptions(s.ws)
	if err != nil {
		return Report{}, err
	}
	seg := opts[appconfig.SegmentKeyProfile]
	r.Registered = slices.Contains(seg.Values, name)
	r.Pinned = slices.Contains(seg.Pinned, name)

	entry, _, err := s.Data(name).Load()
	if err != nil {
		return Report{}, err
	}
	if _, ok := entry.TokenValue(); ok {
		r.HasToken = true
		exp, err := entry.Expiry(today)
		if err != nil {
			return Report{}, fmt.Errorf("%s: %w", s.Data(name).TokenFile(), err)
		}
		r.TokenExpiry = &exp
	}
	r.RateLimitTier, r.SubscriptionType = entry.Tier()
	if r.HasCredentials, err = pathstat.Exists(filepath.Join(r.ConfigDir, CredentialsFileName)); err != nil {
		return Report{}, err
	}

	if r.Exists {
		if r.SharedDirs, err = s.ClassifySharedDirs(name); err != nil {
			return Report{}, err
		}
		for _, d := range r.SharedDirs {
			if d.State == SharedRealDir {
				r.Danger = true
			}
		}
		live, err := sessions.LiveRecords(r.ConfigDir)
		if err != nil {
			return Report{}, err
		}
		r.ActiveSessions = len(live)
		for _, rec := range live {
			if rec.Interactive() {
				r.InteractiveSessions++
			}
		}
		r.DiskUsageBytes = diskUsage(r.ConfigDir)
		if err := r.readSettings(); err != nil {
			return Report{}, err
		}
	}
	r.HasAuthShadow = s.DetectAuthShadow(name)
	return r, nil
}

// readSettings summarizes settings.json into r; a missing file leaves
// SettingsFound false.
func (r *Report) readSettings() error {
	path := filepath.Join(r.ConfigDir, SettingsFileName)
	settings, found, err := readSettingsFile(path)
	if err != nil || !found {
		return err
	}
	r.SettingsFound = true
	r.PermissionCounts = map[string]int{}
	for _, c := range permissionCategories() {
		rules, err := permissionRules(settings, c)
		if err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
		r.PermissionCounts[c] = len(rules)
	}
	r.AwaySummaryEnabled, _ = settings.Get("awaySummaryEnabled")
	r.CleanupPeriodDays, _ = settings.Get("cleanupPeriodDays")
	r.AutoMemoryEnabled, _ = settings.Get("autoMemoryEnabled")
	return nil
}

// readSettingsFile reads a settings.json as an ordered tree, reporting
// false when it does not exist.
func readSettingsFile(path string) (*jsonfile.Object, bool, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	settings, err := jsonfile.DecodeObject(data)
	if err != nil {
		return nil, false, fmt.Errorf("%s: %w", path, err)
	}
	return settings, true, nil
}

// diskUsage sums the sizes of the files under dir that are not links,
// descending into no linked directory (the shared-store links), so only data
// the profile owns is counted. Entries that cannot be read are skipped: the
// figure is an estimate for display.
func diskUsage(dir string) int64 {
	var total int64
	_ = filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if d != nil && d.IsDir() && path != dir {
				return fs.SkipDir
			}
			return nil
		}
		if d.IsDir() || d.Type()&fs.ModeSymlink != 0 {
			return nil
		}
		if info, err := d.Info(); err == nil {
			total += info.Size()
		}
		return nil
	})
	return total
}

// FormatSize renders a byte count as B (whole), KB, MB, or GB (one
// decimal), in steps of 1024.
func FormatSize(size int64) string {
	value := float64(size)
	for _, unit := range []string{"B", "KB", "MB"} {
		if value < 1024 {
			if unit == "B" {
				return fmt.Sprintf("%.0f %s", value, unit)
			}
			return fmt.Sprintf("%.1f %s", value, unit)
		}
		value /= 1024
	}
	return fmt.Sprintf("%.1f GB", value)
}

// displayValue renders a settings value as the report shows it: None for
// absent or null, True and False, numbers as written, strings bare, and
// anything else as compact JSON.
func displayValue(v jsonfile.Value) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case json.Number:
		return x.String()
	case string:
		return x
	}
	text, err := jsonfile.MarshalSpacedASCII(v)
	if err != nil {
		return "?"
	}
	return string(text)
}

// FormatReport renders r as plain display lines, for the TUI page and the
// CLI.
func FormatReport(r Report) []string {
	configLine := "Config dir: " + r.ConfigDir
	if !r.Exists {
		configLine += " (missing)"
	}
	lines := []string{"Profile: " + r.Name, configLine}
	if r.Name == DefaultName {
		lines = append(lines, "Mode: vanilla (managed by Claude Code, no cw guardrails)")
	}
	switch {
	case r.Registered && r.Pinned:
		lines = append(lines, "Registered: yes (pinned)")
	case r.Registered:
		lines = append(lines, "Registered: yes")
	case r.Pinned:
		lines = append(lines, "Registered: pinned only")
	default:
		lines = append(lines, "Registered: no")
	}
	creds := "missing"
	if r.HasCredentials {
		creds = "present"
	}
	lines = append(lines, "Credentials file: "+creds)

	switch {
	case !r.HasToken || r.TokenExpiry == nil:
		lines = append(lines, "Token: none")
	case r.TokenExpiry.RemainingDays == nil:
		lines = append(lines, "Token: present (expiry unknown (externally issued))")
	default:
		exp := r.TokenExpiry
		created, expires := "unknown", "unknown"
		if exp.Created != nil {
			created = exp.Created.Format("2006-01-02")
		}
		if exp.Expires != nil {
			expires = exp.Expires.Format("2006-01-02")
		}
		lines = append(lines, fmt.Sprintf("Token: present (created %s, expires %s, %d days left)", created, expires, *exp.RemainingDays))
	}
	if r.HasAuthShadow {
		lines = append(lines, "Auth shadow: yes (session credentials override token)")
	}

	// Keyed on the subscription type, which every plan carries; Pro and
	// Enterprise have no rate-limit tier.
	switch {
	case r.SubscriptionType != "" && r.RateLimitTier != "":
		lines = append(lines, fmt.Sprintf("Tier: %s (%s)", r.SubscriptionType, r.RateLimitTier))
	case r.SubscriptionType != "":
		lines = append(lines, "Tier: "+r.SubscriptionType)
	case r.RateLimitTier != "":
		lines = append(lines, "Tier: "+r.RateLimitTier)
	default:
		lines = append(lines, "Tier: unknown")
	}

	if len(r.SharedDirs) > 0 {
		intact := 0
		for _, d := range r.SharedDirs {
			if d.State == SharedIntact {
				intact++
			}
		}
		lines = append(lines, fmt.Sprintf("Shared dirs: %d/%d intact", intact, len(r.SharedDirs)))
		sorted := append([]SharedEntry(nil), r.SharedDirs...)
		slices.SortStableFunc(sorted, func(a, b SharedEntry) int { return strings.Compare(a.Name, b.Name) })
		for _, d := range sorted {
			if d.State != SharedIntact {
				lines = append(lines, fmt.Sprintf("  %s: %s", d.Name, d.State))
			}
		}
		if r.Danger {
			lines = append(lines, "  DANGER: real data at a shared-dir name (not a symlink)")
		}
	}

	if r.SettingsFound {
		c := r.PermissionCounts
		lines = append(lines,
			fmt.Sprintf("Permissions: %d allow, %d deny, %d ask", c["allow"], c["deny"], c["ask"]),
			"awaySummaryEnabled: "+displayValue(r.AwaySummaryEnabled),
			"cleanupPeriodDays: "+displayValue(r.CleanupPeriodDays),
			"autoMemoryEnabled: "+displayValue(r.AutoMemoryEnabled),
		)
	} else {
		lines = append(lines, "Settings: no settings.json")
	}
	lines = append(lines,
		fmt.Sprintf("Active sessions: %d (%d interactive)", r.ActiveSessions, r.InteractiveSessions),
		"Disk usage: "+FormatSize(r.DiskUsageBytes),
	)
	return lines
}
