// Package sessionsview draws Claude Code sessions: the machine-wide sessions
// overview (every session on one framed, scrolling table, with collapsed and
// expanded rows), the registry-record row blocks and the scrolling column of
// them the deletion checklist draws with, and the vertical viewport both
// scroll by.
//
// The layout functions are pure: rows, dimensions, a clock, and a home
// directory in, styled lines out. Only RenderFrame, Draw, and RunOverview
// write to a terminal, and they leave entering and restoring cbreak mode to
// whoever opened it.
package sessionsview

import "fmt"

// RowSlice is the visible part of one row block. ScreenTop is
// window-relative (0 is the window's first line); SkipTop counts the row's
// own leading lines that fall above the window, and Lines how many of its
// lines are visible.
type RowSlice struct {
	Index     int
	ScreenTop int
	SkipTop   int
	Lines     int
	Height    int
}

// Clipped reports whether some of the row's lines are outside the window.
func (s RowSlice) Clipped() bool {
	return s.Lines < s.Height
}

// Viewport is where the window sits over the content, and what it shows.
// Start is the first visible content line, Height the window as the caller
// declared it, and Total the summed height of every row.
type Viewport struct {
	Start       int
	Height      int
	Total       int
	Rows        []RowSlice
	HiddenAbove int
	HiddenBelow int
}

// Scrolling reports whether the content is taller than the window.
func (v Viewport) Scrolling() bool {
	return v.Total > v.Height
}

// RowTops returns the content line each row starts at, one entry per row.
func RowTops(rowHeights []int) []int {
	tops := make([]int, len(rowHeights))
	offset := 0
	for i, h := range rowHeights {
		tops[i] = offset
		offset += h
	}
	return tops
}

func sum(values []int) int {
	total := 0
	for _, v := range values {
		total += v
	}
	return total
}

// startLine is the first visible content line: the window centered on the
// focused row and clamped to the content, scrolling being a function of the
// focus alone.
func startLine(rowHeights, tops []int, focus, window int) int {
	total := sum(rowHeights)
	if window <= 0 || total <= window {
		return 0
	}
	if focus < 0 || focus >= len(rowHeights) {
		return 0
	}
	top := tops[focus]
	height := rowHeights[focus]
	start := top + floorDiv(height-window, 2)
	start = max(0, min(start, total-window))
	if height > window {
		// Centering a block taller than the window would cut off its first
		// line, which is the one carrying its name.
		start = min(start, top)
	}
	return start
}

// floorDiv divides rounding toward negative infinity, as Python's // does.
func floorDiv(a, b int) int {
	q := a / b
	if (a%b != 0) && ((a < 0) != (b < 0)) {
		q--
	}
	return q
}

// ComputeViewport places a window of window lines over rows of the given
// heights. The window is centered on the row at focus and clamped to the
// content; a focused row taller than the window has its top pinned instead.
// A focus naming no row, and a window with no lines, both leave the window
// at the top. A negative window or row height is an error.
//
// Every row with any visible line gets a RowSlice, including the partially
// visible rows at each edge; whether to draw a partial row clipped or leave
// it out is the caller's choice.
func ComputeViewport(rowHeights []int, focus, window int) (Viewport, error) {
	if window < 0 {
		return Viewport{}, fmt.Errorf("window height must not be negative: %d", window)
	}
	for i, h := range rowHeights {
		if h < 0 {
			return Viewport{}, fmt.Errorf("row %d has a negative height: %d", i, h)
		}
	}
	tops := RowTops(rowHeights)
	total := sum(rowHeights)
	start := startLine(rowHeights, tops, focus, window)
	end := start + window

	v := Viewport{Start: start, Height: window, Total: total}
	for i, h := range rowHeights {
		top := tops[i]
		bottom := top + h
		if bottom <= start {
			v.HiddenAbove++
			continue
		}
		if top >= end {
			v.HiddenBelow++
			continue
		}
		visibleTop := max(top, start)
		visibleBottom := min(bottom, end)
		v.Rows = append(v.Rows, RowSlice{
			Index:     i,
			ScreenTop: visibleTop - start,
			SkipTop:   visibleTop - top,
			Lines:     visibleBottom - visibleTop,
			Height:    h,
		})
	}
	return v, nil
}
