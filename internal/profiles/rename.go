package profiles

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/sessions"
)

// RenamePendingFile is the breadcrumb a rename writes into the profile
// directory before moving it and removes once every store is updated.
const RenamePendingFile = ".rename_pending"

// renamePending is the breadcrumb's content.
type renamePending struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// PendingRenameError is a rename breadcrumb left by an interrupted rename.
// When it could be read, From and To name the rename, and rerunning
// `claudewheel profile rename <from> <to>` finishes it. Detail says why it
// cannot be finished that way otherwise.
type PendingRenameError struct {
	Breadcrumb string
	From, To   string
	Detail     string
}

func (e *PendingRenameError) Error() string {
	if e.Detail != "" {
		return fmt.Sprintf("an interrupted profile rename left the breadcrumb %s, which %s, so claudewheel cannot finish it: "+
			"check which profile directory holds the profile's data, give it the intended name, and remove the breadcrumb", e.Breadcrumb, e.Detail)
	}
	return fmt.Sprintf("the profile rename from '%s' to '%s' was interrupted (breadcrumb %s): finish it with `claudewheel profile rename %s %s`",
		e.From, e.To, e.Breadcrumb, e.From, e.To)
}

// pendingRename is one breadcrumb found under profiles/.
type pendingRename struct {
	dirName string
	path    string
	crumb   renamePending
	err     *PendingRenameError
}

// pendingRenames reads every breadcrumb under profiles/, in name order.
func (s Store) pendingRenames() ([]pendingRename, error) {
	entries, err := readDirIfExists(s.ws.ProfilesDir())
	if err != nil {
		return nil, err
	}
	var out []pendingRename
	for _, e := range entries {
		dir := filepath.Join(s.ws.ProfilesDir(), e.Name())
		isDirectory, err := isDir(dir)
		if err != nil {
			return nil, err
		}
		if !isDirectory {
			continue
		}
		path := filepath.Join(dir, RenamePendingFile)
		present, err := exists(path)
		if err != nil {
			return nil, err
		}
		if !present {
			continue
		}
		p := pendingRename{dirName: e.Name(), path: path}
		data, err := os.ReadFile(path)
		switch {
		case err != nil:
			p.err = &PendingRenameError{Breadcrumb: path, Detail: fmt.Sprintf("cannot be read (%v)", err)}
		case jsonfile.DecodeStrict(data, &p.crumb) != nil:
			p.err = &PendingRenameError{Breadcrumb: path, Detail: "is not a {\"from\", \"to\"} object"}
		case p.crumb.From == "" || p.crumb.To == "":
			p.err = &PendingRenameError{Breadcrumb: path, Detail: "names no source or no target"}
		case e.Name() != p.crumb.From && e.Name() != p.crumb.To:
			p.err = &PendingRenameError{Breadcrumb: path, Detail: fmt.Sprintf(
				"records a rename from '%s' to '%s' but sits in the directory '%s'", p.crumb.From, p.crumb.To, e.Name())}
		default:
			p.err = &PendingRenameError{Breadcrumb: path, From: p.crumb.From, To: p.crumb.To}
		}
		out = append(out, p)
	}
	return out, nil
}

// CheckPendingRenames returns a *PendingRenameError for the first rename
// breadcrumb left under profiles/ by an interrupted rename, and nil when
// there is none. Every entry point of the store refuses to work past one.
func (s Store) CheckPendingRenames() error {
	pending, err := s.pendingRenames()
	if err != nil {
		return err
	}
	if len(pending) > 0 {
		return pending[0].err
	}
	return nil
}

// Rename moves profile oldName to newName: its directory (with the token
// stored inside it), its options.json registration and metadata, and
// state.json's last_config. It is crash-safe through a breadcrumb written
// into the directory before it moves and removed last. Rerunning the same
// rename after an interruption finishes it, and resumed reports that it
// did; any other breadcrumb is refused. newName must be valid and free in
// both the directory tree and options.json; a profile holding a live
// interactive session is refused.
func (s Store) Rename(fx *effects.FX, oldName, newName string) (resumed bool, err error) {
	for _, name := range []string{oldName, newName} {
		if slices.Contains(ReservedNames(), name) {
			return false, fmt.Errorf("'%s' cannot be renamed to or from", name)
		}
	}
	pending, err := s.pendingRenames()
	if err != nil {
		return false, err
	}
	for _, p := range pending {
		if p.err.Detail != "" || p.crumb.From != oldName || p.crumb.To != newName {
			return false, p.err
		}
		if p.dirName == newName {
			// The directory moved before the interruption: finish the
			// store updates, which are idempotent, and drop the breadcrumb.
			if err := s.renameInStores(fx, oldName, newName); err != nil {
				return false, err
			}
			return true, fx.Remove(p.path)
		}
		// The directory never moved: the rename runs again from the start.
		resumed = true
	}

	if err := CheckNewName(newName); err != nil {
		return false, err
	}
	opts, err := appconfig.ReadOptions(s.ws)
	if err != nil {
		return false, err
	}
	seg := opts[Segment]
	oldDir, newDir := s.PathFor(oldName), s.PathFor(newName)
	oldExists, err := isDir(oldDir)
	if err != nil {
		return false, err
	}
	registered := slices.Contains(seg.Values, oldName) || slices.Contains(seg.Pinned, oldName)
	if !registered && !oldExists {
		return false, fmt.Errorf("Profile '%s' not found", oldName)
	}
	if !oldExists {
		return false, fmt.Errorf("profile directory does not exist: %s", oldDir)
	}
	taken, err := lexists(newDir)
	if err != nil {
		return false, err
	}
	if taken {
		return false, fmt.Errorf("Profile '%s' already exists (directory %s)", newName, newDir)
	}
	if slices.Contains(seg.Values, newName) || slices.Contains(seg.Pinned, newName) {
		return false, fmt.Errorf("Profile '%s' already registered in options", newName)
	}
	if sessions.HasLiveInteractive(oldDir) {
		return false, fmt.Errorf("Profile '%s' has a live interactive session. Stop it before renaming", oldName)
	}

	crumb, err := jsonfile.MarshalIndented(renamePending{From: oldName, To: newName})
	if err != nil {
		return false, err
	}
	if err := fx.WriteFileAtomic(filepath.Join(oldDir, RenamePendingFile), crumb); err != nil {
		return false, err
	}
	if err := fx.Rename(oldDir, newDir); err != nil {
		return false, err
	}
	if err := s.renameInStores(fx, oldName, newName); err != nil {
		return false, err
	}
	return resumed, fx.Remove(filepath.Join(newDir, RenamePendingFile))
}

// renameInStores swaps oldName for newName in options.json (values, pinned, and
// metadata) and in state.json's last_config. Both are idempotent.
func (s Store) renameInStores(fx *effects.FX, oldName, newName string) error {
	if _, err := appconfig.RenameOptionValue(fx, s.ws, Segment, oldName, newName); err != nil {
		return err
	}
	st, err := appconfig.ReadState(s.ws)
	if err != nil {
		return err
	}
	if st.LastConfig[Segment] != oldName {
		return nil
	}
	return appconfig.UpdateState(fx, s.ws, func(st *appconfig.State) error {
		if st.LastConfig[Segment] == oldName {
			st.LastConfig[Segment] = newName
		}
		return nil
	})
}
