package sessionmove

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/realpath"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// migrateSimpleDirs are the profile directories whose entries are named by
// session uuid.
var migrateSimpleDirs = []string{"session-env", "file-history", "tasks"}

// SessionChoice selects the sessions Migrate moves: one session by its full
// lowercase uuid, or all of them. Exactly one must be given.
type SessionChoice struct {
	Session string
	All     bool
}

func (c SessionChoice) validate() error {
	switch {
	case c.All && c.Session != "":
		return errors.New("choose one session or all sessions, not both")
	case !c.All && c.Session == "":
		return errors.New("choose one session (by its uuid) or all sessions")
	case !c.All && !lifecycle.SessionUUIDRE.MatchString(c.Session):
		return fmt.Errorf("'%s' is not a session id: a session id is a full lowercase UUID (8-4-4-4-12 hex digits), as Claude Code records it", c.Session)
	}
	return nil
}

// MigrateResult counts what a migration did, or would do.
type MigrateResult struct {
	Moved      int
	Collisions int
	UUIDsFound int
}

func isUUID(name string) bool {
	return lifecycle.SessionUUIDRE.MatchString(name)
}

// discoverUUIDs returns the session uuids found across a profile's artifact
// directories: projects/<store dir>/<uuid>.jsonl and <uuid>/, the entries of
// the simple directories, and todos/<uuid>-agent-*.json.
func discoverUUIDs(src string) (map[string]bool, error) {
	uuids := map[string]bool{}
	projects := filepath.Join(src, workspace.ProjectsDirName)
	ok, err := isDir(projects)
	if err != nil {
		return nil, err
	}
	if ok {
		storeDirs, err := subdirs(projects)
		if err != nil {
			return nil, err
		}
		for _, storeDir := range storeDirs {
			names, err := dirNames(storeDir)
			if err != nil {
				return nil, err
			}
			for _, name := range names {
				if stem, found := strings.CutSuffix(name, ".jsonl"); found {
					if isUUID(stem) {
						uuids[stem] = true
					}
					continue
				}
				if !isUUID(name) {
					continue
				}
				dir, err := isDir(filepath.Join(storeDir, name))
				if err != nil {
					return nil, err
				}
				if dir {
					uuids[name] = true
				}
			}
		}
	}
	for _, d := range migrateSimpleDirs {
		names, err := namesIfDir(filepath.Join(src, d))
		if err != nil {
			return nil, err
		}
		for _, name := range names {
			if isUUID(name) {
				uuids[name] = true
			}
		}
	}
	names, err := namesIfDir(filepath.Join(src, "todos"))
	if err != nil {
		return nil, err
	}
	for _, name := range names {
		prefix, _, found := strings.Cut(name, "-agent-")
		if found && strings.HasSuffix(name, ".json") && isUUID(prefix) {
			uuids[prefix] = true
		}
	}
	return uuids, nil
}

// namesIfDir returns the sorted names in dir, or none when dir is not a
// directory.
func namesIfDir(dir string) ([]string, error) {
	ok, err := isDir(dir)
	if err != nil || !ok {
		return nil, err
	}
	return dirNames(dir)
}

// symlinkTarget returns the resolved target of p when p is a symbolic link.
func symlinkTarget(p string) (string, bool, error) {
	link, err := isSymlink(p)
	if err != nil || !link {
		return "", false, err
	}
	target, err := realpath.Resolve(p)
	if err != nil {
		return "", false, err
	}
	return target, true, nil
}

// sharedProjects reports whether both profiles' projects entries are
// symbolic links to the same directory, so their files are already together.
func sharedProjects(src, dst string) (bool, error) {
	srcTarget, srcLink, err := symlinkTarget(filepath.Join(src, workspace.ProjectsDirName))
	if err != nil {
		return false, err
	}
	dstTarget, dstLink, err := symlinkTarget(filepath.Join(dst, workspace.ProjectsDirName))
	if err != nil {
		return false, err
	}
	return srcLink && dstLink && srcTarget == dstTarget, nil
}

// migrator is one Migrate run.
type migrator struct {
	fx     *effects.FX
	result MigrateResult
}

func (m *migrator) log(msg string) {
	m.fx.Info("[migrate] " + msg)
}

