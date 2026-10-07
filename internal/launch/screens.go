package launch

import (
	"context"
	"errors"

	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// screens opens the terminal for the prompts a launch shows outside the bar,
// each in its own alternate-screen session, with the configured theme's
// colors resolved once.
type screens struct {
	ws    workspace.Workspace
	theme string
	// colors is resolved on the first open, before the terminal enters
	// cbreak mode (resolving "auto" asks the terminal).
	colors   widgets.Colors
	resolved bool
}

// prepare refuses when nobody is at a terminal to answer
// (widgets.RequireTerminal; what names the screen for that refusal), and
// resolves the colors on the first call.
func (s *screens) prepare(ctx context.Context, what string) error {
	if err := widgets.RequireTerminal(what); err != nil {
		return err
	}
	if !s.resolved {
		c, err := widgets.LoadColors(ctx, s.ws, s.theme)
		if err != nil {
			return err
		}
		s.colors, s.resolved = c, true
	}
	return nil
}

// openTerminal opens the terminal, not yet in cbreak mode, after prepare.
// The caller defers Close, which restores the terminal.
func (s *screens) openTerminal(ctx context.Context, what string) (*terminal.Terminal, error) {
	if err := s.prepare(ctx, what); err != nil {
		return nil, err
	}
	return terminal.Open()
}

// open opens the terminal in cbreak mode on the alternate screen for a
// prompt, after prepare. The caller defers Close, which restores it.
func (s *screens) open(ctx context.Context) (*terminal.Terminal, error) {
	if err := s.prepare(ctx, "this launch's prompt"); err != nil {
		return nil, err
	}
	return widgets.OpenRawScreen()
}

// confirm asks one confirmation on its own screen.
func (s *screens) confirm(ctx context.Context, q widgets.Confirmation) (answer widgets.Answer, err error) {
	t, err := s.open(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { err = errors.Join(err, t.Close()) }()
	return widgets.Confirm(ctx, t, s.colors, q)
}

// selection runs a selection list on its own screen; false when Escape
// cancelled it.
func (s *screens) selection(ctx context.Context, title string, options []widgets.Option) (key string, chosen bool, err error) {
	t, err := s.open(ctx)
	if err != nil {
		return "", false, err
	}
	defer func() { err = errors.Join(err, t.Close()) }()
	return widgets.RunSelection(ctx, t, s.colors, title, options, "")
}
