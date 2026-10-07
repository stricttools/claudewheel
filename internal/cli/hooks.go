package cli

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/hookscripts"
	"github.com/stricttools/claudewheel/internal/probe"
)

// registerHooks adds deploy-hooks.
func registerHooks(app registrar) {
	// "Name one script or pass --all" is split at the framework's boundary:
	// the at-least-one half is the deploy-target constraint, and the
	// exclusive half is the handler's, since a positional argument cannot be
	// a member of a selection.
	mutatingCommand(app, "deploy-hooks",
		"deploy built-in hook scripts, the heavy wrapper, and claudewheel-tool-scope (the shell prefix every launched session runs its commands through; a launch deploys it when it is missing) to the ~/.claudewheel/scripts/ directory, linking heavy into ~/.local/bin so it is on PATH, and install the probe runner's user service (claudewheel-probe-runner.service, in ~/.config/systemd/user, running this claudewheel binary), enabled and started; systemctl --user stop claudewheel-probe-runner.service stops it gracefully",
		handleDeployHooks,
		strictcli.WithArgs(strictcli.NewArg("name",
			"name of the specific hook script to deploy, or claudewheel-probe-runner.service to install the probe runner's service (omit to use --all)",
			strictcli.ArgOptional())),
		strictcli.WithFlags(
			strictcli.BoolFlag("all",
				"deploy every known hook script from the built-in registry and the probe runner's service at once; when omitted, the positional name selects one script, or the service by its name",
				strictcli.Optional()),
			strictcli.BoolFlag("force-overwrite",
				"overwrite existing hook scripts and the probe runner's unit on disk instead of skipping them (a rewritten unit restarts the service), and replace whatever stands at a PATH command's link (~/.local/bin/heavy); when omitted, an existing script or unit is left alone and a link path held by anything else is refused",
				strictcli.Optional())),
		strictcli.WithConstraints(strictcli.AtLeastOne("deploy-target",
			strictcli.Member("name", strictcli.WhenNonEmpty()),
			strictcli.Member("all", strictcli.WhenTrue()))))
}

// The past-tense actions deploy reports, in the conditional form a preview
// reports them in.
var wouldAction = map[hookscripts.Action]string{
	hookscripts.Created:     "would create",
	hookscripts.Overwritten: "would overwrite",
	hookscripts.Linked:      "would link",
	hookscripts.Relinked:    "would relink",
}

func handleDeployHooks(c *call, kw map[string]interface{}) error {
	name, _ := kwOptString(kw, "name")
	all := kwSwitch(kw, "all")
	force := kwSwitch(kw, "force_overwrite")
	if name != "" && all {
		return errors.New("--all and a positional name are mutually exclusive")
	}
	if name != "" && !hookscripts.IsScript(name) && name != probe.ServiceName {
		known := append(hookscripts.Names(), probe.ServiceName)
		return fmt.Errorf("unknown hook script: '%s' (known: %s)", name, strings.Join(known, ", "))
	}
	if _, err := c.appConfig(); err != nil {
		return err
	}
	var targets []string
	switch {
	case all:
		targets = hookscripts.Names()
	case hookscripts.IsScript(name):
		targets = []string{name}
	}

	scriptsDir := c.ws.ScriptsDir()
	scripts, err := hookscripts.DeployScripts(c.fx, targets, scriptsDir, force)
	if err != nil {
		return err
	}
	for _, s := range scripts {
		switch {
		case s.Action == hookscripts.Exists:
			c.say("already exists: " + s.Path)
		case c.previewing():
			c.sayf("%s: %s", wouldAction[s.Action], s.Path)
		default:
			c.sayf("%s: %s", s.Action, s.Path)
		}
	}

	links, err := hookscripts.LinkPathCommands(c.fx, targets, scriptsDir, c.ws.BinDir(), force)
	if err != nil {
		return err
	}
	refused := false
	for _, l := range links {
		switch {
		case l.Action == hookscripts.Exists:
			c.sayf("already linked: %s -> %s", l.Link, l.Target)
		case l.Action == hookscripts.Foreign:
			c.fail(fmt.Sprintf("%s exists and is not a link to %s, so the command on PATH is not the one claudewheel deploys; pass --force-overwrite to replace it with that link", l.Link, l.Target))
			refused = true
		case c.previewing():
			c.sayf("%s: %s -> %s", wouldAction[l.Action], l.Link, l.Target)
		default:
			c.sayf("%s: %s -> %s", l.Action, l.Link, l.Target)
		}
	}

	if all || name == probe.ServiceName {
		if err := deployService(c, force); err != nil {
			return err
		}
	}
	if refused {
		return exitStatus(1)
	}
	return nil
}

// deployService installs the probe runner's unit, which runs the binary
// executing now.
func deployService(c *call, force bool) error {
	executable, err := os.Executable()
	if err != nil {
		return fmt.Errorf("cannot find this claudewheel binary for the probe runner's unit: %w", err)
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil {
		return fmt.Errorf("cannot resolve this claudewheel binary for the probe runner's unit: %w", err)
	}
	unit, action, err := hookscripts.DeployService(c.fx, c.ws.SystemdUserDir(), executable, force)
	var exitErr *effects.ExitError
	if errors.As(err, &exitErr) {
		return fmt.Errorf("%s failed with exit status %d; %s is not running", exitErr.Command, exitErr.Code, probe.ServiceName)
	}
	if err != nil {
		return err
	}
	switch {
	case action == hookscripts.Exists:
		c.sayf("already exists: %s (enabled and started)", unit)
	case c.previewing():
		c.sayf("%s: %s (and enable and restart it)", wouldAction[action], unit)
	default:
		c.sayf("%s: %s (enabled and restarted)", action, unit)
	}
	return nil
}
