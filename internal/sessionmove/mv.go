package sessionmove

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// ClaudeGlobalConfigName is the file in a profile's config directory where
// Claude Code keeps its per-project registry (projects{}) and
// githubRepoPaths.
const ClaudeGlobalConfigName = ".claude.json"

// MvOptions are the choices of one Mv run.
type MvOptions struct {
	// PostHoc skips the directory rename: the directory was already renamed,
	// and only the session data moves.
	PostHoc bool
	// Quiet suppresses the progress lines.
	Quiet bool
	// CountOnly runs every check and counts what the move would change,
	// issuing no effect at all, not even to a --dry-run record; the launch's
	// rename prompt states those counts before it asks.
	CountOnly bool
}

// MvResult counts what a move did, or would do.
type MvResult struct {
	DirsRenamed            int
	FilesRewritten         int
	LinesReplaced          int
	ProjectKeysUpdated     int
	GithubRepoPathsUpdated int
	PathsMigrated          int
	ProfilesScanned        int
}

// migration is one project path the move relabels, and its destination.
type migration struct {
	from, to string
}

// mover is one Mv run: its effects, its options, and its log.
type mover struct {
	fx   *effects.FX
	opts MvOptions
}

func (m *mover) log(msg string) {
	if !m.opts.Quiet {
		m.fx.Info("[mv] " + msg)
	}
}

// conditional reports whether progress is told as what would happen.
func (m *mover) conditional() bool {
	return m.opts.CountOnly || m.fx.Previewing()
}

// verb picks the conditional or the past form of a progress verb.
func (m *mover) verb(would, did string) string {
	if m.conditional() {
		return would
	}
	return did
}

// transcriptsToRewrite returns every JSONL file the rewrite visits in one
// migrated store dir, nested files (subagent transcripts) included;
// history.jsonl is skipped, being append-only and not needed to resume.
func transcriptsToRewrite(scanDir string) ([]string, error) {
	return walkMatching(scanDir, func(name string) bool {
		return strings.HasSuffix(name, ".jsonl") && name != "history.jsonl"
	})
}

// checkTranscriptsReadable refuses the move, before anything changes, when a
// transcript it would rewrite cannot be read, listing every such file.
func checkTranscriptsReadable(stores []string, migrations []migration) error {
	var failures []string
	for _, projects := range stores {
		for _, mg := range migrations {
			for _, name := range []string{workspace.EncodePath(mg.from), workspace.EncodePath(mg.to)} {
				scanDir := filepath.Join(projects, name)
				ok, err := isDir(scanDir)
				if err != nil {
					return err
				}
				if !ok {
					continue
				}
				paths, err := transcriptsToRewrite(scanDir)
				if err != nil {
					return err
				}
				for _, p := range paths {
					if _, err := readTranscript(p); err != nil {
						failures = append(failures, "  "+err.Error())
					}
				}
			}
		}
	}
	if len(failures) > 0 {
		return fmt.Errorf("cannot migrate, nothing was changed: session files the move must rewrite cannot be read:\n%s", strings.Join(failures, "\n"))
	}
	return nil
}

// checkMergesComplete refuses the move, before anything changes, when a
// merge would strand entries: a migrated path's store dir merged into an
// existing destination store dir must move every entry, so an entry name
// both hold is listed for every such pair.
func checkMergesComplete(stores []string, migrations []migration) error {
	var conflicts []string
	for _, projects := range stores {
		for _, mg := range migrations {
			oldProject := filepath.Join(projects, workspace.EncodePath(mg.from))
			newProject := filepath.Join(projects, workspace.EncodePath(mg.to))
			oldIsDir, err := isDir(oldProject)
			if err != nil {
				return err
			}
			newExists, err := exists(newProject)
			if err != nil {
				return err
			}
			if !oldIsDir || !newExists {
				continue
			}
			names, err := dirNames(oldProject)
			if err != nil {
				return err
			}
			var shared []string
			for _, name := range names {
				both, err := exists(filepath.Join(newProject, name))
				if err != nil {
					return err
				}
				if both {
					shared = append(shared, name)
				}
			}
			if len(shared) > 0 {
				conflicts = append(conflicts, fmt.Sprintf("  %s -> %s: both hold %s", oldProject, newProject, strings.Join(shared, ", ")))
			}
		}
	}
	if len(conflicts) > 0 {
		return fmt.Errorf("cannot migrate, nothing was changed: merging these store dirs would leave the old one non-empty:\n%s", strings.Join(conflicts, "\n"))
	}
	return nil
}

