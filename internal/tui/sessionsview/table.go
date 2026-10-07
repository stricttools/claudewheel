package sessionsview

import (
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// CategoryLabels is what a registry session category reads as in the table's
// category column. A category not listed reads as itself.
func CategoryLabels() map[string]string {
	return map[string]string{
		"interactive":   "interactive",
		"bg":            "background",
		"daemon":        "daemon",
		"daemon-worker": "worker",
	}
}

// CategoryUnknown is the category cell of a session only the lifecycle store
// knows: no registry record ever said what it was.
const CategoryUnknown = "-"

// Missing is what an absent value reads as in every column.
const Missing = "-"

// The box-drawing pieces. The vertical scrollbar replaces the right border's
// │ with ┃, the horizontal one the bottom border's ─ and ┴ with ━.
const (
	TopLeft          = "╭"
	TopRight         = "╮"
	BottomLeft       = "╰"
	BottomRight      = "╯"
	RuleLeft         = "├"
	RuleRight        = "┤"
	Horizontal       = "─"
	Vertical         = "│"
	JointTop         = "┬"
	JointRule        = "┼"
	JointBottom      = "┴"
	VerticalHandle   = "┃"
	HorizontalHandle = "━"
)

// Ellipsis marks where a truncated cell was cut, at its end or its start.
const Ellipsis = "…"

// TableStyle names how a span is drawn; StyleSequence is the only place one
// becomes an escape sequence. A State cell's style is built by StateStyle.
type TableStyle string

// The fixed table styles.
const (
	StyleFrame    TableStyle = "frame"
	StyleHeader   TableStyle = "header"
	StyleRow      TableStyle = "row"
	StyleRowFocus TableStyle = "row_focus"
	StyleDetail   TableStyle = "detail"
	StyleEmpty    TableStyle = "empty"
)

// The prefixes of a State cell's style, followed by the state name.
const (
	stateStylePrefix      = "state"
	stateFocusStylePrefix = "state_focus"
)

// EmptyText is what an empty table or session list says.
const EmptyText = "No sessions."

// ChromeLines are the frame lines that are not the data window: the two
// borders, the header, and the header rule.
const ChromeLines = 4

// ExpandedHeight is how many lines an expanded row occupies: its own, plus
// three of detail.
const ExpandedHeight = 4

const (
	detailSep    = " · "
	detailIndent = "  "
)

// StateStyle is the style of a State cell in state.
func StateStyle(state string, focused bool) TableStyle {
	if focused {
		return TableStyle(stateFocusStylePrefix + ":" + state)
	}
	return TableStyle(stateStylePrefix + ":" + state)
}

// SessionRow is one session as the table needs it, already joined and
// resolved by the gatherer, so the layout asks nothing about the world.
// Empty text fields are absent values. Record and Lifecycle are carried for
// the overview's own keys (pruning needs the record, marking the session
// id), not for the layout.
type SessionRow struct {
	Session    string
	Name       string
	NameSource string
	State      string
	// Category is the category column's text, already a label.
	Category   string
	Cwd        string
	Profile    string
	Version    string
	Model      string
	StartedMS  *int64
	RSSKiB     *int64
	PID        *int64
	Current    bool
	ConfigDir  string
	Transcript string
	Record     *sessions.SessionRecord
	Lifecycle  *lifecycle.SessionLifecycle
	// Undelivered counts probe reports queued for the session and not yet
	// confirmed delivered, kept for an ended session until it resumes.
	Undelivered int
}

// Span is a run of characters drawn in one style.
type Span struct {
	Text  string
	Style TableStyle
}

// Line is one drawn line: its spans, left to right.
type Line []Span

// Frame is a laid-out table and the dimensions the key loop needs back.
// HScroll is the clamped horizontal offset, so a screen that asked for more
// than the strip offers reads the real one back. Window is the data window
// in lines (what a page key moves by), and TotalLines the summed height of
// every visible row.
type Frame struct {
	Lines         []Line
	Window        int
	TotalLines    int
	StripWidth    int
	InteriorWidth int
	HScroll       int
}

// columnSpec is one column: its label, how wide it gets, and how it handles
// overflow. A fixed width (above zero) is one the content cannot change;
// otherwise the column is as wide as its longest cell (never narrower than
// its label), raised to minimum and capped at maximum, and a maximum of zero
// is no cap at all.
type columnSpec struct {
	label        string
	fixed        int
	minimum      int
	maximum      int
	alignRight   bool
	truncateLeft bool
}

func longestState() int {
	longest := 0
	for _, s := range lifecycle.States() {
		longest = max(longest, widgets.RuneCount(s))
	}
	return longest
}

// columnSpecs are the columns, in order. State is as wide as the longest
// state name, so a state added to the lifecycle model cannot outgrow it.
// Started is never capped: cutting an age would destroy it, not abbreviate
// it.
func columnSpecs() []columnSpec {
	return []columnSpec{
		{label: "Name", minimum: 8, maximum: 40},
		{label: "State", fixed: longestState()},
		{label: "Kind", fixed: 11},
		{label: "Directory", minimum: 10, maximum: 40, truncateLeft: true},
		{label: "Version", fixed: 7},
		{label: "Model", maximum: 24},
		{label: "Started"},
		{label: "MiB", fixed: 5, alignRight: true},
		{label: "Undelivered", alignRight: true},
	}
}

// stateColumn is the column carrying the state, and therefore its own color.
const stateColumn = 1

// stateGroups are the order the state groups are listed in: running
// sessions, then what wants attention, then what is over.
func stateGroups() []map[string]bool {
	return []map[string]bool{
		lifecycle.LiveStates(),
		{lifecycle.StateStarting: true},
		{lifecycle.StateOnHold: true, lifecycle.StateBlocked: true},
		{lifecycle.StateCrashed: true},
		{lifecycle.StateDone: true},
		{lifecycle.StateExited: true},
	}
}

func groupOf(groups []map[string]bool, state string) int {
	for i, g := range groups {
		if g[state] {
			return i
		}
	}
	return len(groups)
}

// SortRows groups rows by state, newest first inside each group. A row with
// no start time sorts after every dated row of its group, and rows equal by
// age sort by name, so a redraw never reshuffles them. rows is not changed.
func SortRows(rows []SessionRow) []SessionRow {
	groups := stateGroups()
	out := append([]SessionRow(nil), rows...)
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if ga, gb := groupOf(groups, a.State), groupOf(groups, b.State); ga != gb {
			return ga < gb
		}
		if (a.StartedMS == nil) != (b.StartedMS == nil) {
			return a.StartedMS != nil
		}
		if a.StartedMS != nil && *a.StartedMS != *b.StartedMS {
			return *a.StartedMS > *b.StartedMS
		}
		return a.Name < b.Name
	})
	return out
}

