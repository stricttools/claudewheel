package health

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/tokens"
)

// tokens checks that each managed profile that holds credentials or a token
// holds a stored token of its own. A profile with neither is new and not yet
// authenticated, and is not reported.
func (c *checker) tokens() Result {
	const label = "tokens"
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	managed := c.managed()
	if len(managed) == 0 {
		return ok(label, "no profiles found")
	}
	var missing, unreadable []string
	for _, p := range managed {
		_, hasToken, err := c.store.Data(p.Name).Token()
		if err != nil {
			unreadable = append(unreadable, fmt.Sprintf("%s: %v", p.Name, err))
			continue
		}
		if !p.HasCredentials && !p.HasToken {
			continue
		}
		if !hasToken {
			missing = append(missing, p.Name)
		}
	}
	if len(unreadable) > 0 {
		return warn(label, strings.Join(unreadable, "; "))
	}
	if len(missing) > 0 {
		return warn(label, "missing tokens: "+strings.Join(missing, ", "))
	}
	return ok(label, fmt.Sprintf("all %d profiles OK", len(managed)))
}

// tokenExpiry reports managed profiles whose token expires within 30 days.
// A token whose expiry is not known is never reported as expiring.
func (c *checker) tokenExpiry() Result {
	const label = "token-expiry"
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	var expiring, unreadable []string
	minRemaining := -1
	seen := false
	for _, p := range c.managed() {
		exp, found, err := c.store.Data(p.Name).Expiry(c.in.Today)
		if err != nil {
			unreadable = append(unreadable, fmt.Sprintf("%s: %v", p.Name, err))
			continue
		}
		if !found || exp.RemainingDays == nil {
			continue
		}
		remaining := *exp.RemainingDays
		if !seen || remaining < minRemaining {
			minRemaining = remaining
			seen = true
		}
		if remaining < 30 {
			expiring = append(expiring, fmt.Sprintf("%s (~%dd)", p.Name, max(0, remaining)))
		}
	}
	if len(unreadable) > 0 {
		return warn(label, strings.Join(unreadable, "; "))
	}
	if len(expiring) > 0 {
		return warn(label, "expiring soon: "+strings.Join(expiring, ", ")+" — run claude setup-token")
	}
	if !seen {
		return ok(label, "no stored tokens")
	}
	return ok(label, fmt.Sprintf("~%d days remaining", minRemaining))
}

// authShadow reports profiles whose session credentials in
// .credentials.json shadow their stored long-lived token.
func (c *checker) authShadow() Result {
	const label = "auth-shadow"
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	if len(c.all) == 0 {
		return ok(label, "no profiles found")
	}
	var shadowed []string
	for _, p := range c.all {
		if c.store.DetectAuthShadow(p.Name) {
			shadowed = append(shadowed, p.Name)
		}
	}
	if len(shadowed) > 0 {
		return warn(label, "shadowed: "+strings.Join(shadowed, ", ")+" — session credentials override long-lived tokens")
	}
	return ok(label, "no auth shadow detected")
}

// orphanProfiles reports directories under the profiles directory that are
// neither discovered profiles nor listed in options.json's profile segment,
// naming the broken links inside each.
func (c *checker) orphanProfiles() Result {
	const label = "orphan-profiles"
	dir := c.in.Workspace.ProfilesDir()
	isDir, err := pathstat.IsDir(dir)
	if err != nil {
		return failed(label, err)
	}
	if !isDir {
		return ok(label, "no profiles dir found")
	}
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	known := map[string]bool{}
	for _, p := range c.all {
		known[p.Name] = true
	}
	options, err := appconfig.ReadOptions(c.in.Workspace)
	if err != nil {
		return failed(label, err)
	}
	if seg, present := options[appconfig.SegmentKeyProfile]; present {
		for _, name := range seg.Values {
			known[name] = true
		}
		for _, name := range seg.Pinned {
			known[name] = true
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return failed(label, err)
	}
	var orphans []string
	for _, e := range entries {
		path := filepath.Join(dir, e.Name())
		entryIsDir, err := pathstat.IsDir(path)
		if err != nil {
			return failed(label, err)
		}
		if !entryIsDir || known[e.Name()] {
			continue
		}
		broken, err := brokenLinks(path)
		if err != nil {
			return failed(label, err)
		}
		if len(broken) > 0 {
			orphans = append(orphans, fmt.Sprintf("%s (broken symlinks: %s)", e.Name(), strings.Join(broken, ", ")))
		} else {
			orphans = append(orphans, e.Name())
		}
	}
	if len(orphans) > 0 {
		return warn(label, "orphans: "+strings.Join(orphans, ", "))
	}
	return ok(label, "no orphan dirs found")
}

// brokenLinks names the symbolic links directly inside dir whose target does
// not exist.
func brokenLinks(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var broken []string
	for _, e := range entries {
		if e.Type()&fs.ModeSymlink == 0 {
			continue
		}
		exists, err := pathstat.Exists(filepath.Join(dir, e.Name()))
		if err != nil {
			return nil, err
		}
		if !exists {
			broken = append(broken, e.Name())
		}
	}
	return broken, nil
}

// modeIssue returns a description when path exists with a permission mode
// other than want, "" when it matches or does not exist.
func modeIssue(path, shown string, want fs.FileMode) (string, error) {
	info, err := os.Stat(path)
	if pathstat.NotFoundOrParentNotDirectory(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if mode := info.Mode().Perm(); mode != want {
		return fmt.Sprintf("%s is 0o%o", shown, mode), nil
	}
	return "", nil
}

// filePermissions checks that every profile's .credentials.json and token
// file are 0600 and its claudewheel data directory 0700: a readable
// directory exposes the token file's name, size, and modification time.
func (c *checker) filePermissions() Result {
	const label = "file-perms"
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	var issues []string
	for _, p := range c.all {
		data := c.store.Data(p.Name)
		dataIsDir, err := pathstat.IsDir(data.DataDir())
		if err != nil {
			return failed(label, err)
		}
		checks := []struct {
			path, shown string
			want        fs.FileMode
			applies     bool
		}{
			{filepath.Join(p.Path, profiles.CredentialsFileName), p.Name + "/" + profiles.CredentialsFileName, 0o600, true},
			{data.DataDir(), p.Name + "/" + tokens.DataDirName, tokens.DataDirMode, dataIsDir},
			{data.TokenFile(), p.Name + "/" + tokens.DataDirName + "/" + tokens.TokenFileName, tokens.TokenFileMode, true},
		}
		for _, ch := range checks {
			if !ch.applies {
				continue
			}
			issue, err := modeIssue(ch.path, ch.shown, ch.want)
			if err != nil {
				return failed(label, err)
			}
			if issue != "" {
				issues = append(issues, issue)
			}
		}
	}
	if len(issues) > 0 {
		return warn(label, strings.Join(issues, "; "))
	}
	return ok(label, "all sensitive files and dirs locked down")
}
