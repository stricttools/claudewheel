// Package archiver delegates profile deletion to saferm, so a deleted profile
// can be restored: saferm archives the directory and then removes it, and
// the handle it reports restores it with one `saferm undelete`.
//
// It finds saferm (claudewheel's own copy under <root>/bin first, then
// PATH), negotiates on the features the delegation uses rather than on a
// version string, runs the delegation through effects, and installs saferm
// from its GitHub release after checking the download's SHA-256 against the
// release's checksum manifest. Whether to offer that install is the caller's
// decision: this package exposes what it needs (Unavailable) and the install
// itself, never a prompt.
//
// No record of an archive is kept in claudewheel: saferm's archive holds
// everything needed to restore and has its own audit trail, so the handle is
// reported to the user and then lives in `saferm list`.
package archiver

import (
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/pathstat"
)

// Saferm is the program claudewheel delegates deletion to.
const Saferm = "saferm"

// RequiredFeatures returns the saferm features the delegation uses, sorted:
// machine-payloads (the --json payload carrying the archived records, where
// the handle comes from), on-error-modes (--on-error, mandatory on delete),
// git-index-switches (--no-update-git-index, so archiving a directory inside
// someone's worktree never stages anything), and uuid-handles (the durable
// handle `saferm undelete` accepts).
func RequiredFeatures() []string {
	return []string{"git-index-switches", "machine-payloads", "on-error-modes", "uuid-handles"}
}

const (
	// ProbeTimeout bounds `saferm capabilities`, which reads no database, so
	// a slow answer means something is wrong rather than busy.
	ProbeTimeout = 15 * time.Second
	// ArchiveTimeout bounds one archival: a profile directory is small, but
	// conversation history under a real (not symlinked) shared name is not.
	ArchiveTimeout = 1800 * time.Second
)

// InstallCommands returns the commands that install saferm, claudewheel's
// own channel first, then the ecosystems' channels.
func InstallCommands() []string {
	return []string{
		"go install github.com/stricttools/saferm@v0",
		"npm install -g saferemove",
		"uv tool install saferm",
		"brew install smm-h/tap/saferm",
	}
}

// BinDir is where claudewheel keeps the saferm it installed itself, under
// the workspace root.
func BinDir(root string) string {
	return filepath.Join(root, "bin")
}

// Handle is what one archival hands back. UUID is the durable handle:
// RestoreCommand puts the whole profile directory back, its stored token
// included. GroupID names the invocation.
type Handle struct {
	UUID    string
	GroupID string
	Path    string
	Size    int64
}

// RestoreCommand is the one command that puts the archived directory back.
// It passes --no-update-git-index as the archival did: a profile directory
// inside a git worktree must not have its restored credentials and token
// staged in an index claudewheel does not own.
func (h Handle) RestoreCommand() string {
	return restoreCommand(h.UUID)
}

func restoreCommand(uuid string) string {
	return Saferm + " undelete --no-update-git-index " + uuid
}

// ArchiveError is a refused or failed archival that destroyed nothing: the
// directory is still on disk.
type ArchiveError struct {
	msg string
}

func (e *ArchiveError) Error() string { return e.msg }

// ArchiveUnreadableError is an archival saferm reported as successful (it
// exited 0, so the directory is archived and gone) whose answer could not be
// read, so there is no handle. Its message carries whatever handle
// information the answer did hold and names `saferm list`.
type ArchiveUnreadableError struct {
	msg string
}

func (e *ArchiveUnreadableError) Error() string { return e.msg }

// Tool is a saferm binary that answered the capabilities probe with every
// required feature. Only Detect builds one.
type Tool struct {
	Binary   string
	Features []string
}

