// Command claudewheel launches Claude Code with a chosen profile, version,
// model, and session, and manages the profiles, sessions, guardrails, and
// probes behind those launches.
package main

import (
	"fmt"
	"os"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/cli"
)

// version is stamped at link time by scripts/build
// (-ldflags "-X main.version=..."); it has no fallback.
var version string

// launchCommand is the command a bare invocation runs: strictcli has no
// default-command declaration, so main inserts its name into the argument
// list before the app parses it.
const launchCommand = "launch"

// frameworkCommands are the top-level commands strictcli answers itself on
// every app; they are not in the app's command or group registry.
var frameworkCommands = []string{"help", "version"}

// passThroughFlags are the flags strictcli answers at the app level before
// any command: a launch is never inserted in front of them.
var passThroughFlags = map[string]bool{
	"--help":               true,
	"-h":                   true,
	"--version":            true,
	"-v":                   true,
	"--dump-schema":        true,
	"--mcp":                true,
	"--lint-framework-use": true,
}

func main() {
	if version == "" {
		fmt.Fprintln(os.Stderr, "error: this claudewheel binary carries no version: build it with scripts/build, which stamps the version recorded in package.json")
		os.Exit(1)
	}
	app := cli.NewApp(version)
	// strictcli's Run reads os.Args and takes no argument list, so the
	// launch insertion rewrites os.Args before it.
	os.Args = append([]string{os.Args[0]}, injectLaunch(os.Args[1:], routingNames(app))...)
	app.Run()
}

// routingNames is every top-level name the app dispatches itself: its
// commands, groups, and deprecated names, plus strictcli's framework commands.
// It is read off the registered app, so a command routes as itself the moment
// it is registered.
func routingNames(app *strictcli.App) map[string]bool {
	names := map[string]bool{}
	for name := range app.Commands() {
		names[name] = true
	}
	for name := range app.Groups() {
		names[name] = true
	}
	for name := range app.DeprecatedCommands() {
		names[name] = true
	}
	for _, name := range frameworkCommands {
		names[name] = true
	}
	return names
}

// injectLaunch returns args (without the program name) with the launch
// command inserted when no command is named: after the leading framework
// switches (cli.FrameworkSwitch), unless the first other token is a routed
// name or a flag strictcli answers at the app level.
func injectLaunch(args []string, routed map[string]bool) []string {
	lead := 0
	for lead < len(args) && cli.FrameworkSwitch(args[lead]) {
		lead++
	}
	if lead < len(args) && (routed[args[lead]] || passThroughFlags[args[lead]]) {
		return args
	}
	out := make([]string, 0, len(args)+1)
	out = append(out, args[:lead]...)
	out = append(out, launchCommand)
	return append(out, args[lead:]...)
}