// pathFields are transcript fields whose string value (or list of string
// values) is one filesystem path. Such a value is rewritten only when it is
// the old path or lies under it, so /x/foo.bak in a path field stays when
// /x/foo moves. Every other string is free text, rewritten where the old
// path ends at a path boundary.
var pathFields = map[string]bool{
	"cwd":                 true,
	"file_path":           true,
	"filePath":            true,
	"filename":            true,
	"filesChanged":        true,
	"notebook_path":       true,
	"path":                true,
	"persistedOutputPath": true,
	"planFilePath":        true,
	"realParentDir":       true,
	"relocatedCwd":        true,
	"workingDirectory":    true,
}

// pathKeyedFields are objects whose keys are filesystem paths (a
// file-history snapshot maps each tracked file to its backup).
var pathKeyedFields = map[string]bool{
	"trackedFileBackups": true,
}

func rewritePathValue(value, oldPath, newPath string) string {
	if value == oldPath || strings.HasPrefix(value, oldPath+"/") {
		return newPath + value[len(oldPath):]
	}
	return value
}

// rewriteTranscriptPaths moves every path equal to or under oldPath to
// newPath in one transcript's text. Path fields (pathFields, and the keys of
// pathKeyedFields) are rewritten when they are oldPath or under it; every
// other string, keys included, where oldPath ends at a path boundary (not
// followed by a letter, digit, '_', or '-').
func rewriteTranscriptPaths(data []byte, oldPath, newPath string) ([]byte, int, error) {
	str := func(s, key string) string {
		if pathFields[key] {
			return rewritePathValue(s, oldPath, newPath)
		}
		out, _ := replaceBounded(s, oldPath, newPath)
		return out
	}
	objectKey := func(k, parent string) string {
		if pathKeyedFields[parent] {
			return rewritePathValue(k, oldPath, newPath)
		}
		out, _ := replaceBounded(k, oldPath, newPath)
		return out
	}
	return rewriteLines(data, oldPath, func(v jsonfile.Value) jsonfile.Value {
		return mapTree(v, "", str, objectKey)
	})
}

// rewriteTranscriptFile rewrites one JSONL file in place and returns the
// number of lines changed. A line that is not JSON (a live session's partial
// last line) is kept as it is.
func (m *mover) rewriteTranscriptFile(path, oldPath, newPath string) (int, error) {
	data, err := readTranscript(path)
	if err != nil {
		return 0, err
	}
	out, replaced, err := rewriteTranscriptPaths(data, oldPath, newPath)
	if err != nil {
		return 0, fmt.Errorf("cannot rewrite %s: %w", path, err)
	}
	if replaced == 0 {
		return 0, nil
	}
	if !m.opts.CountOnly {
		if err := m.fx.WriteFileAtomic(path, out); err != nil {
			return 0, err
		}
	}
	m.log(fmt.Sprintf("  %s %s (%d lines)", m.verb("would rewrite", "rewrote"), path, replaced))
	return replaced, nil
}

// planMigrations returns the ordered migrations: oldResolved itself and
// every descendant, each to newResolved plus its relative suffix, longest
// source first so a shorter prefix never runs before its own descendants.
func planMigrations(oldResolved, newResolved string, descendants map[string]bool) []migration {
	paths := []string{oldResolved}
	for d := range descendants {
		if d != oldResolved {
			paths = append(paths, d)
		}
	}
	sort.Slice(paths, func(i, j int) bool {
		li, lj := utf8.RuneCountInString(paths[i]), utf8.RuneCountInString(paths[j])
		if li != lj {
			return li > lj
		}
		return paths[i] < paths[j]
	})
	out := make([]migration, len(paths))
	for i, p := range paths {
		out[i] = migration{from: p, to: newResolved + p[len(oldResolved):]}
	}
	return out
}

