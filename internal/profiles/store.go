// Package profiles is the profile store: it enumerates, resolves, creates,
// deletes, and renames claudewheel profiles, resolves a profile to the
// environment a launch gives Claude Code, and holds the operations on one
// profile: auth-shadow repair, plan declaration, the inspection report, the
// plugin tree, permission rules, and the processes holding it.
//
// A profile is a Claude Code config directory under <root>/profiles/<name>;
// the name "default" is Claude Code's own ~/.claude, which claudewheel never
// creates, renames, or deletes. Profiles are discovered from directories,
// never from a persisted list.
package profiles

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/archiver"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/tokens"
	"github.com/stricttools/claudewheel/internal/workspace"
)

const (
	// DefaultName is Claude Code's built-in ~/.claude, inspectable like any
	// profile but never created, renamed, or deleted by claudewheel.
	DefaultName = "default"
	// Segment is the options.json segment profiles are registered under.
	Segment = "profile"
	// CredentialsFileName is Claude Code's credentials file in a profile.
	CredentialsFileName = ".credentials.json"
	// SettingsFileName is Claude Code's settings file in a profile.
	SettingsFileName = "settings.json"
	// GlobalConfigName is Claude Code's global config file, which lives
	// inside the profile directory because CLAUDE_CONFIG_DIR points there.
	GlobalConfigName = ".claude.json"
	// SkillsLinkName is the profile's link to the shared skills directory.
	SkillsLinkName = "skills"
)

// ReservedNames returns the names that are not claudewheel profiles.
func ReservedNames() []string {
	return []string{DefaultName}
}

// ReservedReason says why name cannot be created, renamed, or deleted, and
// reports false when it can. It names no command: there is none that
// deletes ~/.claude.
func ReservedReason(name string) (string, bool) {
	if !slices.Contains(ReservedNames(), name) {
		return "", false
	}
	return fmt.Sprintf("'%s' is Claude Code's built-in ~/.claude, not a claudewheel profile. "+
		"Claude Code manages it; claudewheel neither creates, renames nor deletes it.", name), true
}

// namePattern is the charset of a new profile name.
var namePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)

// CheckNewName refuses a name a new or renamed profile cannot take: one
// outside lowercase letters, digits, and hyphens (starting with a letter or
// digit), or a reserved name.
func CheckNewName(name string) error {
	if !namePattern.MatchString(name) {
		return fmt.Errorf("invalid profile name %q: use lowercase letters, digits, hyphens only (must start with letter or digit)", name)
	}
	if slices.Contains(ReservedNames(), name) {
		return fmt.Errorf("'%s' is a reserved name", name)
	}
	return nil
}

// Store is the profile store of one workspace. Building one touches nothing.
type Store struct {
	ws workspace.Workspace
}

// New returns the profile store of ws.
func New(ws workspace.Workspace) Store {
	return Store{ws: ws}
}

// Workspace is the store's workspace.
func (s Store) Workspace() workspace.Workspace { return s.ws }

// PathFor maps a profile name to its config directory: DefaultName to
// ~/.claude, every other name to <root>/profiles/<name>.
func (s Store) PathFor(name string) string {
	if name == DefaultName {
		return s.ws.ClaudeDir()
	}
	return filepath.Join(s.ws.ProfilesDir(), name)
}

// Data is the claudewheel data (token entry and plan) of profile name.
func (s Store) Data(name string) tokens.Store {
	return tokens.NewStore(s.PathFor(name))
}

// Profile is one discovered profile.
type Profile struct {
	Name string
	// Path is the profile's config directory (its CLAUDE_CONFIG_DIR).
	Path           string
	HasCredentials bool
	// HasToken reports whether the profile's token entry holds a token.
	HasToken bool
}

// record is a profile the directory layout reveals, before any token file
// is read.
type record struct {
	name           string
	path           string
	hasCredentials bool
}

