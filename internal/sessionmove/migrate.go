package sessionmove

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/pathstat"
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
	ok, err := pathstat.IsDir(projects)
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
				dir, err := pathstat.IsDir(filepath.Join(storeDir, name))
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
	ok, err := pathstat.IsDir(dir)
	if err != nil || !ok {
		return nil, err
	}
	return dirNames(dir)
}

// symlinkTarget returns the resolved target of p when p is a symbolic link.
func symlinkTarget(p string) (string, bool, error) {
	link, err := pathstat.IsSymlink(p)
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

// artifactMove is one artifact a migration moves.
type artifactMove struct {
	src, dst string
}

// planSession appends to moves every artifact of one session in src, each
// with its place in dst.
func planSession(moves []artifactMove, src, dst, uuid string) ([]artifactMove, error) {
	add := func(from, to string) error {
		there, err := pathstat.Exists(from)
		if err == nil && there {
			moves = append(moves, artifactMove{src: from, dst: to})
		}
		return err
	}
	projects := filepath.Join(src, workspace.ProjectsDirName)
	ok, err := pathstat.IsDir(projects)
	if err != nil {
		return nil, err
	}
	if ok {
		storeDirs, err := subdirs(projects)
		if err != nil {
			return nil, err
		}
		for _, storeDir := range storeDirs {
			name := filepath.Base(storeDir)
			if err := add(filepath.Join(storeDir, uuid+".jsonl"), filepath.Join(dst, workspace.ProjectsDirName, name, uuid+".jsonl")); err != nil {
				return nil, err
			}
			folder := filepath.Join(storeDir, uuid)
			isFolder, err := pathstat.IsDir(folder)
			if err != nil {
				return nil, err
			}
			if isFolder {
				if err := add(folder, filepath.Join(dst, workspace.ProjectsDirName, name, uuid)); err != nil {
					return nil, err
				}
			}
		}
	}
	for _, d := range migrateSimpleDirs {
		if err := add(filepath.Join(src, d, uuid), filepath.Join(dst, d, uuid)); err != nil {
			return nil, err
		}
	}
	names, err := namesIfDir(filepath.Join(src, "todos"))
	if err != nil {
		return nil, err
	}
	for _, name := range names {
		if strings.HasPrefix(name, uuid+"-agent-") && strings.HasSuffix(name, ".json") {
			if err := add(filepath.Join(src, "todos", name), filepath.Join(dst, "todos", name)); err != nil {
				return nil, err
			}
		}
	}
	return moves, nil
}

// collisions returns a line for every move whose destination already holds
// something (a dangling symbolic link included).
func collisions(moves []artifactMove) ([]string, error) {
	var out []string
	for _, mv := range moves {
		taken, err := pathstat.Lexists(mv.dst)
		if err != nil {
			return nil, err
		}
		if taken {
			out = append(out, fmt.Sprintf("  %s (from %s)", mv.dst, mv.src))
		}
	}
	return out, nil
}

// move moves one artifact, its destination checked free beforehand.
func (m *migrator) move(mv artifactMove) error {
	m.result.Moved++
	if m.fx.Previewing() {
		m.log("MOVE  " + mv.src)
		m.log("  ->  " + mv.dst)
	}
	if err := m.fx.MkdirAll(filepath.Dir(mv.dst)); err != nil {
		return err
	}
	return m.fx.Move(mv.src, mv.dst)
}

// Migrate moves the chosen sessions' artifacts from the profile src to the
// profile dst, each given with its config directory (the default profile's
// is Claude Code's own ~/.claude). Every artifact is found and every
// destination checked before anything moves: a destination that already
// exists is an error listing every such collision, and nothing moves. When
// both profiles' projects entries link to one shared store, nothing moves. A
// chosen session the source does not hold is an error.
func Migrate(fx *effects.FX, src, dst sessions.ProfileConfigDir, choice SessionChoice) (MigrateResult, error) {
	m := &migrator{fx: fx}
	if err := choice.validate(); err != nil {
		return m.result, err
	}
	for _, p := range []struct{ what, dir string }{{"source", src.ConfigDir}, {"dest", dst.ConfigDir}} {
		ok, err := pathstat.IsDir(p.dir)
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
	var moves []artifactMove
	if !skipMove {
		for _, uuid := range uuids {
			if moves, err = planSession(moves, src.ConfigDir, dst.ConfigDir, uuid); err != nil {
				return m.result, err
			}
		}
		taken, err := collisions(moves)
		if err != nil {
			return m.result, err
		}
		if len(taken) > 0 {
			return m.result, fmt.Errorf("cannot migrate, nothing was moved: these destinations already exist in profile %s:\n%s", dst.Name, strings.Join(taken, "\n"))
		}
	}

	m.log(fmt.Sprintf("%s -> %s", src.Name, dst.Name))
	m.log(fmt.Sprintf("discovered %d session UUIDs in source", m.result.UUIDsFound))
	if skipMove {
		m.log("shared store detected — skipping moves (files already co-located)")
	}
	if fx.Previewing() {
		m.log("DRY RUN — no changes will be made")
	}

	for _, mv := range moves {
		if err := m.move(mv); err != nil {
			return m.result, err
		}
	}

	m.log("summary")
	m.log(fmt.Sprintf("  moved:      %d", m.result.Moved))
	if fx.Previewing() {
		m.log("  (dry run — nothing written)")
	}
	return m.result, nil
}