// decodeRel returns every existing directory under root whose store-dir
// name is name, as paths relative to root. root is the moved tree as it
// exists on disk and rootReal the real path its contents are recorded
// under; a relative directory rel matches when
// EncodePath(rootReal + "/" + rel) is name. The encoding is lossy, so one
// name can match several directories; all are returned so the caller can
// detect the ambiguity. A directory that cannot be listed adds no match: it
// is one of three sources of proof, and the caller refuses a name none of
// them resolves.
func decodeRel(root, rootReal, name string) []string {
	limit := workspace.ProjectDirNameLimit
	head := name
	if len(head) > limit {
		head = head[:limit]
	}
	var matches []string
	var walk func(dir, rel string)
	walk = func(dir, rel string) {
		entries, err := subdirs(dir)
		if err != nil {
			return
		}
		for _, entry := range entries {
			entryRel := filepath.Base(entry)
			if rel != "" {
				entryRel = rel + "/" + entryRel
			}
			full := rootReal + "/" + entryRel
			if workspace.EncodePath(full) == name {
				matches = append(matches, entryRel)
			}
			// Descend only where a deeper path could still produce the name.
			deeper := workspace.EncodePathUntruncated(full) + "-"
			if len(deeper) > limit {
				deeper = deeper[:limit]
			}
			if strings.HasPrefix(head, deeper) {
				walk(entry, entryRel)
			}
		}
	}
	walk(root, "")
	return matches
}

// namesUnder returns a test for store-dir names that could belong to a path
// under oldResolved. A path under it encodes to
// EncodePathUntruncated(oldResolved) + "-" + ...; its store name keeps the
// first ProjectDirNameLimit characters of that. The test admits siblings
// that merely share the encoded prefix; resolution sorts them out.
func namesUnder(oldResolved string) func(string) bool {
	limit := workspace.ProjectDirNameLimit
	prefix := workspace.EncodePathUntruncated(oldResolved) + "-"
	if len(prefix) <= limit {
		return func(name string) bool { return strings.HasPrefix(name, prefix) }
	}
	cut := prefix[:limit]
	return func(name string) bool { return len(name) > limit && strings.HasPrefix(name, cut) }
}

// readClaudeJSON reads one profile's .claude.json; a file that cannot be
// read or parsed is an error, never a profile contributing nothing.
func readClaudeJSON(path string) (*jsonfile.Object, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	o, err := jsonfile.DecodeObject(data)
	if err != nil {
		return nil, fmt.Errorf("cannot parse %s: %w", path, err)
	}
	return o, nil
}

// claudeJSONFiles returns the .claude.json of every profile directory but
// the shared store, where one is a regular file.
func claudeJSONFiles(profileDirs []string, sharedDir string) ([]string, error) {
	var out []string
	for _, pdir := range profileDirs {
		if pdir == sharedDir {
			continue
		}
		path := filepath.Join(pdir, ClaudeGlobalConfigName)
		ok, err := isFile(path)
		if err != nil {
			return nil, err
		}
		if ok {
			out = append(out, path)
		}
	}
	return out, nil
}

// collectProjectKeys returns every projects{} key across the profiles'
// .claude.json files.
func collectProjectKeys(files []string) (map[string]bool, error) {
	keys := map[string]bool{}
	for _, path := range files {
		data, err := readClaudeJSON(path)
		if err != nil {
			return nil, err
		}
		if projects, ok := mustObject(data.Get("projects")); ok {
			for _, k := range projects.Keys() {
				keys[k] = true
			}
		}
	}
	return keys, nil
}

func mustObject(v jsonfile.Value, present bool) (*jsonfile.Object, bool) {
	if !present {
		return nil, false
	}
	o, ok := v.(*jsonfile.Object)
	return o, ok
}

func under(path, root string) bool {
	return path == root || strings.HasPrefix(path, root+"/")
}