// records applies the discovery rules without opening a token file:
// ~/.claude is "default" whenever it is a directory (Claude Code manages
// it, so its credentials may live elsewhere), and each directory under
// profiles/ is a profile when it holds .credentials.json, settings.json, or
// claudewheel's data directory. A leftover rename breadcrumb is an error.
func (s Store) records() ([]record, error) {
	if err := s.CheckPendingRenames(); err != nil {
		return nil, err
	}
	var out []record
	claudeDir := s.ws.ClaudeDir()
	isDefault, err := isDir(claudeDir)
	if err != nil {
		return nil, err
	}
	if isDefault {
		creds, err := exists(filepath.Join(claudeDir, CredentialsFileName))
		if err != nil {
			return nil, err
		}
		out = append(out, record{name: DefaultName, path: claudeDir, hasCredentials: creds})
	}
	entries, err := readDirIfExists(s.ws.ProfilesDir())
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		path := filepath.Join(s.ws.ProfilesDir(), e.Name())
		dir, err := isDir(path)
		if err != nil {
			return nil, err
		}
		if !dir {
			continue
		}
		creds, err := exists(filepath.Join(path, CredentialsFileName))
		if err != nil {
			return nil, err
		}
		settings, err := exists(filepath.Join(path, SettingsFileName))
		if err != nil {
			return nil, err
		}
		data, err := isDir(filepath.Join(path, tokens.DataDirName))
		if err != nil {
			return nil, err
		}
		if creds || settings || data {
			out = append(out, record{name: e.Name(), path: path, hasCredentials: creds})
		}
	}
	return out, nil
}

// recordFor returns the record of name, reporting false when no profile
// answers to it.
func (s Store) recordFor(name string) (record, bool, error) {
	recs, err := s.records()
	if err != nil {
		return record{}, false, err
	}
	for _, r := range recs {
		if r.name == name {
			return r, true, nil
		}
	}
	return record{}, false, nil
}

// Names returns every discovered profile name, sorted, reading no token
// file.
func (s Store) Names() ([]string, error) {
	recs, err := s.records()
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(recs))
	for _, r := range recs {
		names = append(names, r.name)
	}
	sort.Strings(names)
	return names, nil
}

// CorruptTokenPolicy says what enumeration does with a token file that
// cannot be read. Its zero value is invalid: every caller chooses.
type CorruptTokenPolicy int

const (
	// CorruptTokenError makes an unreadable token file an error.
	CorruptTokenError CorruptTokenPolicy = iota + 1
	// CorruptTokenAsNone reads an unreadable token file as no token, for
	// maintenance that touches settings rather than tokens. Each profile is
	// judged on its own file.
	CorruptTokenAsNone
)

// Enumerate returns every profile sorted by name; an unreadable token file
// is an error naming it.
func (s Store) Enumerate() ([]Profile, error) {
	return s.Discover(CorruptTokenError)
}

// Discover returns every profile sorted by name, applying policy to each
// profile's token file.
func (s Store) Discover(policy CorruptTokenPolicy) ([]Profile, error) {
	if policy != CorruptTokenError && policy != CorruptTokenAsNone {
		return nil, fmt.Errorf("profiles.Discover: invalid corrupt-token policy %d", policy)
	}
	recs, err := s.records()
	if err != nil {
		return nil, err
	}
	out := make([]Profile, 0, len(recs))
	for _, r := range recs {
		_, hasToken, err := tokens.NewStore(r.path).Token()
		if err != nil {
			var corrupt *tokens.StoreError
			if policy == CorruptTokenAsNone && errors.As(err, &corrupt) {
				hasToken = false
			} else {
				return nil, err
			}
		}
		out = append(out, Profile{Name: r.name, Path: r.path, HasCredentials: r.hasCredentials, HasToken: hasToken})
	}
	slices.SortStableFunc(out, func(a, b Profile) int { return strings.Compare(a.Name, b.Name) })
	return out, nil
}