// Archive hands path to saferm, which archives it and then removes it. Under
// --dry-run the invocation is recorded and nil is returned with no error:
// nothing ran, so there is no handle. Every flag is explicit: --on-error
// abort (a profile is one directory, with no remainder to carry on with),
// --no-update-git-index, the mandatory --description, and --json (the
// payload is where the handle comes from).
func (t *Tool) Archive(fx *effects.FX, path, description string) (*Handle, error) {
	argv := []string{
		t.Binary, "delete",
		"--on-error", "abort",
		"--no-update-git-index",
		"--recursive",
		"--description", description,
		"--json",
		path,
	}
	result, err := fx.Run(effects.Cmd{
		Argv:     argv,
		Capture:  true,
		Timeout:  ArchiveTimeout,
		Resource: "profile-archive:" + path,
		Grant:    "archive-delegation",
	})
	if err != nil {
		if errors.Is(err, effects.ErrTimedOut) {
			return nil, &ArchiveError{msg: fmt.Sprintf("%s did not finish archiving %s within %.0fs. Nothing was deleted", Saferm, path, ArchiveTimeout.Seconds())}
		}
		return nil, &ArchiveError{msg: fmt.Sprintf("could not run %s to archive %s: %v. Nothing was deleted", t.Binary, path, err)}
	}
	if result.Recorded() {
		return nil, nil
	}
	if code := result.ExitCode(); code != 0 {
		detail := ""
		if text := strings.TrimSpace(result.Stderr()); text != "" {
			detail = ": " + text
		}
		return nil, &ArchiveError{msg: fmt.Sprintf("%s exited %d archiving %s%s. Nothing was deleted", Saferm, code, path, detail)}
	}

	// saferm exited 0: the directory is archived and gone. Nothing below may
	// say otherwise, and a handle is built only from a well-formed record.
	payload, ok := payloadOf(result.Stdout())
	if !ok {
		return nil, unreadable(path, "could not be parsed", "", "")
	}
	groupID, ok := optionalString(payload, "group_id")
	if !ok {
		return nil, unreadable(path, "named a group id that is not a string", "", "")
	}
	archivedValue, _ := payload.Get("archived")
	archived, isList := archivedValue.([]jsonfile.Value)
	if !isList || len(archived) == 0 {
		return nil, unreadable(path, "named no archived record", "", groupID)
	}
	record, isObject := archived[0].(*jsonfile.Object)
	if !isObject {
		return nil, unreadable(path, "named an archived record that is not an object", "", groupID)
	}
	uuid, ok := optionalString(record, "uuid")
	uuid = strings.TrimSpace(uuid)
	if !ok || uuid == "" {
		return nil, unreadable(path, "named an archived record with no uuid", "", groupID)
	}
	size, ok := optionalInt(record, "size")
	if !ok {
		return nil, unreadable(path, "named a size that is not a whole number", uuid, groupID)
	}
	recordPath := path
	if p, present := record.Get("path"); present && p != nil {
		s, isString := p.(string)
		if !isString {
			return nil, unreadable(path, "named a path that is not a string", uuid, groupID)
		}
		recordPath = s
	}
	return &Handle{UUID: uuid, GroupID: groupID, Path: recordPath, Size: size}, nil
}

// unreadable builds the error for a success whose answer could not be used:
// the archival happened, whatever handle information was read, and where the
// record is regardless.
func unreadable(path, detail, uuid, groupID string) *ArchiveUnreadableError {
	known := ""
	switch {
	case uuid != "":
		known = fmt.Sprintf(" The handle it did report is %s, so the profile can still be restored: %s.", uuid, restoreCommand(uuid))
	case groupID != "":
		known = fmt.Sprintf(" The invocation's group id is %s.", groupID)
	}
	return &ArchiveUnreadableError{msg: fmt.Sprintf(
		"%s exited 0 archiving %s, so the profile directory was archived and removed, but its --json answer %s, so claudewheel has no handle to report.%s Find the record with `%s list`",
		Saferm, path, detail, known, Saferm)}
}

// payloadOf returns the payload object of a strictcli machine-mode answer,
// which is the sole document on stdout.
func payloadOf(stdout string) (*jsonfile.Object, bool) {
	envelope, err := jsonfile.DecodeObject([]byte(stdout))
	if err != nil {
		return nil, false
	}
	payload, _ := envelope.Get("payload")
	obj, ok := payload.(*jsonfile.Object)
	return obj, ok
}

// optionalString reads a string member; absent and null read as "". A value
// of another type reports false.
func optionalString(o *jsonfile.Object, key string) (string, bool) {
	v, present := o.Get(key)
	if !present || v == nil {
		return "", true
	}
	s, ok := v.(string)
	return s, ok
}

// optionalInt reads a whole-number member; absent and null read as 0. A
// value of another type, or a number with a fraction, reports false.
func optionalInt(o *jsonfile.Object, key string) (int64, bool) {
	v, present := o.Get(key)
	if !present || v == nil {
		return 0, true
	}
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	i, err := n.Int64()
	return i, err == nil
}

// Reason says why saferm cannot be used.
type Reason string

const (
	// ReasonAbsent is no saferm binary anywhere.
	ReasonAbsent Reason = "absent"
	// ReasonNoVerb is a binary too old to answer `saferm capabilities`.
	ReasonNoVerb Reason = "no-verb"
	// ReasonMissingFeatures is a binary that answers but lacks a required
	// feature.
	ReasonMissingFeatures Reason = "missing-features"
)

// Unavailable is why deletion cannot proceed. Every reason has the same
// fix: install or upgrade saferm.
type Unavailable struct {
	Reason Reason
	// Binary is the saferm found, empty when absent.
	Binary string
	// Missing lists the required features the binary lacks, sorted.
	Missing []string
}

// Upgrade reports whether a saferm exists and is merely too old.
func (u *Unavailable) Upgrade() bool {
	return u.Binary != ""
}

// Verb is "Upgrade" when a saferm exists and "Install" otherwise.
func (u *Unavailable) Verb() string {
	if u.Upgrade() {
		return "Upgrade"
	}
	return "Install"
}

