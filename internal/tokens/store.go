package tokens

import (
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/pathstat"
)

// The layout of claudewheel's data inside a profile directory: everything
// else in a profile directory belongs to Claude Code.
const (
	// DataDirName is the subdirectory holding claudewheel's data.
	DataDirName = ".claudewheel"
	// DataDirMode is set on the data directory every time it is ensured:
	// owner only, so the listing does not reveal the token file's name,
	// size, or modification time.
	DataDirMode = 0o700
	// TokenFileName is the token entry file inside the data directory.
	TokenFileName = "token.json"
	// TokenFileMode is the mode the token file is always written with, as a
	// secret: owner read and write only.
	TokenFileMode = effects.SecretFileMode
)

// StoreError reports a token file that exists but cannot be used. Reason
// never holds the token.
type StoreError struct {
	Path   string
	Reason error
}

func (e *StoreError) Error() string {
	return fmt.Sprintf("%s is corrupt or unreadable (%v); token resolution cannot proceed. Fix or remove the file, then retry", e.Path, e.Reason)
}

func (e *StoreError) Unwrap() error { return e.Reason }

// Store reads and writes the token entry of the profile whose Claude Code
// config directory is its profile directory. Building one touches nothing.
type Store struct {
	profileDir string
}

// NewStore returns the store of the profile directory profileDir.
func NewStore(profileDir string) Store {
	return Store{profileDir: profileDir}
}

// DataDir is the profile's claudewheel data directory.
func (s Store) DataDir() string { return filepath.Join(s.profileDir, DataDirName) }

// TokenFile is the profile's token entry file.
func (s Store) TokenFile() string { return filepath.Join(s.DataDir(), TokenFileName) }

// Exists reports whether the profile has a claudewheel data directory.
func (s Store) Exists() (bool, error) {
	info, err := os.Stat(s.DataDir())
	if pathstat.NotFoundOrParentNotDirectory(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return info.IsDir(), nil
}

// Load reads the token entry. A missing file is no entry (found is false);
// a file that cannot be read or decoded strictly is a *StoreError.
func (s Store) Load() (entry Entry, found bool, err error) {
	path := s.TokenFile()
	data, err := os.ReadFile(path)
	if pathstat.NotFoundOrParentNotDirectory(err) {
		return Entry{}, false, nil
	}
	if err != nil {
		return Entry{}, false, &StoreError{Path: path, Reason: err}
	}
	if err := jsonfile.DecodeStrict(data, &entry); err != nil {
		return Entry{}, false, &StoreError{Path: path, Reason: err}
	}
	return entry, true, nil
}

// Token returns the stored token, and false when there is none.
func (s Store) Token() (string, bool, error) {
	entry, _, err := s.Load()
	if err != nil {
		return "", false, err
	}
	token, ok := entry.TokenValue()
	return token, ok, nil
}

// Expiry returns the stored token's lifetime as of today (see Entry.Expiry),
// and false when there is no entry or it is empty.
func (s Store) Expiry(today time.Time) (Expiry, bool, error) {
	entry, found, err := s.Load()
	if err != nil || !found || entry.IsEmpty() {
		return Expiry{}, false, err
	}
	exp, err := entry.Expiry(today)
	if err != nil {
		return Expiry{}, false, fmt.Errorf("%s: %w", s.TokenFile(), err)
	}
	return exp, true, nil
}

// PlanEnv returns the declared plan as Claude Code's environment variables
// (see Entry.PlanEnv); no entry declares nothing.
func (s Store) PlanEnv() (map[string]string, error) {
	entry, _, err := s.Load()
	if err != nil {
		return nil, err
	}
	return entry.PlanEnv(s.TokenFile())
}

// EnsureDir creates the data directory when it is missing and sets its mode
// to DataDirMode.
func (s Store) EnsureDir(fx *effects.FX) error {
	if err := fx.MkdirAll(s.DataDir()); err != nil {
		return err
	}
	return fx.Chmod(s.DataDir(), DataDirMode)
}

// WriteToken replaces the entry with a new one for token (see BuildEntry).
func (s Store) WriteToken(fx *effects.FX, token string, expiry ExpiryDisposition, plan PlanTier, today time.Time) error {
	entry, err := BuildEntry(token, expiry, plan, today)
	if err != nil {
		return err
	}
	return s.write(fx, entry)
}

// SetPlan writes plan's fields into the entry, creating the entry when
// absent and leaving its other fields alone. A corrupt entry is an error,
// never overwritten.
func (s Store) SetPlan(fx *effects.FX, plan PlanTier) error {
	entry, _, err := s.Load()
	if err != nil {
		return err
	}
	entry.ApplyPlan(plan)
	return s.write(fx, entry)
}

// write ensures the data directory and writes entry as a secret.
func (s Store) write(fx *effects.FX, entry Entry) error {
	data, err := jsonfile.MarshalIndented(entry)
	if err != nil {
		return fmt.Errorf("encoding the token entry: %w", err)
	}
	if err := s.EnsureDir(fx); err != nil {
		return err
	}
	return fx.WriteSecretAtomic(s.TokenFile(), data)
}