// Get returns the profile name, reporting false when none answers to it.
// Only name's own token file is read.
func (s Store) Get(name string) (Profile, bool, error) {
	r, found, err := s.recordFor(name)
	if err != nil || !found {
		return Profile{}, false, err
	}
	_, hasToken, err := tokens.NewStore(r.path).Token()
	if err != nil {
		return Profile{}, false, err
	}
	return Profile{Name: r.name, Path: r.path, HasCredentials: r.hasCredentials, HasToken: hasToken}, true, nil
}

// SharedState is the state of one shared-store entry in a profile.
type SharedState string

const (
	// SharedIntact is a link resolving to the shared-store directory.
	SharedIntact SharedState = "intact"
	// SharedWrongTarget is a link resolving elsewhere.
	SharedWrongTarget SharedState = "wrong-target"
	// SharedRealDir is real data (not a link) at the shared name.
	SharedRealDir SharedState = "real-dir"
	// SharedMissing is nothing at the shared name.
	SharedMissing SharedState = "missing"
)

// SharedEntry is one shared-store name in a profile and its state.
type SharedEntry struct {
	Name  string
	State SharedState
}

// ClassifySharedDirs returns the state of each shared-store entry in
// profile name: the shared subdirectories in order, then skills.
func (s Store) ClassifySharedDirs(name string) ([]SharedEntry, error) {
	shared := s.ws.Shared()
	profileDir := s.PathFor(name)
	type pair struct{ name, target string }
	var pairs []pair
	for _, sub := range workspace.SharedSubdirs() {
		pairs = append(pairs, pair{sub, shared.Subdir(sub)})
	}
	pairs = append(pairs, pair{SkillsLinkName, shared.SkillsDir()})
	out := make([]SharedEntry, 0, len(pairs))
	for _, p := range pairs {
		link := filepath.Join(profileDir, p.name)
		info, err := os.Lstat(link)
		switch {
		case errors.Is(err, fs.ErrNotExist):
			out = append(out, SharedEntry{p.name, SharedMissing})
			continue
		case err != nil:
			return nil, err
		}
		if info.Mode()&fs.ModeSymlink == 0 {
			out = append(out, SharedEntry{p.name, SharedRealDir})
			continue
		}
		got, err := resolveLenient(link)
		if err != nil {
			return nil, err
		}
		want, err := resolveLenient(p.target)
		if err != nil {
			return nil, err
		}
		state := SharedWrongTarget
		if got == want {
			state = SharedIntact
		}
		out = append(out, SharedEntry{p.name, state})
	}
	return out, nil
}

// DirSurvey is what a profile directory holds at the top level, read before
// anything is removed. Links are counted as links and never followed.
type DirSurvey struct {
	Symlinks     int
	RealChildren int
	Names        []string
}

// SurveyProfileDir counts what profile name's directory holds, changing
// nothing. A missing directory holds nothing.
func (s Store) SurveyProfileDir(name string) (DirSurvey, error) {
	dir := s.PathFor(name)
	isDirectory, err := isDir(dir)
	if err != nil || !isDirectory {
		return DirSurvey{}, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return DirSurvey{}, err
	}
	var survey DirSurvey
	for _, e := range entries {
		survey.Names = append(survey.Names, e.Name())
		if e.Type()&fs.ModeSymlink != 0 {
			survey.Symlinks++
		} else {
			survey.RealChildren++
		}
	}
	return survey, nil
}

// CreateOptions are the choices Create makes on the caller's behalf. Both
// are stated by every caller.
type CreateOptions struct {
	// SetOnboarding writes hasCompletedOnboarding into .claude.json, so
	// Claude Code skips its login screen under an injected token.
	SetOnboarding bool
	// SymlinkShared links the shared-store subdirectories and skills into
	// the profile; without it the profile gets plain directories.
	SymlinkShared bool
}

