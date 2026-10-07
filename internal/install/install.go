// Package install downloads, verifies, and installs Claude Code binaries from
// Google Cloud Storage into ~/.local/share/claude/versions/<version>, locates
// the installed binaries and the active claude link, uninstalls a version
// (never the active one), and orders version names.
package install

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// GCSBase is the bucket path Claude Code releases are published under.
const GCSBase = "https://storage.googleapis.com/" +
	"claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/" +
	"claude-code-releases"

// DownloadTimeout bounds the wait for the binary download's response and
// every pause between two chunks of it (the binary is about 235 MB).
const DownloadTimeout = 300 * time.Second

// ManifestTimeout bounds the manifest request.
const ManifestTimeout = 10 * time.Second

// userAgent is sent with every request to the bucket.
const userAgent = "claudewheel"

// defaultBinaryName is the file name of a platform's binary when its
// manifest entry names none.
const defaultBinaryName = "claude"

// Platform returns this machine's platform as the manifest names it, such as
// linux-x64 or darwin-arm64. An architecture or system the manifest has no
// name for is passed through under Go's name, so it matches no entry.
func Platform() string {
	arch := runtime.GOARCH
	switch arch {
	case "amd64":
		arch = "x64"
	}
	system := runtime.GOOS
	if system == "windows" {
		system = "win32"
	}
	return system + "-" + arch
}

// PlatformEntry is one platform's entry in a version manifest.
type PlatformEntry struct {
	// Checksum is the sha256 hex of the binary.
	Checksum string
	// Binary is the binary's file name in the bucket.
	Binary string
	// Size is the binary's size in bytes; 0 when the manifest omits it.
	Size int64
}

// Manifest is a version manifest: its platform entries by platform name.
// The manifest is Google's file, so unknown keys are ignored and only the
// fields used are checked.
type Manifest struct {
	Version   string
	Platforms *jsonfile.Object
}

// FetchManifest fetches and checks the manifest of version. The request
// changes nothing, so it runs in every mode, --dry-run included: a preview
// needs it to name the file it would write. A present "platforms" that is
// not an object is a malformed manifest.
func FetchManifest(fx *effects.FX, version string) (Manifest, error) {
	resp, err := fx.HTTPRead(effects.Request{
		Method:  "GET",
		URL:     GCSBase + "/" + version + "/manifest.json",
		Header:  map[string]string{"User-Agent": userAgent},
		Timeout: ManifestTimeout,
	})
	if err != nil {
		var statusErr *effects.StatusError
		if errors.As(err, &statusErr) {
			return Manifest{}, fmt.Errorf("version %s not found on server (HTTP %d)", version, statusErr.Status)
		}
		return Manifest{}, fmt.Errorf("failed to fetch manifest for %s: %w", version, err)
	}
	tree, err := jsonfile.Decode(resp.Body)
	if err != nil {
		return Manifest{}, fmt.Errorf("failed to fetch manifest for %s: %w", version, err)
	}
	top, ok := tree.(*jsonfile.Object)
	if !ok {
		return Manifest{}, fmt.Errorf("manifest for %s is malformed: expected a JSON object, got %s", version, jsonfile.Describe(tree))
	}
	platforms := jsonfile.NewObject()
	if v, present := top.Get("platforms"); present {
		if platforms, ok = v.(*jsonfile.Object); !ok {
			return Manifest{}, fmt.Errorf(`manifest for %s is malformed: "platforms" must be a JSON object, got %s`, version, jsonfile.Describe(v))
		}
	}
	return Manifest{Version: version, Platforms: platforms}, nil
}

