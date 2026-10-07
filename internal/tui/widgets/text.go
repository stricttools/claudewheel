package widgets

import (
	"strings"
	"unicode/utf8"
)

// PageTextWidth is the width a page's prose is wrapped to (WrapText).
const PageTextWidth = 56

// RuneCount is the width of s in characters (code points), the unit every
// TUI width and cut is measured in.
func RuneCount(s string) int {
	return utf8.RuneCountInString(s)
}

// RunePrefix returns the first n characters of s: all of it when it is
// shorter, none when n is not positive.
func RunePrefix(s string, n int) string {
	if n <= 0 {
		return ""
	}
	r := []rune(s)
	if n >= len(r) {
		return s
	}
	return string(r[:n])
}

// RuneSuffix returns s without its first n characters.
func RuneSuffix(s string, n int) string {
	r := []rune(s)
	if n >= len(r) {
		return ""
	}
	return string(r[max(0, n):])
}

// CenteredColumn returns the 1-based column that centers text of width w on
// a screen cols wide, at least column 1.
func CenteredColumn(cols, w int) int {
	return max(1, (cols-w)/2)
}

// WrapText breaks text into lines of at most width characters at spaces; a
// word longer than width is split.
func WrapText(text string, width int) []string {
	var lines []string
	current := ""
	for _, word := range strings.Fields(text) {
		for RuneCount(word) > width {
			if current != "" {
				lines = append(lines, current)
				current = ""
			}
			lines = append(lines, RunePrefix(word, width))
			word = RuneSuffix(word, width)
		}
		switch {
		case current == "":
			current = word
		case RuneCount(current)+1+RuneCount(word) <= width:
			current += " " + word
		default:
			lines = append(lines, current)
			current = word
		}
	}
	if current != "" {
		lines = append(lines, current)
	}
	return lines
}
