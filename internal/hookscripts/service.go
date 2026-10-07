package hookscripts

import (
	"fmt"
	"path/filepath"
	"strings"
	"unicode"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/probe"
)

// unsafeInExecStart are the characters systemd would quote, escape, or expand
// (specifiers, environment variables) in an unquoted ExecStart path.
const unsafeInExecStart = "\"'\\%$;"

// ServiceUnit is the unit file of the probe runner's user service. It runs
// executable (the claudewheel binary; the caller decides which) as
// "probe run-service", restarts it when it fails, and starts it with the
// user's systemd manager, so it runs again after a reboot. systemctl --user
// stop stops it with SIGTERM, and the unit counts exit status 143 (the exit
// on SIGTERM) as success.
//
// The path is written unquoted, so a relative path, or one holding
// whitespace, a control character, or a character systemd would interpret,
// is refused.
func ServiceUnit(executable string) (string, error) {
	if !filepath.IsAbs(executable) {
		return "", fmt.Errorf("the probe runner's executable must be an absolute path: %q", executable)
	}
	for _, r := range executable {
		if unicode.IsSpace(r) || unicode.IsControl(r) || strings.ContainsRune(unsafeInExecStart, r) {
			return "", fmt.Errorf("the probe runner's executable path %q holds %q, which a systemd ExecStart line would not take literally", executable, r)
		}
	}
	return "# Deployed by claudewheel ('claudewheel deploy-hooks " + probe.ServiceName + "'); edits are overwritten.\n" +
		"[Unit]\n" +
		"Description=claudewheel probe runner: reports OOM kills to the Claude Code sessions they concern\n" +
		"\n" +
		"[Service]\n" +
		"Type=simple\n" +
		"ExecStart=" + executable + " probe run-service\n" +
		"SuccessExitStatus=143\n" +
		"Restart=on-failure\n" +
		"RestartSec=5\n" +
		"\n" +
		"[Install]\n" +
		"WantedBy=default.target\n", nil
}

// DeployService writes the probe runner's unit into unitDir, enables it, and
// restarts it, so it runs the binary deployed now. A unit already there is
// left alone unless forceOverwrite is set: it is only enabled and started
// when it is not running, and reported as Exists. It returns the unit's path
// and what was done. A failing systemctl is an *effects.ExitError.
func DeployService(fx *effects.FX, unitDir, executable string, forceOverwrite bool) (string, Action, error) {
	unit, err := ServiceUnit(executable)
	if err != nil {
		return "", "", err
	}
	path := filepath.Join(unitDir, probe.ServiceName)
	present, err := exists(path)
	if err != nil {
		return "", "", err
	}
	if present && !forceOverwrite {
		if err := systemctl(fx, "enable", "--now", probe.ServiceName); err != nil {
			return "", "", err
		}
		return path, Exists, nil
	}
	action := Created
	if present {
		action = Overwritten
	}
	if err := fx.MkdirAll(unitDir); err != nil {
		return "", "", err
	}
	if err := fx.WriteFileAtomic(path, []byte(unit)); err != nil {
		return "", "", err
	}
	for _, args := range [][]string{
		{"daemon-reload"},
		{"enable", probe.ServiceName},
		{"restart", probe.ServiceName},
	} {
		if err := systemctl(fx, args...); err != nil {
			return "", "", err
		}
	}
	return path, action, nil
}

// systemctl runs systemctl --user with args, a nonzero exit being an error.
func systemctl(fx *effects.FX, args ...string) error {
	_, err := fx.Run(effects.Cmd{Argv: append([]string{"systemctl", "--user"}, args...), Check: true})
	return err
}
