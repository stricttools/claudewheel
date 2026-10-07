package sessions

import (
	"os"
	"path/filepath"
	"sort"
)

// A session store is a projects directory: one per profile directory, and
// the shared one under ~/.claudewheel/shared. Managed profiles' projects
// entries are symbolic links to the shared store while the default profile
// (~/.claude) may keep its own, so one resolved directory is reached under
// several names, and a pass over session data must visit each once.

// DiscoverProfileDirs returns profileDirs (every profile's directory, as the
// profile store enumerates them) plus sharedDir when it is a directory not
// already listed, sorted.
func DiscoverProfileDirs(profileDirs []string, sharedDir string) []string {
	dirs := append([]string(nil), profileDirs...)
	if info, err := os.Stat(sharedDir); err == nil && info.IsDir() {
		listed := false
		for _, d := range dirs {
			if d == sharedDir {
				listed = true
				break
			}
		}
		if !listed {
			dirs = append(dirs, sharedDir)
		}
	}
	sort.Strings(dirs)
	return dirs
}

// DistinctStoreDirs returns the projects store directories of profileDirs,
// each resolved directory once, as resolved paths in first-seen order. A second
// visit would rewrite already-rewritten paths again (foo -> foobar
// compounding into foobarbar) and multiply every counter. A profile
// directory without a projects directory is skipped.
func DistinctStoreDirs(profileDirs []string) ([]string, error) {
	seen := map[string]bool{}
	var dirs []string
	for _, pdir := range profileDirs {
		projects := filepath.Join(pdir, "projects")
		if info, err := os.Stat(projects); err != nil || !info.IsDir() {
			continue
		}
		resolved, err := filepath.EvalSymlinks(projects)
		if err != nil {
			return nil, err
		}
		resolved, err = filepath.Abs(resolved)
		if err != nil {
			return nil, err
		}
		if !seen[resolved] {
			seen[resolved] = true
			dirs = append(dirs, resolved)
		}
	}
	return dirs, nil
}