// Create makes profile name from its finished settings (an ordered tree):
// the directory, settings.json, the onboarding flag, the shared-store
// links, and its pinned registration in options.json. Any failure after the
// directory was made removes the directory again (links unlinked, never
// followed); that removal is not an archival, since nothing in it is the
// user's data yet.
func (s Store) Create(fx *effects.FX, name string, settings *jsonfile.Object, opts CreateOptions) (Profile, error) {
	if err := CheckNewName(name); err != nil {
		return Profile{}, err
	}
	if err := s.CheckPendingRenames(); err != nil {
		return Profile{}, err
	}
	target := s.PathFor(name)
	present, err := lexists(target)
	if err != nil {
		return Profile{}, err
	}
	if present {
		return Profile{}, fmt.Errorf("profile directory already exists: %s", target)
	}
	if err := fx.MkdirNew(target); err != nil {
		return Profile{}, err
	}
	p, err := s.fill(fx, name, target, settings, opts)
	if err != nil {
		if discardErr := discardPartialDir(fx, target); discardErr != nil {
			return Profile{}, errors.Join(err, fmt.Errorf("removing the partly created %s: %w", target, discardErr))
		}
		return Profile{}, err
	}
	return p, nil
}

// fill writes everything of a new profile into its fresh directory target.
func (s Store) fill(fx *effects.FX, name, target string, settings *jsonfile.Object, opts CreateOptions) (Profile, error) {
	data, err := jsonfile.MarshalIndented(settings)
	if err != nil {
		return Profile{}, fmt.Errorf("encoding settings.json: %w", err)
	}
	if err := fx.WriteFileAtomic(filepath.Join(target, SettingsFileName), data); err != nil {
		return Profile{}, err
	}
	if opts.SetOnboarding {
		// The directory is fresh, so there is no .claude.json to merge into.
		global := jsonfile.NewObject()
		global.Set("hasCompletedOnboarding", true)
		text, err := jsonfile.MarshalIndented(global)
		if err != nil {
			return Profile{}, err
		}
		if err := fx.WriteFileAtomic(filepath.Join(target, GlobalConfigName), text); err != nil {
			return Profile{}, err
		}
	}
	if opts.SymlinkShared {
		shared := s.ws.Shared()
		for _, sub := range workspace.SharedSubdirs() {
			link := filepath.Join(target, sub)
			present, err := lexists(link)
			if err != nil {
				return Profile{}, err
			}
			if present {
				continue
			}
			if err := fx.MkdirAll(shared.Subdir(sub)); err != nil {
				return Profile{}, err
			}
			if err := fx.Symlink(link, shared.Subdir(sub)); err != nil {
				return Profile{}, err
			}
		}
		skillsLink := filepath.Join(target, SkillsLinkName)
		skills, err := isDir(shared.SkillsDir())
		if err != nil {
			return Profile{}, err
		}
		linked, err := lexists(skillsLink)
		if err != nil {
			return Profile{}, err
		}
		if skills && !linked {
			if err := fx.Symlink(skillsLink, shared.SkillsDir()); err != nil {
				return Profile{}, err
			}
		}
	}
	if _, err := appconfig.AddPinned(fx, s.ws, Segment, name); err != nil {
		return Profile{}, err
	}
	creds, err := exists(filepath.Join(target, CredentialsFileName))
	if err != nil {
		return Profile{}, err
	}
	_, hasToken, err := s.Data(name).Token()
	if err != nil {
		return Profile{}, err
	}
	return Profile{Name: name, Path: target, HasCredentials: creds, HasToken: hasToken}, nil
}

// discardPartialDir removes the debris of a failed Create: links are
// unlinked without being followed, so a shared-store link made a moment ago
// cannot take the store with it.
func discardPartialDir(fx *effects.FX, dir string) error {
	isDirectory, err := isDir(dir)
	if err != nil || !isDirectory {
		return err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		child := filepath.Join(dir, e.Name())
		switch {
		case e.Type()&fs.ModeSymlink != 0:
			err = fx.Remove(child)
		case e.IsDir():
			err = fx.RemoveTree(child)
		default:
			err = fx.Remove(child)
		}
		if err != nil {
			return err
		}
	}
	return fx.RemoveEmptyDir(dir)
}

