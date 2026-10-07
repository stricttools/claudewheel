// Package cli builds the claudewheel command-line application: one file per
// command group, each adding its commands to the app NewApp builds.
package cli

import (
	"github.com/stricttools/strictcli/go/strictcli"
)

// appHelp is the app's one-line description, shown by `claudewheel --help`.
const appHelp = "A TUI Claude Code Launcher that lets you have more than one profile, manage sessions lifecycle, pick the exact CC version, model to use (even older unlisted ones), pick which GitHub account to use, etc."

// NewApp builds the claudewheel app stamped with version. Building it does no
// file, terminal, or network I/O, so `claudewheel --version` and
// `claudewheel help --json` stay fast and hermetic.
func NewApp(version string) *strictcli.App {
	app := strictcli.NewApp("claudewheel", version, appHelp)
	register(app)
	return app
}

// anywhereSwitches are the framework-owned flags strictcli takes anywhere
// before a bare "--", in front of the command or after it, and removes from
// the command line: the effects quartet and --json. None takes a value.
var anywhereSwitches = map[string]bool{
	"--dry-run":               true,
	"--approve-consequential": true,
	"--quiet":                 true,
	"--verbose":               true,
	"--json":                  true,
}

// FrameworkSwitch reports whether tok is a framework-owned flag that takes
// no value and that strictcli accepts in front of the command: one of
// anywhereSwitches, or --hermetic, which it takes only there. main steps
// over them when it inserts the launch command, so `claudewheel --dry-run
// stats` previews stats rather than a launch.
func FrameworkSwitch(tok string) bool {
	return anywhereSwitches[tok] || tok == "--hermetic"
}

// register adds every command group to app. Each group lives in its own file
// and exposes one function taking the app; a new group is one more call here.
func register(app *strictcli.App) {
	registerVersions(app)
	registerWorkspace(app)
	registerSessions(app)
	registerHooks(app)
	registerProbe(app)
	registerMaintenance(app)
	registerProfile(app)
	registerPermission(app)
	registerLaunch(app)
}