// VisibleRows returns the rows shown at this filter setting, in the order
// given. The default view hides what is finished, which accumulates without
// end and is not what the screen is opened to look at.
func VisibleRows(rows []SessionRow, showAll bool) []SessionRow {
	if showAll {
		return append([]SessionRow(nil), rows...)
	}
	hidden := lifecycle.HiddenByDefaultStates()
	var out []SessionRow
	for _, row := range rows {
		if !hidden[row.State] {
			out = append(out, row)
		}
	}
	return out
}

// Tildify writes home as ~ in path, or returns Missing for an empty path.
// home is a parameter: the layout does not read the environment.
func Tildify(path, home string) string {
	if path == "" {
		return Missing
	}
	if home != "" && (path == home || strings.HasPrefix(path, home+"/")) {
		return "~" + path[len(home):]
	}
	return path
}

func truncate(text string, width int, fromLeft bool) string {
	if width <= 0 {
		return ""
	}
	r := []rune(text)
	if len(r) <= width {
		return text
	}
	if width == 1 {
		return Ellipsis
	}
	if fromLeft {
		return Ellipsis + string(r[len(r)-(width-1):])
	}
	return string(r[:width-1]) + Ellipsis
}

func mib(rssKiB *int64) string {
	if rssKiB == nil {
		return Missing
	}
	return fmt.Sprintf("%d", int64(math.RoundToEven(float64(*rssKiB)/1024)))
}