// Archiver archives a directory and removes it. Under --dry-run it records
// the invocation and returns a nil handle. *archiver.Tool is the
// implementation.
type Archiver interface {
	Archive(fx *effects.FX, path, description string) (*archiver.Handle, error)
}

// DeletionResult is a successful deletion. The counts come from a survey
// taken before anything was removed. Archive is the handle that restores
// the profile, nil under --dry-run or when there was no directory; it is
// reported to the user, never stored.
type DeletionResult struct {
	RemovedSymlinks    int
	RemovedReal        int
	RemovedFromOptions bool
	LastConfigPurged   bool
	Archive            *archiver.Handle
}

// DeletionBookkeepingError is a deletion whose archival succeeded and whose
// update of options.json or state.json did not. It carries the handle, so a
// successful archival always tells the caller how to restore.
type DeletionBookkeepingError struct {
	Name    string
	Archive *archiver.Handle
	Reason  error
}

func (e *DeletionBookkeepingError) Error() string {
	archived, restore := "removed", ""
	if e.Archive != nil {
		archived = fmt.Sprintf("archived as %s and removed", e.Archive.UUID)
		restore = fmt.Sprintf(" Restore it with: %s.", e.Archive.RestoreCommand())
	}
	return fmt.Sprintf("Profile '%s' was %s, but claudewheel could not update its own registration: %v. "+
		"options.json and state.json may still name it -- run `claudewheel profile delete %s` again to finish the cleanup, "+
		"which archives nothing because the directory is already gone.%s", e.Name, archived, e.Reason, e.Name, restore)
}

func (e *DeletionBookkeepingError) Unwrap() error { return e.Reason }

// Delete deletes profile name: its directory goes to arch, which archives
// and removes it, and only then is it removed from options.json and from
// state.json's last_config. Refusals come first and change nothing: a
// reserved name, a name neither registered nor on disk, and real data at a
// shared-store name unless allowDataDestruction. Whether a live session
// blocks the deletion is the caller's policy. An archival error leaves the
// profile on disk and registered; a failure after the archival is a
// *DeletionBookkeepingError carrying the handle.
func (s Store) Delete(fx *effects.FX, name string, arch Archiver, allowDataDestruction bool) (DeletionResult, error) {
	if reason, reserved := ReservedReason(name); reserved {
		return DeletionResult{}, errors.New(reason)
	}
	if arch == nil {
		return DeletionResult{}, errors.New("profiles.Delete: an archiver is required")
	}
	if err := s.CheckPendingRenames(); err != nil {
		return DeletionResult{}, err
	}
	opts, err := appconfig.ReadOptions(s.ws)
	if err != nil {
		return DeletionResult{}, err
	}
	seg := opts[Segment]
	registered := slices.Contains(seg.Values, name) || slices.Contains(seg.Pinned, name)
	dir := s.PathFor(name)
	dirExists, err := isDir(dir)
	if err != nil {
		return DeletionResult{}, err
	}
	if !registered && !dirExists {
		known := slices.Concat(seg.Values, seg.Pinned)
		sort.Strings(known)
		known = slices.Compact(known)
		list := "<none>"
		if len(known) > 0 {
			list = strings.Join(known, ", ")
		}
		return DeletionResult{}, fmt.Errorf("Profile '%s' is not registered in options.json and has no directory on disk. Known profiles: %s", name, list)
	}
	_, inMetadata := seg.MetadataFor(name)

	if dirExists {
		states, err := s.ClassifySharedDirs(name)
		if err != nil {
			return DeletionResult{}, err
		}
		var atRisk []string
		for _, st := range states {
			if st.State == SharedRealDir {
				atRisk = append(atRisk, st.Name)
			}
		}
		sort.Strings(atRisk)
		if len(atRisk) > 0 && !allowDataDestruction {
			return DeletionResult{}, fmt.Errorf("Profile '%s' holds REAL data (not symlinks) at: %s. "+
				"Deleting it would destroy that data; it is deleted only with data destruction allowed (--force-delete-data)",
				name, strings.Join(atRisk, ", "))
		}
	}

	survey, err := s.SurveyProfileDir(name)
	if err != nil {
		return DeletionResult{}, err
	}
	var handle *archiver.Handle
	if dirExists {
		handle, err = arch.Archive(fx, dir, fmt.Sprintf(
			"claudewheel profile delete '%s': the profile directory with its settings, credentials and stored OAuth token", name))
		if err != nil {
			return DeletionResult{}, err
		}
		if !fx.Previewing() {
			if err := checkGone(dir); err != nil {
				return DeletionResult{}, err
			}
		}
	}

	if _, err := appconfig.RemoveOptionValue(fx, s.ws, Segment, name); err != nil {
		return DeletionResult{}, &DeletionBookkeepingError{Name: name, Archive: handle, Reason: err}
	}
	purged, err := s.clearLastConfig(fx, name)
	if err != nil {
		return DeletionResult{}, &DeletionBookkeepingError{Name: name, Archive: handle, Reason: err}
	}
	return DeletionResult{
		RemovedSymlinks:    survey.Symlinks,
		RemovedReal:        survey.RealChildren,
		RemovedFromOptions: registered || inMetadata,
		LastConfigPurged:   purged,
		Archive:            handle,
	}, nil
}

