package cli

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/install"
)

// registerVersions adds the commands managing installed Claude Code
// versions: versions, install, and uninstall. They act outside the
// workspace (~/.local/share/claude/versions and ~/.local/bin/claude), so
// they do not open the app config.
func registerVersions(app registrar) {
	readOnlyCommand(app, "versions",
		"list all installed Claude Code versions, marking the current symlink target",
		handleVersions)

	mutatingCommand(app, "install",
		"download and install a specific Claude Code version",
		handleInstall,
		strictcli.WithGrants(strictcli.Grant{
			Name:   "download",
			Reason: "installs an executable fetched from the Claude Code release bucket",
			Kind:   strictcli.NetMutate,
		}),
		strictcli.WithArgs(strictcli.NewArg("version",
			"semver version string to download and install (e.g. 2.1.119)",
			strictcli.ArgRequired())))

	mutatingCommand(app, "uninstall",
		"delete an installed Claude Code version binary from the versions directory",
		handleUninstall,
		strictcli.WithArgs(strictcli.NewArg("version",
			"semver version string to remove (refuses if it is the current symlink target)",
			strictcli.ArgRequired())))
}

func handleVersions(c *call, kw map[string]interface{}) error {
	locator := install.LocatorFor(c.ws)
	versions, err := locator.InstalledVersions()
	if err != nil {
		return err
	}
	current := ""
	if target, ok := locator.SymlinkTarget(); ok {
		current = filepath.Base(target)
	}
	if len(versions) == 0 {
		c.say("No versions found in " + locator.VersionsDir)
		return nil
	}
	for _, v := range versions {
		suffix := ""
		if v == current {
			suffix = " (current)"
		}
		c.sayf("  %s%s", v, suffix)
	}
	return nil
}

func handleInstall(c *call, kw map[string]interface{}) error {
	version := kwString(kw, "version")
	locator := install.LocatorFor(c.ws)
	verb := "Downloading"
	if c.previewing() {
		verb = "Would download"
	}
	c.info(fmt.Sprintf("%s Claude Code %s...", verb, version))
	// The progress line is redrawn in place on stderr, so it never mixes
	// into the command's answer.
	progressShown := false
	progress := func(downloaded, total int64) {
		if total <= 0 || c.ctx.Quiet() {
			return
		}
		const mib = 1024 * 1024
		fmt.Fprintf(os.Stderr, "\r  %.0f/%.0f MB (%d%%)", float64(downloaded)/mib, float64(total)/mib, downloaded*100/total)
		progressShown = true
	}
	dest, err := install.Install(c.fx, locator, version, progress)
	if progressShown {
		fmt.Fprintln(os.Stderr)
	}
	if err != nil {
		return fmt.Errorf("installation failed: %w", err)
	}
	if c.previewing() {
		c.say("Would install to " + dest)
	} else {
		c.say("Installed to " + dest)
	}
	return nil
}

func handleUninstall(c *call, kw map[string]interface{}) error {
	version := kwString(kw, "version")
	target, err := install.Uninstall(c.fx, install.LocatorFor(c.ws), version)
	if err != nil {
		return err
	}
	verb := "Uninstalled"
	if c.previewing() {
		verb = "Would uninstall"
	}
	c.sayf("%s %s (%s)", verb, version, target)
	return nil
}