func startedCell(startedMS *int64, nowMS int64) string {
	if startedMS == nil {
		return "unknown"
	}
	return FormatUptime(startedMS, nowMS) + " ago"
}

func orMissing(s string) string {
	if s == "" {
		return Missing
	}
	return s
}

// Cells returns the untruncated cell texts of row, in column order.
func Cells(row SessionRow, nowMS int64, home string) []string {
	name := row.Name
	if row.Current {
		name = CurrentMark + " " + row.Name
	}
	undelivered := Missing
	if row.Undelivered != 0 {
		undelivered = fmt.Sprintf("%d", row.Undelivered)
	}
	return []string{
		name,
		row.State,
		row.Category,
		Tildify(row.Cwd, home),
		orMissing(row.Version),
		orMissing(row.Model),
		startedCell(row.StartedMS, nowMS),
		mib(row.RSSKiB),
		undelivered,
	}
}

// Labels returns the header labels, in column order.
func Labels() []string {
	specs := columnSpecs()
	out := make([]string, len(specs))
	for i, s := range specs {
		out[i] = s.label
	}
	return out
}

func columnWidths(specs []columnSpec, table [][]string) []int {
	widths := make([]int, len(specs))
	for i, spec := range specs {
		if spec.fixed > 0 {
			widths[i] = spec.fixed
			continue
		}
		natural := widgets.RuneCount(spec.label)
		for _, row := range table {
			natural = max(natural, widgets.RuneCount(row[i]))
		}
		width := max(spec.minimum, natural)
		if spec.maximum > 0 {
			width = min(spec.maximum, width)
		}
		widths[i] = width
	}
	return widths
}

// pad fits text to width: truncated if long, aligned if short.
func pad(text string, width int, spec columnSpec) string {
	cut := truncate(text, width, spec.truncateLeft)
	fill := strings.Repeat(" ", max(0, width-widgets.RuneCount(cut)))
	if spec.alignRight {
		return fill + cut
	}
	return cut + fill
}

// stripCells returns each cell as drawn, one space of padding either side.
func stripCells(specs []columnSpec, texts []string, widths []int) []string {
	out := make([]string, len(specs))
	for i, spec := range specs {
		out[i] = " " + pad(texts[i], widths[i], spec) + " "
	}
	return out
}

func stripWidth(widths []int) int {
	total := len(widths) - 1
	for _, w := range widths {
		total += w + 2
	}
	return total
}

// jointLine is a horizontal rule with joint wherever a column separator falls.
func jointLine(widths []int, joint string) string {
	parts := make([]string, len(widths))
	for i, w := range widths {
		parts[i] = strings.Repeat(Horizontal, w+2)
	}
	return strings.Join(parts, joint)
}

// clip returns the width characters of pieces beginning at start, padded
// when short: the horizontal viewport, applied over spans so a row's state
// cell keeps its own style through the shift and the clip.
func clip(pieces []Span, start, width int, padText string, padStyle TableStyle) Line {
	if width <= 0 {
		return nil
	}
	var out Line
	cursor := 0
	drawn := 0
	for _, piece := range pieces {
		r := []rune(piece.Text)
		begin, end := cursor, cursor+len(r)
		cursor = end
		low, high := max(begin, start), min(end, start+width)
		if high > low {
			out = append(out, Span{string(r[low-begin : high-begin]), piece.Style})
			drawn += high - low
		}
	}
	if drawn < width {
		out = append(out, Span{strings.Repeat(padText, width-drawn), padStyle})
	}
	return out
}