// checkGone refuses a directory still standing after a reportedly
// successful archival, naming what is left in it.
func checkGone(dir string) error {
	present, err := lexists(dir)
	if err != nil || !present {
		return err
	}
	var leftovers []string
	if entries, err := os.ReadDir(dir); err == nil {
		for _, e := range entries {
			leftovers = append(leftovers, e.Name())
		}
	}
	listed := "<empty>"
	if len(leftovers) > 0 {
		listed = strings.Join(leftovers, ", ")
	}
	return fmt.Errorf("profile directory %s still exists after it was archived: %s", dir, listed)
}

// clearLastConfig drops last_config's profile from state.json when it names
// name, reporting whether it did.
func (s Store) clearLastConfig(fx *effects.FX, name string) (bool, error) {
	st, err := appconfig.ReadState(s.ws)
	if err != nil {
		return false, err
	}
	if st.LastConfig[Segment] != name {
		return false, nil
	}
	return true, appconfig.UpdateState(fx, s.ws, func(st *appconfig.State) error {
		delete(st.LastConfig, Segment)
		return nil
	})
}

// isDir reports whether path is a directory, following links. A missing
// path or a broken link is not one.
func isDir(path string) (bool, error) {
	info, err := os.Stat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return info.IsDir(), nil
}

// exists reports whether path exists, following links: a broken link does
// not.
func exists(path string) (bool, error) {
	_, err := os.Stat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// lexists reports whether anything, a broken link included, is at path.
func lexists(path string) (bool, error) {
	_, err := os.Lstat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// readDirIfExists lists dir sorted by name; a missing dir lists nothing.
func readDirIfExists(dir string) ([]os.DirEntry, error) {
	entries, err := os.ReadDir(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	return entries, err
}

// maxLinkHops bounds link chains, as the kernel's ELOOP limit does.
const maxLinkHops = 40

// resolveLenient resolves every link in p; when the chain ends at a missing
// path, that path (absolute and clean) is the answer, as Python's
// non-strict Path.resolve gives.
func resolveLenient(p string) (string, error) {
	p, err := filepath.Abs(p)
	if err != nil {
		return "", err
	}
	for hop := 0; hop < maxLinkHops; hop++ {
		resolved, err := filepath.EvalSymlinks(p)
		if err == nil {
			return resolved, nil
		}
		if !errors.Is(err, fs.ErrNotExist) {
			return "", err
		}
		target, err := os.Readlink(p)
		if err != nil {
			// p is missing, or is no link while a component above it is
			// missing: the chain ends here.
			return filepath.Clean(p), nil
		}
		if !filepath.IsAbs(target) {
			target = filepath.Join(filepath.Dir(p), target)
		}
		p = target
	}
	return "", fmt.Errorf("too many levels of symbolic links resolving %s", p)
}
