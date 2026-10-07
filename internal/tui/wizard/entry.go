package wizard

import (
	"context"
	"errors"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// tokenMask is drawn for every character of a token being entered.
const tokenMask = "*"

// entryAreaWidth matches the widgets' centered field area.
const entryAreaWidth = 60

// hintEntry is the token entry page's hint.
const hintEntry = "paste or type the token   enter: submit (empty aborts)   esc: cancel"

// tokenEntry is a fullscreen page asking for a token: the title, what is
// asked, and an error line from the previous attempt.
type tokenEntry struct {
	title  string
	prompt string
	errMsg string
}

func centeredCol(cols, width int) int {
	return max(1, (cols-width)/2)
}

func (e tokenEntry) render(t *terminal.Terminal, c widgets.Colors, typed int) error {
	rows, cols := t.Rows, t.Cols
	startRow := max(1, (rows-4)/2)
	left := centeredCol(cols, entryAreaWidth)
	var buf strings.Builder
	buf.WriteString(terminal.ClearScreen)
	buf.WriteString(terminal.MoveTo(startRow, centeredCol(cols, utf8.RuneCountInString(e.title))))
	buf.WriteString(terminal.Bold + c.FormsTitleFg + e.title + terminal.Reset)
	buf.WriteString(terminal.MoveTo(startRow+2, left) + c.FormsFieldFg + e.prompt + terminal.Reset)
	// The mask line shows how many characters were taken, never the
	// characters; a long token is shown by its count past the area.
	shown := min(typed, entryAreaWidth-2)
	mask := "[" + strings.Repeat(tokenMask, shown) + terminal.Reset + c.FormsCursorFg + "_" + terminal.Reset + c.FormsFieldFg + "]"
	buf.WriteString(terminal.MoveTo(startRow+3, left) + c.FormsFieldFg + mask + terminal.Reset)
	if e.errMsg != "" {
		buf.WriteString(terminal.MoveTo(rows-1, centeredCol(cols, utf8.RuneCountInString(e.errMsg))))
		buf.WriteString(terminal.Bold + c.FormsErrorFg + e.errMsg + terminal.Reset)
	}
	buf.WriteString(terminal.MoveTo(rows, 2) + c.FormsHintFg + hintEntry + terminal.Reset)
	return t.Write(buf.String())
}

// errEntryCancelled is returned by readToken when Escape cancels the entry.
var errEntryCancelled = errors.New("token entry cancelled")

// readToken shows the entry page and reads a token without ever drawing its
// characters. Enter returns what was typed with all whitespace removed,
// including the spaces a line-wrapped terminal copy puts inside it, so an
// empty answer is "". Escape (or a Ctrl-C byte) returns errEntryCancelled.
// It redraws on a resize; when ctx is done it returns the cancellation cause.
func readToken(ctx context.Context, t *terminal.Terminal, c widgets.Colors, e tokenEntry) (string, error) {
	var chars []rune
	for {
		if err := e.render(t, c, len(chars)); err != nil {
			return "", err
		}
		key, err := t.ReadKey(ctx)
		if err != nil {
			return "", err
		}
		switch key {
		case terminal.KeyEnter:
			return strings.Join(strings.Fields(string(chars)), ""), nil
		case terminal.KeyEsc, terminal.KeyCtrlC:
			return "", errEntryCancelled
		case terminal.KeyBackspace:
			if len(chars) > 0 {
				chars = chars[:len(chars)-1]
			}
			continue
		}
		if r, ok := key.Char(); ok && unicode.IsPrint(r) {
			chars = append(chars, r)
		}
	}
}
