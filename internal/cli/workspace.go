package cli

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/pathstat"
)

// registerWorkspace adds the commands that show, edit, and convert the
// workspace's own files: show, config, reset-options, stats, and
// upgrade-workspace.
func registerWorkspace(app registrar) {
	readOnlyCommand(app, "show",
		"print a summary of current segment selections, theme, and recent directories",
		handleShow)

	mutatingCommand(app, "config",
		"open the ~/.claudewheel/ config directory in the editor $EDITOR names; refuses when $EDITOR is unset",
		handleConfig)

	mutatingCommand(app, "reset-options",
		"replace options.json with the default options a first run writes, dropping every value added to it since",
		handleResetOptions)

	readOnlyCommand(app, "stats",
		"report the shared store's file count and size, by top-level entry",
		handleStats)

	mutatingCommand(app, "upgrade-workspace",
		"convert a workspace an older claudewheel wrote: remove the retired keys (_schema_version from config.json, scratchpad_snooze_until from state.json, and a vanilla_guardrails_opt_in that is not a boolean) and add the keys the defaults declare that a file lacks, never changing a value already present. Commands refuse a workspace that needs converting and name this command. Every file is checked to convert before any is written; preview the changes with --dry-run",
		handleUpgradeWorkspace)
}

func handleShow(c *call, kw map[string]interface{}) error {
	cfg, err := c.appConfig()
	if err != nil {
		return err
	}
	enabled := map[string]bool{}
	for _, key := range cfg.Config.EnabledSegments {
		enabled[key] = true
	}
	var shown []appconfig.Segment
	labelWidth := 0
	for _, seg := range cfg.Segments {
		if enabled[seg.Key] {
			shown = append(shown, seg)
			labelWidth = max(labelWidth, utf8.RuneCountInString(seg.Label))
		}
	}
	c.say("claudewheel state:")
	for _, seg := range shown {
		value, ok := cfg.State.LastConfig[seg.Key]
		if !ok {
			value = "<unset>"
		}
		c.sayf("  %-*s %s", labelWidth+1, seg.Label+":", value)
	}
	c.say("")
	c.say("Theme: " + cfg.Config.Theme)
	flags := "<none>"
	if len(cfg.Config.DefaultFlags) > 0 {
		flags = strings.Join(cfg.Config.DefaultFlags, " ")
	}
	c.say("Default flags: " + flags)
	healthCheck := "False"
	if cfg.Config.HealthCheckOnLaunch {
		healthCheck = "True"
	}
	c.say("Health check on launch: " + healthCheck)
	recent := cfg.State.RecentDirs
	if len(recent) == 0 {
		c.say("Recent dirs: <none>")
	} else {
		first := recent[:min(5, len(recent))]
		c.sayf("Recent dirs (%d of %d):", len(first), len(recent))
		for _, d := range first {
			c.say("  " + d)
		}
	}
	c.sayf("Launch count: %d", cfg.State.LaunchCount)
	return nil
}

func handleConfig(c *call, kw map[string]interface{}) error {
	editor, ok := os.LookupEnv("EDITOR")
	if !ok || editor == "" {
		return errors.New("$EDITOR is not set: set it to the editor to open " + c.ws.Root() + " in")
	}
	if _, err := os.Stat(c.ws.Root()); err != nil {
		return fmt.Errorf("cannot open %s: %w (`claudewheel launch` creates it)", c.ws.Root(), err)
	}
	cwd, err := os.Getwd()
	if err != nil {
		return err
	}
	return c.fx.Exec(effects.Cmd{Argv: []string{editor, c.ws.Root()}, Dir: cwd, Env: os.Environ()})
}

func handleResetOptions(c *call, kw map[string]interface{}) error {
	// The app config is deliberately not opened: replacing options.json is
	// how a corrupt one is repaired, and opening would refuse it.
	if _, err := os.Stat(c.ws.Root()); err != nil {
		return fmt.Errorf("cannot reset %s: %w (`claudewheel launch` creates the workspace)", c.ws.OptionsFile(), err)
	}
	if err := appconfig.WriteDefaultOptions(c.fx, c.ws); err != nil {
		return err
	}
	verb := "Wrote"
	if c.previewing() {
		verb = "Would write"
	}
	c.sayf("%s the default options to %s", verb, c.ws.OptionsFile())
	return nil
}

func handleStats(c *call, kw map[string]interface{}) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	log := func(line string) { c.say("[stats] " + line) }
	shared := c.ws.SharedDir()
	info, err := os.Stat(shared)
	if pathstat.NotFoundOrParentNotDirectory(err) || (err == nil && !info.IsDir()) {
		log("shared store not found")
		return nil
	}
	if err != nil {
		return err
	}
	log("shared store: " + shared)
	entries, err := os.ReadDir(shared)
	if err != nil {
		return err
	}
	var totalFiles, totalBytes int64
	for _, entry := range entries {
		files, bytes, counted, err := entryStats(filepath.Join(shared, entry.Name()))
		if err != nil {
			return err
		}
		if !counted {
			continue
		}
		log(fmt.Sprintf("  %-20s %6d files  %10.1f KB", entry.Name(), files, float64(bytes)/1024))
		totalFiles += files
		totalBytes += bytes
	}
	log(fmt.Sprintf("  %-20s %6d files  %10.1f KB", "TOTAL", totalFiles, float64(totalBytes)/1024))
	log("done")
	return nil
}

// entryStats counts one top-level entry of the shared store: a directory
// (not a link to one) by the regular files under it, links not followed; a
// file, or a link to one, as one file of its target's size. Anything else
// (a link to a directory, a dangling link) is not counted.
func entryStats(path string) (files, bytes int64, counted bool, err error) {
	linfo, err := os.Lstat(path)
	if err != nil {
		return 0, 0, false, err
	}
	if linfo.IsDir() {
		err := filepath.WalkDir(path, func(p string, d fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if !d.Type().IsRegular() {
				return nil
			}
			info, err := d.Info()
			if err != nil {
				return err
			}
			files++
			bytes += info.Size()
			return nil
		})
		return files, bytes, err == nil, err
	}
	info, err := os.Stat(path)
	if pathstat.NotFoundOrParentNotDirectory(err) {
		return 0, 0, false, nil
	}
	if err != nil {
		return 0, 0, false, err
	}
	if !info.Mode().IsRegular() {
		return 0, 0, false, nil
	}
	return 1, info.Size(), true, nil
}

func handleUpgradeWorkspace(c *call, kw map[string]interface{}) error {
	changes, err := appconfig.Upgrade(c.fx, c.ws)
	if err != nil {
		return err
	}
	if len(changes) == 0 {
		c.say("The workspace needs no converting.")
		return nil
	}
	if c.previewing() {
		c.say("Would convert:")
	} else {
		c.say("Converted:")
	}
	for _, change := range changes {
		c.say("  " + change.String())
	}
	return nil
}