// cutLine drops everything past width, for a terminal too narrow.
func cutLine(line Line, width int) Line {
	var out Line
	room := width
	for _, span := range line {
		if room <= 0 {
			break
		}
		text := widgets.RunePrefix(span.Text, room)
		out = append(out, Span{text, span.Style})
		room -= widgets.RuneCount(text)
	}
	return out
}

// handle places a scrollbar handle: where it starts and how long it is, in
// track units. It is the visible fraction of the content, never shorter
// than one unit and never past the end of the track.
func handle(visible, total, offset int) (start, length int) {
	length = max(1, min(visible, (visible*visible+total-1)/total))
	start = min(offset*visible/total, visible-length)
	return max(0, start), length
}

func detailLines(row SessionRow, home string) []string {
	pid := Missing
	if row.PID != nil {
		pid = fmt.Sprintf("%d", *row.PID)
	}
	session := row.Session
	if session == "" {
		session = "unknown"
	}
	identity := strings.Join([]string{
		"pid " + pid,
		"profile " + orMissing(row.Profile),
		"name source " + orMissing(row.NameSource),
		"config " + Tildify(row.ConfigDir, home),
	}, detailSep)
	return []string{
		detailIndent + "session " + session,
		detailIndent + identity,
		detailIndent + "transcript " + Tildify(row.Transcript, home),
	}
}

// rowLine is one row as three strip pieces: before the State cell, it, and
// after. The column separators carry the row's style rather than the
// frame's, so a focused row's highlight is one unbroken band.
func rowLine(specs []columnSpec, texts []string, widths []int, focused bool) []Span {
	drawn := stripCells(specs, texts, widths)
	style := StyleRow
	if focused {
		style = StyleRowFocus
	}
	before := strings.Join(drawn[:stateColumn], Vertical)
	after := strings.Join(drawn[stateColumn+1:], Vertical)
	return []Span{
		{before + Vertical, style},
		{drawn[stateColumn], StateStyle(texts[stateColumn], focused)},
		{Vertical + after, style},
	}
}

// clippedText is text shifted left by start and fitted to width with padText.
func clippedText(text string, start, width int, padText string) string {
	if width <= 0 {
		return ""
	}
	r := []rune(text)
	lo := min(start, len(r))
	hi := min(start+width, len(r))
	cut := string(r[lo:hi])
	return cut + strings.Repeat(padText, width-(hi-lo))
}

// TableSpec is what Layout lays out. Focus and Expanded index the visible
// rows (VisibleRows at ShowAll), the same list the overview's keys act on;
// Expanded is -1 when no row is expanded.
type TableSpec struct {
	Rows     []SessionRow
	Focus    int
	Expanded int
	Height   int
	Width    int
	HScroll  int
	NowMS    int64
	Home     string
	ShowAll  bool
}

