package terminal

import "fmt"

// ANSI escape sequences shared by the renderer, the widgets, and the
// terminal itself.
const (
	Esc = "\x1b"

	Reset = "\x1b[0m"
	Bold  = "\x1b[1m"
	Dim   = "\x1b[2m"
	// The terminal's own green and red, for state indicators whose meaning
	// is fixed ("still running", "stopped") rather than themed.
	Green = "\x1b[32m"
	Red   = "\x1b[31m"

	HideCursor   = "\x1b[?25l"
	ShowCursor   = "\x1b[?25h"
	AltScreenOn  = "\x1b[?1049h"
	AltScreenOff = "\x1b[?1049l"
	ClearScreen  = "\x1b[2J"
	ClearLine    = "\x1b[2K"

	mode2031On  = "\x1b[?2031h"
	mode2031Off = "\x1b[?2031l"
)

// CSI builds a control sequence from its parameter string: CSI("2J") is
// ESC [ 2 J.
func CSI(code string) string {
	return Esc + "[" + code
}

// MoveTo moves the cursor to a 1-based row and column.
func MoveTo(row, col int) string {
	return CSI(fmt.Sprintf("%d;%dH", row, col))
}

// FgRGB sets the foreground to a 24-bit color.
func FgRGB(r, g, b int) string {
	return CSI(fmt.Sprintf("38;2;%d;%d;%dm", r, g, b))
}

// BgRGB sets the background to a 24-bit color.
func BgRGB(r, g, b int) string {
	return CSI(fmt.Sprintf("48;2;%d;%d;%dm", r, g, b))
}
