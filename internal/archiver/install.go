package archiver

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
)

// ReleaseBase is where the latest saferm release's assets and checksum
// manifest are downloaded from.
const ReleaseBase = "https://github.com/stricttools/saferm/releases/latest/download"

const (
	// ManifestTimeout bounds fetching the checksum manifest.
	ManifestTimeout = 30 * time.Second
	// DownloadTimeout bounds fetching the release asset.
	DownloadTimeout = 300 * time.Second
)

// userAgent is sent with every release request.
const userAgent = "claudewheel"

// InstallError is an install that could not be completed: saferm is still
// not installed.
type InstallError struct {
	msg string
}

func (e *InstallError) Error() string { return e.msg }

func installErrorf(format string, args ...any) *InstallError {
	return &InstallError{msg: fmt.Sprintf(format, args...)}
}

// Checksums is a release's checksum manifest: each asset's SHA-256 in hex,
// with the asset names in manifest order.
type Checksums struct {
	Names  []string
	SHA256 map[string]string
}

// FetchChecksums reads the latest release's checksums.txt, a declared read
// that runs in every mode: it names the version, the asset, and the digest.
func FetchChecksums(fx *effects.FX) (Checksums, error) {
	url := ReleaseBase + "/checksums.txt"
	resp, err := fx.HTTPRead(effects.Request{
		Method:  "GET",
		URL:     url,
		Header:  map[string]string{"User-Agent": userAgent},
		Timeout: ManifestTimeout,
	})
	if err != nil {
		var status *effects.StatusError
		if errors.As(err, &status) {
			return Checksums{}, installErrorf("could not read %s (HTTP %d)", url, status.Status)
		}
		return Checksums{}, installErrorf("could not read %s: %v", url, err)
	}
	manifest := Checksums{SHA256: map[string]string{}}
	for _, line := range strings.Split(strings.ToValidUTF8(string(resp.Body), "\uFFFD"), "\n") {
		parts := strings.Fields(line)
		if len(parts) != 2 {
			continue
		}
		if _, seen := manifest.SHA256[parts[1]]; !seen {
			manifest.Names = append(manifest.Names, parts[1])
		}
		manifest.SHA256[parts[1]] = parts[0]
	}
	if len(manifest.Names) == 0 {
		return Checksums{}, installErrorf("%s named no assets", url)
	}
	return manifest, nil
}

// Version returns the version the manifest's first saferm asset carries
// (saferm_<version>_<os>_<arch>.<ext>).
func (c Checksums) Version() (string, error) {
	for _, name := range c.Names {
		parts := strings.Split(name, "_")
		if len(parts) >= 4 && parts[0] == Saferm {
			return parts[1], nil
		}
	}
	return "", installErrorf("the checksum manifest names no %s asset", Saferm)
}

// AssetName is this platform's release asset for version, named as the
// release names it: saferm_<version>_<os>_<arch>.tar.gz, .zip on Windows.
func AssetName(version string) string {
	ext := "tar.gz"
	if runtime.GOOS == "windows" {
		ext = "zip"
	}
	return fmt.Sprintf("%s_%s_%s_%s.%s", Saferm, version, runtime.GOOS, runtime.GOARCH, ext)
}

// Install downloads, verifies, and installs the latest saferm as
// BinDir(root)/saferm, returning its path. The asset is installed only after
// its SHA-256 matched the manifest: a mismatch installs nothing. onProgress,
// when not nil, is called once with the downloaded size. The caller detects
// again afterwards, so the deletion proceeds only against a saferm that
// answered the probe.
func Install(fx *effects.FX, root string, onProgress func(done, total int64)) (string, error) {
	manifest, err := FetchChecksums(fx)
	if err != nil {
		return "", err
	}
	version, err := manifest.Version()
	if err != nil {
		return "", err
	}
	asset := AssetName(version)
	expected, ok := manifest.SHA256[asset]
	if !ok {
		available := append([]string(nil), manifest.Names...)
		sort.Strings(available)
		return "", installErrorf("%s %s publishes no asset for this platform (%s). Available: %s",
			Saferm, version, asset, strings.Join(available, ", "))
	}
	if strings.HasSuffix(asset, ".zip") {
		return "", installErrorf("claudewheel cannot unpack %s; install %s with one of:\n%s",
			asset, Saferm, (&Unavailable{}).Remedy())
	}

	url := ReleaseBase + "/" + asset
	resp, err := fx.HTTPRead(effects.Request{
		Method:  "GET",
		URL:     url,
		Header:  map[string]string{"User-Agent": userAgent},
		Timeout: DownloadTimeout,
	})
	if err != nil {
		return "", installErrorf("could not download %s: %v", url, err)
	}
	blob := resp.Body
	if onProgress != nil {
		onProgress(int64(len(blob)), int64(len(blob)))
	}

	sum := sha256.Sum256(blob)
	actual := hex.EncodeToString(sum[:])
	if actual != strings.ToLower(expected) {
		return "", installErrorf("checksum mismatch for %s: expected %s..., got %s...  Nothing was installed",
			asset, prefix(expected, 16), actual[:16])
	}

	binary, err := extractBinary(blob)
	if err != nil {
		return "", installErrorf("could not unpack %s: %v", asset, err)
	}

	dir := BinDir(root)
	dest := filepath.Join(dir, Saferm)
	staging := dest + ".downloading"
	if err := fx.MkdirAll(dir); err != nil {
		return "", err
	}
	if err := fx.WriteFile(staging, binary); err != nil {
		return "", err
	}
	if err := fx.Chmod(staging, 0o755); err != nil {
		return "", err
	}
	if err := fx.Rename(staging, dest); err != nil {
		return "", err
	}
	return dest, nil
}

// extractBinary returns the content of the regular file named saferm at the
// top of the gzipped tar archive blob.
func extractBinary(blob []byte) ([]byte, error) {
	gz, err := gzip.NewReader(bytes.NewReader(blob))
	if err != nil {
		return nil, err
	}
	defer gz.Close()
	tr := tar.NewReader(gz)
	for {
		header, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return nil, fmt.Errorf("it holds no `%s` entry", Saferm)
		}
		if err != nil {
			return nil, err
		}
		if header.Name != Saferm {
			continue
		}
		if header.Typeflag != tar.TypeReg {
			return nil, fmt.Errorf("its `%s` entry is not a regular file", Saferm)
		}
		return io.ReadAll(tr)
	}
}

// prefix returns the first n bytes of s, or s when it is shorter.
func prefix(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