// discoverDescendants returns every project path equal to or under
// oldResolved that has data: the projects{} keys under it, and the store
// dirs whose name could encode a path under it, each resolved back to real
// paths from all three proofs at once (registry keys encoding to its name,
// directories under the moved tree encoding to it, and the cwds its own
// sessions recorded encoding to it). One distinct path must result; a
// candidate resolving to a sibling that merely shares the encoded prefix is
// not part of the move, and one resolving to several paths or to none is an
// error listing every such candidate, returned before anything changes.
func discoverDescendants(stores []string, oldResolved, sourceRoot string, knownKeys map[string]bool) (map[string]bool, error) {
	descendants := map[string]bool{}
	for k := range knownKeys {
		if under(k, oldResolved) {
			descendants[k] = true
		}
	}

	oldEncoded := workspace.EncodePath(oldResolved)
	underOld := namesUnder(oldResolved)
	candidates := map[string][]string{}
	for _, projects := range stores {
		dirs, err := subdirs(projects)
		if err != nil {
			return nil, err
		}
		for _, entry := range dirs {
			name := filepath.Base(entry)
			if name != oldEncoded && underOld(name) {
				candidates[name] = append(candidates[name], entry)
			}
		}
	}
	names := make([]string, 0, len(candidates))
	for name := range candidates {
		names = append(names, name)
	}
	sort.Strings(names)

	var problems []string
	for _, cand := range names {
		storeDirs := candidates[cand]
		resolved := map[string]bool{}
		for k := range knownKeys {
			if workspace.EncodePath(k) == cand {
				resolved[k] = true
			}
		}
		for _, rel := range decodeRel(sourceRoot, oldResolved, cand) {
			resolved[oldResolved+"/"+rel] = true
		}
		for _, storeDir := range storeDirs {
			cwds, err := sessions.RecordedStoreCwds(storeDir)
			if err != nil {
				return nil, err
			}
			for _, c := range cwds {
				resolved[c] = true
			}
		}
		where := strings.Join(storeDirs, ", ")
		switch len(resolved) {
		case 0:
			problems = append(problems, fmt.Sprintf("  %s: no project key encodes to it, no directory under the moved tree encodes to it, and no recorded cwd in its sessions encodes to it", where))
		case 1:
			for path := range resolved {
				// Otherwise a sibling that merely shares the encoded prefix.
				if under(path, oldResolved) {
					descendants[path] = true
				}
			}
		default:
			listing := make([]string, 0, len(resolved))
			for path := range resolved {
				listing = append(listing, path)
			}
			sort.Strings(listing)
			problems = append(problems, fmt.Sprintf("  %s: ambiguous, its name encodes: %s", where, strings.Join(listing, ", ")))
		}
	}
	if len(problems) > 0 {
		return nil, fmt.Errorf("cannot safely migrate, nothing was changed: store dirs whose name could belong to a path under the source do not resolve to exactly one real path:\n%s", strings.Join(problems, "\n"))
	}
	return descendants, nil
}

// renameProjectDir renames oldProject to newProject, merging into it when it
// exists, and reports whether a rename or merge happened (or would).
func (m *mover) renameProjectDir(oldProject, newProject string) (bool, error) {
	ok, err := isDir(oldProject)
	if err != nil || !ok {
		return false, err
	}
	merge, err := exists(newProject)
	if err != nil {
		return false, err
	}
	if !merge {
		if !m.opts.CountOnly {
			if err := m.fx.Rename(oldProject, newProject); err != nil {
				return false, err
			}
		}
		m.log(fmt.Sprintf("  %s %s -> %s", m.verb("would rename", "renamed"), oldProject, newProject))
		return true, nil
	}
	m.log(fmt.Sprintf("  %s %s -> %s", m.verb("would merge", "merging"), oldProject, newProject))
	// Name collisions were refused before any change (checkMergesComplete),
	// so every entry moves.
	names, err := dirNames(oldProject)
	if err != nil {
		return false, err
	}
	for _, name := range names {
		if !m.opts.CountOnly {
			if err := m.fx.Move(filepath.Join(oldProject, name), filepath.Join(newProject, name)); err != nil {
				return false, err
			}
		}
		m.log(fmt.Sprintf("    %s: %s", m.verb("would move", "moved"), name))
	}
	if !m.opts.CountOnly {
		if err := m.fx.RemoveEmptyDir(oldProject); err != nil {
			return false, fmt.Errorf("cannot remove %s after merging it into %s: %w", oldProject, newProject, err)
		}
	}
	return true, nil
}

// rewritePrefixedPath rewrites a path equal to or under a migrated source;
// migrations are longest source first, so the most specific one wins.
func rewritePrefixedPath(path string, migrations []migration) string {
	for _, mg := range migrations {
		if under(path, mg.from) {
			return mg.to + path[len(mg.from):]
		}
	}
	return path
}