// moveArtifact moves src to dst, refusing to overwrite: a dst already there
// is counted as a collision and src stays.
func (m *migrator) moveArtifact(src, dst string) error {
	there, err := exists(src)
	if err != nil || !there {
		return err
	}
	taken, err := exists(dst)
	if err != nil {
		return err
	}
	if taken {
		m.log(fmt.Sprintf("COLLISION: %s already exists, leaving %s in place", dst, src))
		m.result.Collisions++
		return nil
	}
	m.result.Moved++
	if m.fx.Previewing() {
		m.log("MOVE  " + src)
		m.log("  ->  " + dst)
	}
	if err := m.fx.MkdirAll(filepath.Dir(dst)); err != nil {
		return err
	}
	return m.fx.Move(src, dst)
}

// moveSession moves every artifact of one session from src to dst.
func (m *migrator) moveSession(src, dst, uuid string) error {
	projects := filepath.Join(src, workspace.ProjectsDirName)
	ok, err := isDir(projects)
	if err != nil {
		return err
	}
	if ok {
		storeDirs, err := subdirs(projects)
		if err != nil {
			return err
		}
		for _, storeDir := range storeDirs {
			name := filepath.Base(storeDir)
			if err := m.moveArtifact(filepath.Join(storeDir, uuid+".jsonl"), filepath.Join(dst, workspace.ProjectsDirName, name, uuid+".jsonl")); err != nil {
				return err
			}
			folder := filepath.Join(storeDir, uuid)
			isFolder, err := isDir(folder)
			if err != nil {
				return err
			}
			if isFolder {
				if err := m.moveArtifact(folder, filepath.Join(dst, workspace.ProjectsDirName, name, uuid)); err != nil {
					return err
				}
			}
		}
	}
	for _, d := range migrateSimpleDirs {
		if err := m.moveArtifact(filepath.Join(src, d, uuid), filepath.Join(dst, d, uuid)); err != nil {
			return err
		}
	}
	names, err := namesIfDir(filepath.Join(src, "todos"))
	if err != nil {
		return err
	}
	for _, name := range names {
		if strings.HasPrefix(name, uuid+"-agent-") && strings.HasSuffix(name, ".json") {
			if err := m.moveArtifact(filepath.Join(src, "todos", name), filepath.Join(dst, "todos", name)); err != nil {
				return err
			}
		}
	}
	return nil
}

// Migrate moves the chosen sessions' artifacts from the profile src to the
// profile dst, each given with its config directory (the default profile's
// is Claude Code's own ~/.claude). An artifact whose destination already
// exists stays where it is and is counted as a collision. When both
// profiles' projects entries link to one shared store, nothing moves. A
// chosen session the source does not hold is an error.
func Migrate(fx *effects.FX, src, dst sessions.ProfileConfigDir, choice SessionChoice) (MigrateResult, error) {
	m := &migrator{fx: fx}
	if err := choice.validate(); err != nil {
		return m.result, err
	}
	for _, p := range []struct{ what, dir string }{{"source", src.ConfigDir}, {"dest", dst.ConfigDir}} {
		ok, err := isDir(p.dir)
		if err != nil {
			return m.result, err
		}
		if !ok {
			return m.result, fmt.Errorf("%s profile dir missing: %s", p.what, p.dir)
		}
	}

	found, err := discoverUUIDs(src.ConfigDir)
	if err != nil {
		return m.result, err
	}
	var uuids []string
	if choice.All {
		for u := range found {
			uuids = append(uuids, u)
		}
		sort.Strings(uuids)
	} else {
		if !found[choice.Session] {
			return m.result, fmt.Errorf("profile %s holds no artifact of session %s", src.Name, choice.Session)
		}
		uuids = []string{choice.Session}
	}
	m.result.UUIDsFound = len(uuids)

	skipMove, err := sharedProjects(src.ConfigDir, dst.ConfigDir)
	if err != nil {
		return m.result, err
	}

	m.log(fmt.Sprintf("%s -> %s", src.Name, dst.Name))
	m.log(fmt.Sprintf("discovered %d session UUIDs in source", m.result.UUIDsFound))
	if skipMove {
		m.log("shared store detected — skipping moves (files already co-located)")
	}
	if fx.Previewing() {
		m.log("DRY RUN — no changes will be made")
	}

	if !skipMove {
		for _, uuid := range uuids {
			if err := m.moveSession(src.ConfigDir, dst.ConfigDir, uuid); err != nil {
				return m.result, err
			}
		}
	}

	m.log("summary")
	m.log(fmt.Sprintf("  moved:      %d", m.result.Moved))
	m.log(fmt.Sprintf("  collisions: %d", m.result.Collisions))
	if fx.Previewing() {
		m.log("  (dry run — nothing written)")
	}
	return m.result, nil
}