// Diagnosis is one line naming saferm and what is wrong with it.
func (u *Unavailable) Diagnosis() string {
	switch u.Reason {
	case ReasonAbsent:
		return Saferm + " is not installed."
	case ReasonNoVerb:
		return fmt.Sprintf("%s at %s is too old: it does not answer `saferm capabilities`.", Saferm, u.Binary)
	default:
		return fmt.Sprintf("%s at %s does not ship: %s.", Saferm, u.Binary, strings.Join(u.Missing, ", "))
	}
}

// Stakes says why the deletion of profile name stops instead of proceeding
// without saferm.
func (u *Unavailable) Stakes(name string) string {
	return fmt.Sprintf("Deleting profile '%s' delegates to %s so it can be restored afterwards. "+
		"Without it the deletion would be irreversible: the profile directory, its settings.json, "+
		"its .credentials.json and its stored OAuth token would be gone for good.", name, Saferm)
}

// Fix is the install commands, one per line, indented for a message body.
func (u *Unavailable) Fix() string {
	lines := make([]string, 0, len(InstallCommands()))
	for _, c := range InstallCommands() {
		lines = append(lines, "  "+c)
	}
	return strings.Join(lines, "\n")
}

// RefusalError is the message of a deletion refused without an install
// offer: previewing is a --dry-run (which installs nothing), otherwise there
// was no terminal to offer the install at. There is deliberately no flag
// that deletes without the archive, so the message teaches none.
func (u *Unavailable) RefusalError(name string, previewing bool) string {
	reason := "There is no terminal to offer the install at"
	if previewing {
		reason = "This is a preview (--dry-run), which installs nothing"
	}
	return fmt.Sprintf("error: %s\n%s\n%s, so nothing was deleted -- profile '%s' is untouched.\n%s %s and run this again:\n%s",
		u.Diagnosis(), u.Stakes(name), reason, name, u.Verb(), Saferm, u.Fix())
}

// MayOfferInstall reports whether a deletion may offer to install saferm:
// only at a terminal (interactive) and never in a preview, which promised
// to change nothing.
func MayOfferInstall(previewing, interactive bool) bool {
	return interactive && !previewing
}

// Locate returns the saferm claudewheel would use: its own copy under
// BinDir(root) first, so an accepted install offer wins over an older saferm
// on PATH, then the first executable saferm on PATH. It reports false when
// there is none.
func Locate(root string) (string, bool, error) {
	own := filepath.Join(BinDir(root), Saferm)
	ok, err := pathstat.IsExecutableFile(own)
	if err != nil {
		return "", false, err
	}
	if ok {
		return own, true, nil
	}
	path, ok, err := effects.LookPath(Saferm, effects.SearchPath(nil))
	if err != nil {
		return "", false, fmt.Errorf("cannot search PATH for %s: %w", Saferm, err)
	}
	return path, ok, nil
}

// Probe asks binary which features it ships, through `capabilities --json`,
// a declared read that runs in every mode. It reports false when the binary
// cannot answer: it fails to run, exits nonzero, or prints no feature list.
func Probe(fx *effects.FX, binary string) ([]string, bool) {
	result, err := fx.Run(effects.Cmd{
		Argv:    []string{binary, "capabilities", "--json"},
		Capture: true,
		Timeout: ProbeTimeout,
		Read:    true,
	})
	if err != nil || result.ExitCode() != 0 {
		return nil, false
	}
	payload, ok := payloadOf(result.Stdout())
	if !ok {
		return nil, false
	}
	value, _ := payload.Get("features")
	list, ok := value.([]jsonfile.Value)
	if !ok {
		return nil, false
	}
	features := make([]string, 0, len(list))
	for _, f := range list {
		s, isString := f.(string)
		if !isString {
			return nil, false
		}
		features = append(features, s)
	}
	return features, true
}

// Detect finds a saferm that ships every required feature. Exactly one of
// its results is non-nil, unless err is set (a path that could not be
// inspected).
func Detect(fx *effects.FX, root string) (*Tool, *Unavailable, error) {
	binary, found, err := Locate(root)
	if err != nil {
		return nil, nil, err
	}
	if !found {
		return nil, &Unavailable{Reason: ReasonAbsent}, nil
	}
	features, ok := Probe(fx, binary)
	if !ok {
		return nil, &Unavailable{Reason: ReasonNoVerb, Binary: binary}, nil
	}
	var missing []string
	for _, f := range RequiredFeatures() {
		if !slices.Contains(features, f) {
			missing = append(missing, f)
		}
	}
	if len(missing) > 0 {
		return nil, &Unavailable{Reason: ReasonMissingFeatures, Binary: binary, Missing: missing}, nil
	}
	return &Tool{Binary: binary, Features: features}, nil, nil
}