// updateClaudeJSON renames every projects{} key that is a migration source
// to its destination, and rewrites the githubRepoPaths values (repository to
// a list of local paths, or one path) equal to or under a source. It returns
// the counts of keys and paths updated; the file is rewritten as an ordered
// tree when either is non-zero.
func (m *mover) updateClaudeJSON(path string, migrations []migration) (int, int, error) {
	data, err := readClaudeJSON(path)
	if err != nil {
		return 0, 0, err
	}

	keysUpdated := 0
	if projects, ok := mustObject(data.Get("projects")); ok {
		for _, mg := range migrations {
			value, present := projects.Get(mg.from)
			if !present {
				continue
			}
			projects.Delete(mg.from)
			projects.Set(mg.to, value)
			m.log(fmt.Sprintf("  %s %s -> %s in %s", m.verb("would rename key", "renamed key"), quoted(mg.from), quoted(mg.to), path))
			keysUpdated++
		}
	}

	githubUpdated := 0
	if repoPaths, ok := mustObject(data.Get("githubRepoPaths")); ok {
		for _, repo := range repoPaths.Keys() {
			value, _ := repoPaths.Get(repo)
			_, isString := value.(string)
			var items []jsonfile.Value
			switch t := value.(type) {
			case string:
				items = []jsonfile.Value{t}
			case []jsonfile.Value:
				items = t
			default:
				continue
			}
			newItems := make([]jsonfile.Value, len(items))
			changed := 0
			for i, item := range items {
				newItems[i] = item
				s, ok := item.(string)
				if !ok {
					continue
				}
				rewritten := rewritePrefixedPath(s, migrations)
				if rewritten != s {
					changed++
					newItems[i] = rewritten
					m.log(fmt.Sprintf("  %s githubRepoPaths[%s]: %s -> %s in %s", m.verb("would rewrite", "rewrote"), quoted(repo), quoted(s), quoted(rewritten), path))
				}
			}
			if changed == 0 {
				continue
			}
			githubUpdated += changed
			if isString {
				repoPaths.Set(repo, newItems[0])
			} else {
				repoPaths.Set(repo, newItems)
			}
		}
	}

	if (keysUpdated > 0 || githubUpdated > 0) && !m.opts.CountOnly {
		out, err := jsonfile.MarshalIndented(data)
		if err != nil {
			return 0, 0, err
		}
		if err := m.fx.WriteFileAtomic(path, out); err != nil {
			return 0, 0, err
		}
	}
	return keysUpdated, githubUpdated, nil
}