// Entry returns the manifest entry of platform. A platform the manifest does
// not list is an error naming the ones it does; an entry that is not an
// object, has no string checksum, a binary that is not a string, or a size
// that is not a non-negative integer is malformed.
func (m Manifest) Entry(platform string) (PlatformEntry, error) {
	v, ok := m.Platforms.Get(platform)
	if !ok {
		available := m.Platforms.Keys()
		sort.Strings(available)
		return PlatformEntry{}, fmt.Errorf("platform %s not available for %s. Available: %s",
			platform, m.Version, strings.Join(available, ", "))
	}
	entry, ok := v.(*jsonfile.Object)
	if !ok {
		return PlatformEntry{}, fmt.Errorf("manifest entry for %s in %s is malformed: expected a JSON object, got %s",
			platform, m.Version, jsonfile.Describe(v))
	}
	malformed := func(what string) error {
		return fmt.Errorf("manifest entry for %s in %s is malformed: %s", platform, m.Version, what)
	}
	checksumValue, ok := entry.Get("checksum")
	if !ok {
		return PlatformEntry{}, fmt.Errorf("manifest entry for %s in %s is missing a checksum", platform, m.Version)
	}
	checksum, ok := checksumValue.(string)
	if !ok {
		return PlatformEntry{}, malformed("checksum is not a string")
	}
	out := PlatformEntry{Checksum: checksum, Binary: defaultBinaryName}
	if b, present := entry.Get("binary"); present {
		if out.Binary, ok = b.(string); !ok {
			return PlatformEntry{}, malformed("binary is not a string")
		}
	}
	if s, present := entry.Get("size"); present {
		n, isNumber := s.(json.Number)
		size, err := strconv.ParseInt(string(n), 10, 64)
		if !isNumber || err != nil || size < 0 {
			return PlatformEntry{}, malformed("size is not a non-negative integer")
		}
		out.Size = size
	}
	return out, nil
}

// stagingPath is where a download is written before it is renamed onto dest:
// dest's full name plus ".downloading", never a replaced suffix, which would
// make every patch release of one minor version share a file.
func stagingPath(dest string) string {
	return dest + ".downloading"
}

// Install downloads version for this platform into the staging file, checks
// its sha256 against the manifest, makes it executable, and renames it to
// l.BinaryFor(version), whose path it returns. progress, when not nil, is
// called after every chunk with the bytes so far and the manifest's size (0
// when unknown). The staging file is removed on any failure. Under --dry-run
// every operation is recorded (the request with resource claude-binary:<version>
// and grant download) and nothing is transferred or checked.
func Install(fx *effects.FX, l Locator, version string, progress func(downloaded, total int64)) (string, error) {
	if err := CheckVersionName(version); err != nil {
		return "", err
	}
	manifest, err := FetchManifest(fx, version)
	if err != nil {
		return "", err
	}
	platform := Platform()
	entry, err := manifest.Entry(platform)
	if err != nil {
		return "", err
	}
	dest := l.BinaryFor(version)
	staged := stagingPath(dest)

	if err := fx.MkdirAll(l.VersionsDir); err != nil {
		return "", err
	}
	sum := sha256.New()
	var downloaded int64
	err = fx.Download(effects.Request{
		Method:   "GET",
		URL:      GCSBase + "/" + version + "/" + platform + "/" + entry.Binary,
		Header:   map[string]string{"User-Agent": userAgent},
		Timeout:  DownloadTimeout,
		Resource: "claude-binary:" + version,
		Grant:    "download",
	}, staged, func(chunk []byte) {
		sum.Write(chunk)
		downloaded += int64(len(chunk))
		if progress != nil {
			progress(downloaded, entry.Size)
		}
	})
	if err != nil {
		return "", discardStaged(fx, staged, fmt.Errorf("failed to download %s: %w", version, err))
	}
	if !fx.Previewing() {
		actual := hex.EncodeToString(sum.Sum(nil))
		if actual != entry.Checksum {
			return "", discardStaged(fx, staged, fmt.Errorf("checksum mismatch for %s: expected %s..., got %s...",
				version, prefix16(entry.Checksum), prefix16(actual)))
		}
	}
	if err := fx.Chmod(staged, 0o755); err != nil {
		return "", discardStaged(fx, staged, fmt.Errorf("failed to install %s: %w", version, err))
	}
	if err := fx.Rename(staged, dest); err != nil {
		return "", discardStaged(fx, staged, fmt.Errorf("failed to install %s: %w", version, err))
	}
	return dest, nil
}

// discardStaged removes the staging file after a failed install and returns
// cause, joined with the removal's own failure when there is one.
func discardStaged(fx *effects.FX, staged string, cause error) error {
	if fx.Previewing() {
		return cause
	}
	if err := fx.RemoveIfExists(staged); err != nil {
		return errors.Join(cause, fmt.Errorf("could not remove %s: %w", staged, err))
	}
	return cause
}

// prefix16 is the first 16 bytes of s, or s when shorter.
func prefix16(s string) string {
	if len(s) > 16 {
		return s[:16]
	}
	return s
}