// Layout lays the rows out into a frame of Height by Width characters, the
// bottom terminal row left to the overview's hint line. The vertical window
// is placed by ComputeViewport, so a row at an edge is drawn clipped rather
// than dropped; the horizontal offset is clamped and reported back. Every
// line is Width characters long, and a terminal too short even for the
// chrome yields the prefix that fits.
//
// Every cell line (the header, the joint lines, each row) is one strip as
// wide as the columns ask for, shifted left by HScroll and clipped to the
// interior. An expanded row's detail lines are not part of the strip: they
// are full-width prose about one session, and scrolling them with the
// columns would hide the beginning of a path.
func Layout(spec TableSpec) (Frame, error) {
	specs := columnSpecs()
	shown := VisibleRows(spec.Rows, spec.ShowAll)
	interior := max(0, spec.Width-2)
	window := max(0, spec.Height-1-ChromeLines)

	table := make([][]string, len(shown))
	for i, row := range shown {
		table[i] = Cells(row, spec.NowMS, spec.Home)
	}
	widths := columnWidths(specs, table)
	strip := stripWidth(widths)
	hscroll := max(0, min(spec.HScroll, max(0, strip-interior)))

	heights := make([]int, len(shown))
	for i := range shown {
		heights[i] = 1
		if i == spec.Expanded {
			heights[i] = ExpandedHeight
		}
	}
	total := sum(heights)
	viewport, err := ComputeViewport(heights, spec.Focus, window)
	if err != nil {
		return Frame{}, err
	}

	var body []Line
	for _, s := range viewport.Rows {
		row := shown[s.Index]
		focused := s.Index == spec.Focus
		pieces := [][]Span{rowLine(specs, table[s.Index], widths, focused)}
		if s.Height > 1 {
			for _, text := range detailLines(row, spec.Home) {
				pieces = append(pieces, []Span{{text, StyleDetail}})
			}
		}
		for offset, piece := range pieces[s.SkipTop : s.SkipTop+s.Lines] {
			detail := s.SkipTop+offset > 0
			start := hscroll
			padStyle := StyleRow
			switch {
			case detail:
				start = 0
				padStyle = StyleDetail
			case focused:
				padStyle = StyleRowFocus
			}
			body = append(body, clip(piece, start, interior, " ", padStyle))
		}
	}
	if len(shown) == 0 && window > 0 {
		body = append(body, clip([]Span{{EmptyText, StyleEmpty}}, 0, interior, " ", StyleEmpty))
	}
	for len(body) < window {
		body = append(body, clip(nil, 0, interior, " ", StyleRow))
	}

	// The vertical handle, over the right border of the data-window lines.
	borders := make([]string, len(body))
	for i := range borders {
		borders[i] = Vertical
	}
	if total > window && window > 0 {
		start, length := handle(window, total, viewport.Start)
		for i := start; i < start+length && i < len(borders); i++ {
			borders[i] = VerticalHandle
		}
	}

	// The bottom border's interior: the joint line, with the horizontal
	// handle over it wherever the strip is wider than the frame.
	bottom := []rune(clippedText(jointLine(widths, JointBottom), hscroll, interior, Horizontal))
	if strip > interior && interior > 0 {
		start, length := handle(interior, strip, hscroll)
		for i := start; i < start+length; i++ {
			bottom[i] = []rune(HorizontalHandle)[0]
		}
	}

	header := Line{{Vertical, StyleFrame}}
	header = append(header, clip([]Span{{strings.Join(stripCells(specs, Labels(), widths), Vertical), StyleHeader}}, hscroll, interior, " ", StyleHeader)...)
	header = append(header, Span{Vertical, StyleFrame})
	lines := []Line{
		{
			{TopLeft, StyleFrame},
			{clippedText(jointLine(widths, JointTop), hscroll, interior, Horizontal), StyleFrame},
			{TopRight, StyleFrame},
		},
		header,
		{
			{RuleLeft, StyleFrame},
			{clippedText(jointLine(widths, JointRule), hscroll, interior, Horizontal), StyleFrame},
			{RuleRight, StyleFrame},
		},
	}
	for i, line := range body {
		drawn := Line{{Vertical, StyleFrame}}
		drawn = append(drawn, line...)
		drawn = append(drawn, Span{borders[i], StyleFrame})
		lines = append(lines, drawn)
	}
	lines = append(lines, Line{
		{BottomLeft, StyleFrame},
		{string(bottom), StyleFrame},
		{BottomRight, StyleFrame},
	})

	keep := min(len(lines), max(0, spec.Height-1))
	out := make([]Line, keep)
	for i := range keep {
		out[i] = cutLine(lines[i], spec.Width)
	}
	return Frame{
		Lines:         out,
		Window:        window,
		TotalLines:    total,
		StripWidth:    strip,
		InteriorWidth: interior,
		HScroll:       hscroll,
	}, nil
}
