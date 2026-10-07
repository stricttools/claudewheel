package workspace

import (
	"path/filepath"
	"unicode/utf8"
)

const (
	// LifecycleDirName is the machine-wide per-session lifecycle store under
	// shared/. It is not symlinked into profiles: a session's record outlives
	// the profile that launched it.
	LifecycleDirName = "lifecycle"
	// ProbesDirName is the machine-wide probe store under shared/, not
	// symlinked into profiles either.
	ProbesDirName = "probes"
	// SessionMovesDirName holds the journals of unfinished session moves
	// under shared/, not symlinked into profiles either.
	SessionMovesDirName = "session-moves"
	// ProjectDirNameLimit is the longest store-dir name Claude Code writes in
	// full; a longer encoded path is cut to this length and suffixed with a
	// hash of the raw path.
	ProjectDirNameLimit = 200
)

// SharedSubdirs returns the directories inside each profile that are
// symlinked to the shared store, in order.
func SharedSubdirs() []string {
	return []string{"projects", "session-env", "file-history", "tasks", "todos", "paste-cache"}
}

// SharedStore computes paths in the shared store. It reads and writes nothing.
type SharedStore struct {
	sharedDir string
	skillsDir string
}

// Dir is the shared store's root, shared/.
func (s SharedStore) Dir() string { return s.sharedDir }

// SkillsDir is the skills directory every profile links to.
func (s SharedStore) SkillsDir() string { return s.skillsDir }

// ProjectsDir holds the per-project session stores, shared/projects.
func (s SharedStore) ProjectsDir() string { return filepath.Join(s.sharedDir, "projects") }

// LifecycleDir holds the per-session lifecycle files, shared/lifecycle.
func (s SharedStore) LifecycleDir() string { return filepath.Join(s.sharedDir, LifecycleDirName) }

// ProbesDir holds the probe store, shared/probes.
func (s SharedStore) ProbesDir() string { return filepath.Join(s.sharedDir, ProbesDirName) }

// SessionMovesDir holds unfinished session-move journals, shared/session-moves.
func (s SharedStore) SessionMovesDir() string {
	return filepath.Join(s.sharedDir, SessionMovesDirName)
}

// InodesFile is the inode map, shared/inodes.json.
func (s SharedStore) InodesFile() string { return filepath.Join(s.sharedDir, "inodes.json") }

// Subdir is the named subdirectory of the shared store.
func (s SharedStore) Subdir(name string) string { return filepath.Join(s.sharedDir, name) }

// codePoints splits p into the characters Python's str holds for the same
// path: valid UTF-8 decodes to its code points, and each byte that is not
// valid UTF-8 becomes U+DC00 plus the byte, as os.fsdecode's surrogateescape
// decodes it.
func codePoints(p string) []rune {
	out := make([]rune, 0, len(p))
	for i := 0; i < len(p); {
		r, size := utf8.DecodeRuneInString(p[i:])
		if r == utf8.RuneError && size == 1 {
			r = 0xdc00 + rune(p[i])
		}
		out = append(out, r)
		i += size
	}
	return out
}

func isASCIIAlnum(r rune) bool {
	return (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9')
}

// EncodePathUntruncated is Claude Code's store-dir sanitizer before its length
// limit applies. Every UTF-16 code unit outside [a-zA-Z0-9] becomes '-', so a
// character outside the Basic Multilingual Plane becomes two dashes. The
// encoding is lossy ('/', '.', '_', a space, and '-' all become '-'), and it
// distributes over path joins: encode(a + "/" + b) is encode(a) + "-" +
// encode(b).
func EncodePathUntruncated(p string) string {
	out := make([]byte, 0, len(p))
	for _, r := range codePoints(p) {
		switch {
		case isASCIIAlnum(r):
			out = append(out, byte(r))
		case r > 0xffff:
			out = append(out, '-', '-')
		default:
			out = append(out, '-')
		}
	}
	return string(out)
}

// EncodePath is the name of the store dir Claude Code keeps a path's sessions
// in: EncodePathUntruncated, and when that is longer than ProjectDirNameLimit,
// its first ProjectDirNameLimit characters, '-', and the base-36 hash of p.
func EncodePath(p string) string {
	name := EncodePathUntruncated(p)
	if len(name) <= ProjectDirNameLimit {
		return name
	}
	return name[:ProjectDirNameLimit] + "-" + pathHashBase36(p)
}

const base36Digits = "0123456789abcdefghijklmnopqrstuvwxyz"

// pathHashBase36 is Claude Code's store-dir name hash of p: a 31-multiplier
// hash over the UTF-16 code units of p, kept to a signed 32-bit integer after
// every step (JavaScript's "| 0"), whose absolute value is written in base 36
// (Math.abs(h).toString(36)).
func pathHashBase36(p string) string {
	var h uint32
	for _, r := range codePoints(p) {
		if r > 0xffff {
			v := uint32(r - 0x10000)
			h = h*31 + (0xd800 + v>>10)
			h = h*31 + (0xdc00 + v&0x3ff)
		} else {
			h = h*31 + uint32(r)
		}
	}
	n := int64(int32(h))
	if n < 0 {
		n = -n
	}
	if n == 0 {
		return "0"
	}
	var digits []byte
	for n > 0 {
		digits = append(digits, base36Digits[n%36])
		n /= 36
	}
	for i, j := 0, len(digits)-1; i < j; i, j = i+1, j-1 {
		digits[i], digits[j] = digits[j], digits[i]
	}
	return string(digits)
}
