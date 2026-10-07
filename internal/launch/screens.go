package launch

import (
	"context"
	"errors"
	"io/fs"
	"syscall"

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

// open opens the terminal in cbreak mode on the alternate screen. The caller
// defers Close, which restores it.
func (s *screens) open(ctx context.Context) (*terminal.Terminal, error) {
	if !s.resolved {
		c, err := widgets.LoadColors(ctx, s.ws, s.theme)
		if err != nil {
			return nil, err
		}
		s.colors, s.resolved = c, true
	}
	t, err := terminal.Open()
	if err != nil {
		return nil, err
	}
	if err := t.EnterRaw(true); err != nil {
		return nil, errors.Join(err, t.Close())
	}
	return t, nil
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

// isNotDir reports a path lookup that met a file where a directory was
// expected.
func isNotDir(err error) bool {
	var pe *fs.PathError
	return errors.As(err, &pe) && errors.Is(pe.Err, syscall.ENOTDIR)
}