// Mv renames the project directory oldPath to newPath and moves the Claude
// Code session data recorded under it; with PostHoc the directory was
// already renamed and only the session data moves. profiles are every
// profile's name and config directory, as the profile store enumerates
// them.
//
// The move is prefix-aware: every project proven to be oldPath or nested
// under it (see discoverDescendants) moves to newPath plus the same relative
// suffix. That covers the store dirs under projects/, the projects{} keys
// and githubRepoPaths entries in every profile's .claude.json, and the paths
// in the transcripts of every moved project. A proven descendant whose
// directory no longer exists is relabeled all the same. Every check runs
// before the first change, so a refusal leaves everything as it was.
func Mv(fx *effects.FX, ws workspace.Workspace, profiles []sessions.ProfileConfigDir, oldPath, newPath string, opts MvOptions) (MvResult, error) {
	m := &mover{fx: fx, opts: opts}
	var result MvResult

	oldResolved, err := ResolveUserPath(oldPath)
	if err != nil {
		return result, err
	}
	newResolved, err := ResolveUserPath(newPath)
	if err != nil {
		return result, err
	}
	if oldResolved == newResolved {
		return result, fmt.Errorf("source and target are the same: %s", oldResolved)
	}
	oldIsDir, err := isDir(oldResolved)
	if err != nil {
		return result, err
	}
	newIsDir, err := isDir(newResolved)
	if err != nil {
		return result, err
	}
	if opts.PostHoc {
		if !newIsDir {
			return result, fmt.Errorf("target does not exist as a directory: %s", newResolved)
		}
		oldExists, err := exists(oldResolved)
		if err != nil {
			return result, err
		}
		if oldExists {
			return result, fmt.Errorf("source still exists: %s -- use 'mv' without --post-hoc to rename it", oldResolved)
		}
	} else {
		// Validated now, renamed after descendant discovery, so nothing moves
		// when discovery refuses.
		if !oldIsDir {
			if newIsDir {
				// An interrupted run: the rename happened, the session move
				// did not.
				return result, fmt.Errorf("source does not exist as a directory: %s (the target %s does exist -- the rename already happened, so finish the session migration with: claudewheel mv --post-hoc %s %s)", oldResolved, newResolved, oldResolved, newResolved)
			}
			return result, fmt.Errorf("source does not exist as a directory: %s", oldResolved)
		}
		newExists, err := exists(newResolved)
		if err != nil {
			return result, err
		}
		if newExists {
			return result, fmt.Errorf("target already exists: %s", newResolved)
		}
	}

	m.log(fmt.Sprintf("moving %s -> %s", oldResolved, newResolved))
	if m.conditional() {
		m.log("DRY RUN -- no changes will be made")
	}
	m.log(fmt.Sprintf("encoded: %s -> %s", workspace.EncodePath(oldResolved), workspace.EncodePath(newResolved)))

	configDirs := make([]string, len(profiles))
	for i, p := range profiles {
		configDirs[i] = p.ConfigDir
	}
	sharedDir := ws.SharedDir()
	profileDirs := sessions.DiscoverProfileDirs(configDirs, sharedDir)
	result.ProfilesScanned = len(profileDirs)
	m.log(fmt.Sprintf("found %d profile/shared dirs", len(profileDirs)))

	// The moved tree as it exists on disk: the old path before the rename,
	// the new one after it (PostHoc). Both hold the same contents.
	sourceRoot := newResolved
	if oldIsDir {
		sourceRoot = oldResolved
	}
	claudeJSONs, err := claudeJSONFiles(profileDirs, sharedDir)
	if err != nil {
		return result, err
	}
	knownKeys, err := collectProjectKeys(claudeJSONs)
	if err != nil {
		return result, err
	}
	stores, err := sessions.DistinctStoreDirs(profileDirs)
	if err != nil {
		return result, err
	}
	descendants, err := discoverDescendants(stores, oldResolved, sourceRoot, knownKeys)
	if err != nil {
		return result, err
	}
	migrations := planMigrations(oldResolved, newResolved, descendants)
	result.PathsMigrated = len(migrations)
	for _, mg := range migrations {
		if mg.from != oldResolved {
			m.log(fmt.Sprintf("  nested project: %s -> %s", mg.from, mg.to))
		}
	}
	if err := checkTranscriptsReadable(stores, migrations); err != nil {
		return result, err
	}
	if err := checkMergesComplete(stores, migrations); err != nil {
		return result, err
	}

	if !opts.PostHoc {
		if m.conditional() {
			m.log(fmt.Sprintf("would rename directory %s -> %s", oldResolved, newResolved))
		}
		if !opts.CountOnly {
			if err := fx.Rename(oldResolved, newResolved); err != nil {
				return result, fmt.Errorf("failed to rename directory %s -> %s: %w", oldResolved, newResolved, err)
			}
		}
	}

	for _, projects := range stores {
		// Rename or merge each migrated store dir, then rewrite the
		// transcripts in every one. After a performed rename or merge the
		// files are in the new dir; when nothing is performed they are still
		// where they were, so the old dir is scanned too.
		var scanDirs []string
		for _, mg := range migrations {
			oldProject := filepath.Join(projects, workspace.EncodePath(mg.from))
			newProject := filepath.Join(projects, workspace.EncodePath(mg.to))
			renamed, err := m.renameProjectDir(oldProject, newProject)
			if err != nil {
				return result, err
			}
			if renamed {
				result.DirsRenamed++
			}
			for _, dir := range []string{newProject, oldProject} {
				if dir == oldProject && !m.conditional() {
					continue
				}
				ok, err := isDir(dir)
				if err != nil {
					return result, err
				}
				if ok && !slices.Contains(scanDirs, dir) {
					scanDirs = append(scanDirs, dir)
				}
			}
		}
		// Destinations are the new path plus a suffix, so replacing the
		// parent prefix fixes every descendant path in one pass.
		for _, scanDir := range scanDirs {
			paths, err := transcriptsToRewrite(scanDir)
			if err != nil {
				return result, err
			}
			for _, p := range paths {
				fixed, err := m.rewriteTranscriptFile(p, oldResolved, newResolved)
				if err != nil {
					return result, err
				}
				if fixed > 0 {
					result.FilesRewritten++
					result.LinesReplaced += fixed
				}
			}
		}
	}

	for _, path := range claudeJSONs {
		keys, github, err := m.updateClaudeJSON(path, migrations)
		if err != nil {
			return result, err
		}
		result.ProjectKeysUpdated += keys
		result.GithubRepoPathsUpdated += github
	}

	m.log("summary")
	m.log(fmt.Sprintf("  project paths migrated: %d", result.PathsMigrated))
	m.log(fmt.Sprintf("  dirs renamed:           %d", result.DirsRenamed))
	m.log(fmt.Sprintf("  files rewritten:        %d", result.FilesRewritten))
	m.log(fmt.Sprintf("  lines replaced:         %d", result.LinesReplaced))
	m.log(fmt.Sprintf("  project keys updated:   %d", result.ProjectKeysUpdated))
	m.log(fmt.Sprintf("  githubRepoPaths fixed:  %d", result.GithubRepoPathsUpdated))
	m.log(fmt.Sprintf("  profiles scanned:       %d", result.ProfilesScanned))
	if m.conditional() {
		m.log("  (dry run -- nothing written)")
	}
	return result, nil
}
