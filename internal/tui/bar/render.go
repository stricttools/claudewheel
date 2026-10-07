package bar

import (
	"fmt"
	"slices"
	"strconv"
	"strings"

	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// ArrowMargin is how many columns each side of the bar keeps for a scroll
// arrow ("<99 " or " 99>") while the bar scrolls.
const ArrowMargin = 4

// hintSeparator joins the hints on the status line.
const hintSeparator = "   "

// ellipsis ends a value cut to its segment's width.
const ellipsis = "…"

// provenanceGlyphs marks each option's source while the sources overlay is
// on.
func provenanceGlyphs() map[Source]string {
	return map[Source]string{
		SourceDiscovered: "*",
		SourcePinned:     "^",
		SourceDefaults:   ".",
		SourceEphemeral:  "~",
	}
}

// provenanceLegend replaces the hints while the sources overlay is on.
const provenanceLegend = "* discovered  ^ pinned  . default  ~ ephemeral   ?: hide"

// MinimapMode is when the minimap (one block per segment, top right) shows.
type MinimapMode string

const (
	// MinimapAuto shows the minimap only while the bar scrolls.
	MinimapAuto MinimapMode = "auto"
	// MinimapAlways always shows it.
	MinimapAlways MinimapMode = "always"
)

// ParseMinimapMode reads config.json's minimap setting; any other value is
// an error naming the accepted ones.
func ParseMinimapMode(s string) (MinimapMode, error) {
	switch MinimapMode(s) {
	case MinimapAuto, MinimapAlways:
		return MinimapMode(s), nil
	}
	return "", fmt.Errorf("config.json minimap is %q; use %q or %q", s, MinimapAuto, MinimapAlways)
}

// Frame is what one draw shows besides the bar: a one-time message (Flash),
// a persistent one (Notice), the sources overlay, and the key hints. Flash
// wins over the overlay's legend, which wins over Notice, which wins over
// the hints.
type Frame struct {
	Flash          string
	Notice         string
	ShowProvenance bool
	Hints          []string
}

// layoutItem is where one segment sits on the bar, in bar columns (1-based,
// before scrolling).
type layoutItem struct {
	col          int
	label        string
	value        string
	hasCursor    bool
	focused      bool
	labelWidth   int
	valueWidth   int
	segmentIndex int
}

// width is the item's width on the bar, cursor included.
func (li layoutItem) width() int {
	w := li.labelWidth + li.valueWidth
	if li.hasCursor {
		w++
	}
	return w
}

// Renderer draws the bar. Widths and cuts count characters (code points).
type Renderer struct {
	Colors  widgets.Colors
	Minimap MinimapMode

	// Set by each draw.
	rows, cols    int
	layout        []layoutItem
	totalWidth    int
	viewportStart int
	scrolling     bool
	// valueCols holds the screen column each drawn segment's value starts
	// at; the focused segment's options fan out from it.
	valueCols map[string]int
	frame     Frame
}

// Render draws the whole screen for b and writes it to t.
func (r *Renderer) Render(t *terminal.Terminal, b *Bar, f Frame) error {
	screen, err := r.Draw(b, f, t.Rows, t.Cols)
	if err != nil {
		return err
	}
	return t.Write(screen)
}

// Draw returns the escape sequences drawing b on a rows by cols screen: the
// bar on the middle row, the focused segment's options above and below
// it, the scroll arrows and minimap when the bar is wider than the screen,
// and the status line.
func (r *Renderer) Draw(b *Bar, f Frame, rows, cols int) (string, error) {
	r.rows, r.cols, r.frame = rows, cols, f
	r.valueCols = map[string]int{}
	for _, seg := range b.Segments {
		if _, ok := r.Colors.Segment(seg.Key); !ok {
			return "", fmt.Errorf("the theme has no colors for the %s segment", seg.Key)
		}
	}
	var buf strings.Builder
	buf.WriteString(terminal.ClearScreen)
	center := rows / 2
	r.drawBar(&buf, b, center)
	r.drawArrows(&buf, center)
	r.drawFanOut(&buf, b, center, r.hintLineCount(f.Hints))
	r.drawStatus(&buf, f)
	r.drawMinimap(&buf, b)
	return buf.String(), nil
}

// segColors returns segment key's colors; Draw checked every segment has
// them.
func (r *Renderer) segColors(key string) widgets.SegmentColors {
	sc, _ := r.Colors.Segment(key)
	return sc
}

// hintLineCount is how many bottom rows the hints take: 1, or 2 when they
// do not fit on one line. It is computed from the hints even when a flash,
// the legend, or a notice replaces them, so the fan-out does not jump.
func (r *Renderer) hintLineCount(hints []string) int {
	if len(hints) == 0 {
		return 1
	}
	if widgets.RuneCount(strings.Join(hints, hintSeparator)) <= r.cols-4 {
		return 1
	}
	return 2
}

// fitValue cuts value to maxW characters with an ellipsis, or pads it with
// spaces to minW.
func fitValue(value string, minW, maxW int) string {
	n := widgets.RuneCount(value)
	if n > maxW {
		return widgets.RunePrefix(value, maxW-1) + ellipsis
	}
	if n < minW {
		return value + strings.Repeat(" ", minW-n)
	}
	return value
}

// computeLayout places every segment on the bar.
func (r *Renderer) computeLayout(b *Bar) {
	col := 2
	sepWidth := widgets.RuneCount(r.Colors.SeparatorChar)
	r.layout = r.layout[:0]
	for i, seg := range b.Segments {
		focused := i == b.Focus
		label := seg.Label
		if seg.HasPending {
			label += "*"
		}
		label += ": "
		var raw string
		sel, hasSel := seg.Selected()
		switch {
		case focused && seg.Creating:
			raw = seg.CreateBuffer
		case focused && seg.Searchable && seg.SearchBuffer != "":
			raw = seg.SearchBuffer
		case hasSel:
			raw = sel
		default:
			raw = r.Colors.EmptyValueText
		}
		value := fitValue(raw, seg.MinWidth, seg.MaxWidth)
		item := layoutItem{
			col:          col,
			label:        label,
			value:        value,
			hasCursor:    focused && (seg.Creating || (seg.Searchable && seg.SearchBuffer != "")),
			focused:      focused,
			labelWidth:   widgets.RuneCount(label),
			valueWidth:   widgets.RuneCount(value),
			segmentIndex: i,
		}
		r.layout = append(r.layout, item)
		col += item.width()
		if i < len(b.Segments)-1 {
			col += sepWidth
		}
	}
	r.totalWidth = col
}

// computeViewport returns the first bar column the screen shows: 0 when the
// bar fits, else a start centering the focused segment in the columns
// between the arrow margins.
func (r *Renderer) computeViewport() int {
	if r.totalWidth <= r.cols {
		return 0
	}
	usable := r.cols - 2*ArrowMargin
	if usable <= 0 {
		return 0
	}
	for _, li := range r.layout {
		if !li.focused {
			continue
		}
		center := li.col + (li.labelWidth+li.valueWidth)/2
		start := center - usable/2
		return max(0, min(start, r.totalWidth-usable))
	}
	return 0
}

// drawBar draws the segments on the center row, scrolled and clipped when
// the bar is wider than the screen, and records where each value is.
func (r *Renderer) drawBar(buf *strings.Builder, b *Bar, center int) {
	c := r.Colors
	sep := c.SeparatorChar
	sepWidth := widgets.RuneCount(sep)
	r.computeLayout(b)
	r.viewportStart = r.computeViewport()
	r.scrolling = r.totalWidth > r.cols
	rightMargin := r.cols - ArrowMargin

	for _, li := range r.layout {
		seg := b.Segments[li.segmentIndex]
		label, value, hasCursor := li.label, li.value, li.hasCursor
		renderCol := li.col
		if r.scrolling {
			screenCol := li.col - r.viewportStart + ArrowMargin
			if screenCol+li.width() <= ArrowMargin || screenCol >= rightMargin {
				continue
			}
			renderCol = screenCol
			// Cut what runs past the right margin.
			maxChars := rightMargin - renderCol
			if maxChars < li.width() {
				if maxChars <= 0 {
					continue
				}
				if maxChars < widgets.RuneCount(label) {
					label = widgets.RunePrefix(label, maxChars)
					value = ""
				} else {
					value = widgets.RunePrefix(value, maxChars-widgets.RuneCount(label))
				}
				hasCursor = false
			}
			// Skip what falls left of the left margin.
			if renderCol < ArrowMargin {
				skip := ArrowMargin - renderCol
				if skip >= widgets.RuneCount(label)+widgets.RuneCount(value) {
					continue
				}
				if skip >= widgets.RuneCount(label) {
					value = widgets.RuneSuffix(value, skip-widgets.RuneCount(label))
					label = ""
				} else {
					label = widgets.RuneSuffix(label, skip)
				}
				renderCol = ArrowMargin
				hasCursor = false
			}
		}

		r.valueCols[seg.Key] = renderCol + widgets.RuneCount(label)
		buf.WriteString(terminal.MoveTo(center, renderCol))
		sc := r.segColors(seg.Key)
		if li.focused {
			buf.WriteString(sc.FocusBg + sc.FocusFg + label)
			switch {
			case seg.Creating:
				buf.WriteString(value)
				buf.WriteString(sc.FocusBg + c.SearchCursorFg + "_" + terminal.Reset)
			case seg.Searchable && seg.SearchBuffer != "":
				// A search matching nothing is drawn in the no-match color.
				if len(seg.FilteredOptions()) == 0 {
					noMatch := c.SearchNoMatchFg
					if noMatch == "" {
						noMatch = sc.FocusFg
					}
					buf.WriteString(sc.FocusBg + noMatch)
				}
				buf.WriteString(value)
				buf.WriteString(sc.FocusBg + c.SearchCursorFg + "_" + terminal.Reset)
			default:
				buf.WriteString(value + terminal.Reset)
			}
		} else {
			buf.WriteString(c.LabelFg + label + terminal.Reset)
			buf.WriteString(r.valueColor(seg) + value + terminal.Reset)
		}

		if li.segmentIndex == len(b.Segments)-1 {
			continue
		}
		sepCol := renderCol + widgets.RuneCount(label) + widgets.RuneCount(value)
		if hasCursor {
			sepCol++
		}
		if r.scrolling && (sepCol+sepWidth <= ArrowMargin || sepCol >= rightMargin) {
			continue
		}
		buf.WriteString(terminal.MoveTo(center, sepCol))
		buf.WriteString(c.SeparatorFg + sep + terminal.Reset)
	}
}

// unavailableFg is a segment's color for options that cannot be launched
// as they are; the theme's unavailable color, or dim when it has none.
func unavailableFg(sc widgets.SegmentColors) string {
	if sc.UnavailableFg != "" {
		return sc.UnavailableFg
	}
	return terminal.Dim
}

// valueColor is the color an unfocused segment's value is drawn in.
func (r *Renderer) valueColor(seg *Segment) string {
	sc := r.segColors(seg.Key)
	sel, hasSel := seg.Selected()
	if hasSel && sel == PlusEntry {
		return terminal.Dim
	}
	v, ok := seg.Value()
	if !ok {
		return r.Colors.EmptyValueFg
	}
	if r.dimmed(seg, v) {
		return unavailableFg(sc)
	}
	return sc.ValueFg
}

// dimmed reports whether option v is drawn in the unavailable color: not
// installed, unmet requirements, rejected by the chosen client, or an
// unauthenticated profile.
func (r *Renderer) dimmed(seg *Segment, v string) bool {
	if seg.State.NotInstalled(v) || seg.Unavailable[v] {
		return true
	}
	if _, rejected := seg.Rejected[v]; rejected {
		return true
	}
	return seg.State.Unauthenticated(v)
}

// countOffscreen counts the segments wholly left and right of the screen.
func (r *Renderer) countOffscreen() (left, right int) {
	rightMargin := r.cols - ArrowMargin
	for _, li := range r.layout {
		screenCol := li.col - r.viewportStart + ArrowMargin
		if screenCol+li.labelWidth+li.valueWidth <= ArrowMargin {
			left++
		} else if screenCol >= rightMargin {
			right++
		}
	}
	return left, right
}

// drawArrows draws the scroll arrows with the count of segments off each
// side, while the bar scrolls and the screen has room for them.
func (r *Renderer) drawArrows(buf *strings.Builder, center int) {
	if !r.scrolling || r.cols < 2*ArrowMargin+1 {
		return
	}
	left, right := r.countOffscreen()
	if left > 0 {
		buf.WriteString(terminal.MoveTo(center, 1))
		buf.WriteString(r.Colors.OverflowArrowFg + "<" + strconv.Itoa(left) + terminal.Reset)
	}
	if right > 0 {
		text := strconv.Itoa(right) + ">"
		buf.WriteString(terminal.MoveTo(center, r.cols-len(text)))
		buf.WriteString(r.Colors.OverflowArrowFg + text + terminal.Reset)
	}
}

// drawFanOut draws the focused segment's other options in its value
// column: those before the selection above it (nearest first, then the
// "nothing selected" entry on a wrapping segment), those after it below.
// While a search is typed, every match goes below. Nothing below reaches
// the reserved hint rows.
func (r *Renderer) drawFanOut(buf *strings.Builder, b *Bar, center, reservedBottom int) {
	seg := b.Focused()
	if !seg.ShowOptions || len(seg.DisplayOptions()) <= 1 {
		return
	}
	col, ok := r.valueCols[seg.Key]
	if !ok {
		return
	}
	var above, below []string
	if seg.SearchBuffer != "" {
		below = seg.FilteredOptions()
		if len(below) == 0 {
			return
		}
	} else {
		opts := seg.DisplayOptions()
		idx := seg.SelectedIndex()
		if idx < 0 {
			below = opts
		} else {
			above = slices.Clone(opts[:idx])
			slices.Reverse(above)
			below = opts[idx+1:]
			if seg.Wrap {
				above = append(above, r.Colors.EmptyValueText)
			}
		}
	}
	draw := func(row int, opt string) {
		display := fitValue(opt, seg.MinWidth, seg.MaxWidth)
		if r.scrolling {
			if col < 1 {
				return
			}
			if col+widgets.RuneCount(display) > r.cols {
				avail := r.cols - col
				if avail <= 0 {
					return
				}
				display = widgets.RunePrefix(display, avail)
			}
		}
		buf.WriteString(terminal.MoveTo(row, col))
		r.drawOption(buf, seg, opt, display)
	}
	for i, opt := range above {
		row := center - (i + 1)
		if row < 1 {
			break
		}
		draw(row, opt)
	}
	for i, opt := range below {
		row := center + i + 1
		if row > r.rows-reservedBottom {
			break
		}
		draw(row, opt)
	}
}

// drawOption draws one fanned-out option. The "nothing selected" entry and
// PlusEntry keep their own colors; an option that cannot be launched as it
// is takes the unavailable color; any other option has the characters a
// search matched highlighted. With the sources overlay on, a glyph naming
// the option's source comes first, in place of the option's last two
// columns.
func (r *Renderer) drawOption(buf *strings.Builder, seg *Segment, opt, display string) {
	c := r.Colors
	sc := r.segColors(seg.Key)
	prefix := ""
	if r.frame.ShowProvenance && opt != c.EmptyValueText && opt != PlusEntry {
		glyph := " "
		if src, ok := seg.State.SourceOf(opt); ok {
			glyph = provenanceGlyphs()[src]
		}
		prefix = glyph + " "
		display = widgets.RunePrefix(display, max(0, widgets.RuneCount(display)-2))
	}
	switch {
	case opt == c.EmptyValueText:
		buf.WriteString(c.EmptyValueFg + display + terminal.Reset)
		return
	case opt == PlusEntry:
		buf.WriteString(terminal.Dim + display + terminal.Reset)
		return
	}
	if prefix != "" {
		buf.WriteString(terminal.Dim + prefix + terminal.Reset)
	}
	if r.dimmed(seg, opt) {
		buf.WriteString(unavailableFg(sc) + display + terminal.Reset)
		return
	}
	if seg.SearchBuffer == "" {
		buf.WriteString(sc.OptionFg + display + terminal.Reset)
		return
	}
	r.drawHighlighted(buf, seg.SearchBuffer, opt, display, sc.OptionFg)
}

// drawHighlighted draws display with the characters the search matched in
// opt in the match color. Matches past display's end are dropped, and the
// ellipsis of a cut value is never highlighted.
func (r *Renderer) drawHighlighted(buf *strings.Builder, query, opt, display, baseFg string) {
	matchFg := r.Colors.SearchMatchFg
	if matchFg == "" {
		matchFg = baseFg
	}
	displayRunes := []rune(display)
	limit := len(displayRunes)
	if widgets.RuneCount(opt) > limit && strings.HasSuffix(display, ellipsis) {
		limit--
	}
	matched := map[int]bool{}
	for _, p := range widgets.FuzzyMatchPositions(query, opt) {
		if p < limit {
			matched[p] = true
		}
	}
	current := baseFg
	buf.WriteString(current)
	for i, ch := range displayRunes {
		target := baseFg
		if matched[i] {
			target = matchFg
		}
		if target != current {
			buf.WriteString(target)
			current = target
		}
		buf.WriteRune(ch)
	}
	buf.WriteString(terminal.Reset)
}

// drawMinimap draws one block per segment in the top-right corner: the
// focused one on the highlight background, those without a value muted,
// the rest in their value color.
func (r *Renderer) drawMinimap(buf *strings.Builder, b *Bar) {
	if r.Minimap != MinimapAlways && !r.scrolling {
		return
	}
	start := r.cols - len(b.Segments)
	if start < 1 {
		return
	}
	buf.WriteString(terminal.MoveTo(1, start))
	for i, seg := range b.Segments {
		valueFg := r.segColors(seg.Key).ValueFg
		_, hasValue := seg.Value()
		switch {
		case i == b.Focus:
			buf.WriteString(r.Colors.OverflowMinimapFocusedBg + valueFg)
		case !hasValue:
			buf.WriteString(r.Colors.OverflowMinimapFg)
		default:
			buf.WriteString(valueFg)
		}
		buf.WriteString(r.Colors.OverflowMinimapChar + terminal.Reset)
	}
}

// drawStatus draws the bottom line: the flash, else the sources legend,
// else the notice, else the hints (on two lines when one is too narrow).
func (r *Renderer) drawStatus(buf *strings.Builder, f Frame) {
	maxWidth := r.cols - 4
	line := func(row int, style, text string) {
		buf.WriteString(terminal.MoveTo(row, 2))
		buf.WriteString(style + widgets.RunePrefix(text, maxWidth) + terminal.Reset)
	}
	switch {
	case f.Flash != "":
		line(r.rows, terminal.Bold+r.Colors.EmptyValueFg, f.Flash)
		return
	case f.ShowProvenance:
		line(r.rows, terminal.Dim, provenanceLegend)
		return
	case f.Notice != "":
		line(r.rows, terminal.Bold+r.Colors.EmptyValueFg, f.Notice)
		return
	case len(f.Hints) == 0:
		return
	}
	joined := strings.Join(f.Hints, hintSeparator)
	if widgets.RuneCount(joined) <= maxWidth {
		line(r.rows, terminal.Dim, joined)
		return
	}
	first, second := splitHints(f.Hints, maxWidth)
	line(r.rows-1, terminal.Dim, first)
	line(r.rows, terminal.Dim, second)
}

// splitHints fills the first line greedily with whole hints (at least one)
// and puts the rest on the second.
func splitHints(hints []string, maxWidth int) (string, string) {
	used, split := 0, 0
	for i, h := range hints {
		need := widgets.RuneCount(h)
		if i > 0 {
			need += len(hintSeparator)
		}
		if used+need > maxWidth {
			break
		}
		used += need
		split = i + 1
	}
	if split == 0 {
		split = 1
	}
	return strings.Join(hints[:split], hintSeparator), strings.Join(hints[split:], hintSeparator)
}
