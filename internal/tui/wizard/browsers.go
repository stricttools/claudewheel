package wizard

import (
	"path/filepath"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/pathstat"
)

// Browser is one installed web browser: the executable the auth URL is
// opened with, and the name the picker shows.
type Browser struct {
	Path string
	Name string
}

// browserSource is one place a browser can be installed from: the name it
// has there and the name the picker shows.
type browserSource struct {
	file string
	name string
}

// nativeBrowsers are searched on PATH, in priority order.
func nativeBrowsers() []browserSource {
	return []browserSource{
		{"firefox", "Firefox"},
		{"chromium", "Chromium"},
		{"chromium-browser", "Chromium"},
		{"google-chrome", "Chrome"},
		{"google-chrome-stable", "Chrome"},
		{"brave-browser", "Brave"},
		{"brave", "Brave"},
		{"microsoft-edge", "Edge"},
		{"microsoft-edge-stable", "Edge"},
		{"opera", "Opera"},
		{"vivaldi", "Vivaldi"},
		{"vivaldi-stable", "Vivaldi"},
		{"epiphany", "GNOME Web"},
		{"midori", "Midori"},
		{"falkon", "Falkon"},
		{"qutebrowser", "Qutebrowser"},
	}
}

// flatpakBrowsers are flatpak application ids, looked up in the export
// directories.
func flatpakBrowsers() []browserSource {
	return []browserSource{
		{"org.mozilla.firefox", "Firefox"},
		{"com.google.Chrome", "Chrome"},
		{"com.brave.Browser", "Brave"},
		{"io.github.ungoogled_software.ungoogled_chromium", "Ungoogled Chromium"},
		{"com.microsoft.Edge", "Edge"},
		{"com.opera.Opera", "Opera"},
		{"com.vivaldi.Vivaldi", "Vivaldi"},
	}
}

// snapBrowsers are snap names, looked up in the snap binary directory.
func snapBrowsers() []browserSource {
	return []browserSource{
		{"firefox", "Firefox"},
		{"chromium", "Chromium"},
		{"brave", "Brave"},
		{"opera", "Opera"},
	}
}

// BrowserDirs are the directories browser detection looks in besides PATH.
type BrowserDirs struct {
	// PathList is the search list native browsers are looked up on
	// (effects.LookPath).
	PathList string
	// FlatpakExports are the flatpak export directories, system first.
	FlatpakExports []string
	// SnapBin is the snap binary directory.
	SnapBin string
}

// SystemBrowserDirs returns the directories of this machine for the user
// whose home directory is home: PATH, /var/lib/flatpak/exports/bin, then
// ~/.local/share/flatpak/exports/bin, and /snap/bin.
func SystemBrowserDirs(home string) BrowserDirs {
	return BrowserDirs{
		PathList: effects.SearchPath(nil),
		FlatpakExports: []string{
			"/var/lib/flatpak/exports/bin",
			filepath.Join(home, ".local", "share", "flatpak", "exports", "bin"),
		},
		SnapBin: "/snap/bin",
	}
}

// DetectBrowsers finds the installed web browsers: native ones on PATH, then
// flatpak exports, then snaps. A browser name is listed once; the first
// source that has it wins. A path that cannot be checked for a reason other
// than its absence is an error.
func DetectBrowsers(dirs BrowserDirs) ([]Browser, error) {
	var found []Browser
	seen := map[string]bool{}
	add := func(path, name string) {
		found = append(found, Browser{Path: path, Name: name})
		seen[name] = true
	}

	for _, b := range nativeBrowsers() {
		if seen[b.name] {
			continue
		}
		path, ok, err := effects.LookPath(b.file, dirs.PathList)
		if err != nil {
			return nil, err
		}
		if ok {
			add(path, b.name)
		}
	}

	for _, b := range flatpakBrowsers() {
		if seen[b.name] {
			continue
		}
		for _, dir := range dirs.FlatpakExports {
			candidate := filepath.Join(dir, b.file)
			ok, err := pathstat.Exists(candidate)
			if err != nil {
				return nil, err
			}
			if ok {
				add(candidate, b.name)
				break
			}
		}
	}

	for _, b := range snapBrowsers() {
		if seen[b.name] {
			continue
		}
		candidate := filepath.Join(dirs.SnapBin, b.file)
		ok, err := pathstat.Exists(candidate)
		if err != nil {
			return nil, err
		}
		if ok {
			add(candidate, b.name)
		}
	}
	return found, nil
}
