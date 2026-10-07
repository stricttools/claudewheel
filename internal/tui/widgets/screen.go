package widgets

import (
	"context"
	"errors"
	"fmt"

	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// RequireTerminal refuses a screen when nobody is at a terminal to answer
// it (terminal.HasControllingTerminal), so it never waits for keys nobody
// presses; what names the screen for the refusal.
func RequireTerminal(what string) error {
	if !terminal.HasControllingTerminal() {
		return fmt.Errorf("%s needs someone at a terminal, and stdin is not a terminal or /dev/tty does not open", what)
	}
	return nil
}

// OpenRawScreen opens the terminal in cbreak mode on the alternate screen.
// The caller defers t.Close, which restores the terminal.
func OpenRawScreen() (*terminal.Terminal, error) {
	t, err := terminal.Open()
	if err != nil {
		return nil, err
	}
	if err := t.EnterRaw(true); err != nil {
		return nil, errors.Join(err, t.Close())
	}
	return t, nil
}

// OpenScreen refuses when nobody is at a terminal (RequireTerminal, what
// naming the screen), loads the colors of the configured theme (asking the
// terminal for its background first when the theme is "auto"), then opens
// the terminal in cbreak mode on the alternate screen. The caller defers
// t.Close, which restores the terminal.
func OpenScreen(ctx context.Context, ws workspace.Workspace, theme, what string) (*terminal.Terminal, Colors, error) {
	if err := RequireTerminal(what); err != nil {
		return nil, Colors{}, err
	}
	colors, err := LoadColors(ctx, ws, theme)
	if err != nil {
		return nil, Colors{}, err
	}
	t, err := OpenRawScreen()
	if err != nil {
		return nil, Colors{}, err
	}
	return t, colors, nil
}
