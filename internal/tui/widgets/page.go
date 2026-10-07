package widgets

import (
	"context"
	"errors"
	"strings"

	"github.com/stricttools/claudewheel/internal/terminal"
)

// HintAnyKey is the hint of a page that any key closes.
const HintAnyKey = "press any key to continue"

// Page is a fullscreen page of text: a centered title, the lines in the
// centered field area, and the hint on the bottom row.
type Page struct {
	Title string
	Lines []string
	Hint  string
}

func renderPage(t *terminal.Terminal, c Colors, p Page) error {
	rows, cols := t.Rows, t.Cols
	startRow := max(1, (rows-(2+len(p.Lines)))/2)
	var buf strings.Builder
	buf.WriteString(terminal.ClearScreen)
	buf.WriteString(terminal.MoveTo(startRow, centered(cols, textWidth(p.Title))))
	buf.WriteString(terminal.Bold + c.FormsTitleFg + p.Title + terminal.Reset)
	leftCol := centered(cols, fieldAreaWidth)
	for i, line := range p.Lines {
		buf.WriteString(terminal.MoveTo(startRow+2+i, leftCol) + c.FormsFieldFg + line + terminal.Reset)
	}
	buf.WriteString(terminal.MoveTo(rows, 2) + c.FormsHintFg + p.Hint + terminal.Reset)
	return t.Write(buf.String())
}

// ShowPage draws the page and returns the next key pressed, redrawing on a
// resize. An empty hint is an error: say what a key does, or use
// HintAnyKey. When ctx is done it returns the cancellation cause.
func ShowPage(ctx context.Context, t *terminal.Terminal, c Colors, p Page) (terminal.Key, error) {
	if err := requireRaw(t); err != nil {
		return "", err
	}
	if p.Hint == "" {
		return "", errors.New("a page needs a hint saying what the keys do")
	}
	for {
		if err := renderPage(t, c, p); err != nil {
			return "", err
		}
		key, err := t.ReadKey(ctx)
		if err != nil {
			return "", err
		}
		if key == terminal.KeyResize || isNotification(key) {
			continue
		}
		return key, nil
	}
}

// Answer is the answer to a confirmation. The zero value is no answer.
type Answer int

const (
	// Accept: y or Y.
	Accept Answer = iota + 1
	// Decline: n or N.
	Decline
	// Skip: Escape, "not answering now"; what that means is the asking
	// surface's to say.
	Skip
)

// String names the answer.
func (a Answer) String() string {
	switch a {
	case Accept:
		return "accept"
	case Decline:
		return "decline"
	case Skip:
		return "skip"
	}
	return "no answer"
}

// ConfirmAnswer maps a key to the confirmation answer it gives: y or Y
// accepts, n or N declines, and Escape (or a Ctrl-C byte) skips. Every other
// key, Enter included, gives none. Every confirmation in the TUI decides
// through this mapping, including surfaces that run their own key loop.
func ConfirmAnswer(key terminal.Key) (Answer, bool) {
	switch key {
	case "y", "Y":
		return Accept, true
	case "n", "N":
		return Decline, true
	case terminal.KeyEsc, terminal.KeyCtrlC:
		return Skip, true
	}
	return 0, false
}

// ConfirmHint is the hint line of a confirmation, naming what each answer
// does on this surface, such as ConfirmHint("delete them", "keep them",
// "ask next launch").
func ConfirmHint(accept, decline, skip string) string {
	return "y: " + accept + "   n: " + decline + "   esc: " + skip
}

// Confirmation is a confirm page: what is asked, and what each answer does
// on this surface, for the hint line.
type Confirmation struct {
	Title   string
	Lines   []string
	Accept  string
	Decline string
	Skip    string
}

// Confirm draws the confirmation as a fullscreen page and waits for an
// answer through ConfirmAnswer: keys that give none, Enter included, do
// nothing. It redraws on a resize, and when ctx is done it returns the
// cancellation cause.
func Confirm(ctx context.Context, t *terminal.Terminal, c Colors, q Confirmation) (Answer, error) {
	if err := requireRaw(t); err != nil {
		return 0, err
	}
	if q.Accept == "" || q.Decline == "" || q.Skip == "" {
		return 0, errors.New("a confirmation must say what accepting, declining, and skipping do")
	}
	page := Page{Title: q.Title, Lines: q.Lines, Hint: ConfirmHint(q.Accept, q.Decline, q.Skip)}
	for {
		if err := renderPage(t, c, page); err != nil {
			return 0, err
		}
		key, err := t.ReadKey(ctx)
		if err != nil {
			return 0, err
		}
		if answer, ok := ConfirmAnswer(key); ok {
			return answer, nil
		}
	}
}
