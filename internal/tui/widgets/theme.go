// Package widgets is the TUI's themed widget layer: a theme turned into
// terminal escape sequences, fuzzy matching for searches, form fields and the
// form runner, fullscreen pages, the selection list, and the confirm
// component that is the one authority for confirmation keys.
//
// Every widget draws on a terminal its caller opened and put in cbreak mode
// (terminal.EnterRaw), and leaves restoring it to the caller's deferred
// Close. Every key loop takes a context and returns the terminal's
// cancellation cause as soon as it is done, and redraws on KeyResize.
package widgets

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// SegmentColors holds one segment's escape sequences.
type SegmentColors struct {
	ValueFg       string
	FocusBg       string
	FocusFg       string
	OptionFg      string
	UnavailableFg string
}

// Colors is a theme with every color turned into its escape sequence. A
// color the theme leaves empty is the empty sequence.
type Colors struct {
	GlobalFg       string
	LabelFg        string
	SeparatorFg    string
	SeparatorChar  string
	EmptyValueFg   string
	EmptyValueText string

	segments map[string]SegmentColors

	SearchCursorFg  string
	SearchMatchFg   string
	SearchNoMatchFg string

	OverflowArrowFg          string
	OverflowMinimapFg        string
	OverflowMinimapFocusedBg string
	OverflowMinimapChar      string

	FormsTitleFg    string
	FormsFocusBg    string
	FormsFocusFg    string
	FormsFieldFg    string
	FormsErrorFg    string
	FormsHintFg     string
	FormsCursorFg   string
	FormsReadonlyFg string

	SessionsFrameFg   string
	SessionsHeaderFg  string
	SessionsRowFg     string
	SessionsFocusBg   string
	SessionsFocusFg   string
	SessionsDetailFg  string
	SessionsHintFg    string
	SessionsMessageFg string

	// One sequence per lifecycle state, keyed by the state name as the
	// lifecycle store writes it ("on-hold"); complete by construction.
	stateFg map[string]string
}

// Segment returns the colors of the segment key, and false when the theme
// has no entry for it.
func (c Colors) Segment(key string) (SegmentColors, bool) {
	s, ok := c.segments[key]
	return s, ok
}

// StateFg returns the foreground sequence of a session lifecycle state. A
// name that is not a lifecycle state is an error.
func (c Colors) StateFg(state string) (string, error) {
	fg, ok := c.stateFg[state]
	if !ok {
		return "", fmt.Errorf("%q is not a session lifecycle state", state)
	}
	return fg, nil
}

// ParseHex reads a "#rrggbb" color. Any other text is an error.
func ParseHex(hex string) (r, g, b int, err error) {
	if len(hex) != 7 || hex[0] != '#' {
		return 0, 0, 0, fmt.Errorf("color %q is not of the form #rrggbb", hex)
	}
	var rgb [3]int
	for i := range rgb {
		v, perr := strconv.ParseUint(hex[1+2*i:3+2*i], 16, 8)
		if perr != nil {
			return 0, 0, 0, fmt.Errorf("color %q is not of the form #rrggbb", hex)
		}
		rgb[i] = int(v)
	}
	return rgb[0], rgb[1], rgb[2], nil
}

// colorParser turns theme colors into sequences, keeping the first error and
// the name of the key that caused it.
type colorParser struct {
	err error
}

func (p *colorParser) seq(key, hex string, build func(r, g, b int) string) string {
	if p.err != nil || hex == "" {
		return ""
	}
	r, g, b, err := ParseHex(hex)
	if err != nil {
		p.err = fmt.Errorf("theme key %s: %w", key, err)
		return ""
	}
	return build(r, g, b)
}

func (p *colorParser) fg(key, hex string) string { return p.seq(key, hex, terminal.FgRGB) }
func (p *colorParser) bg(key, hex string) string { return p.seq(key, hex, terminal.BgRGB) }

// ParseTheme turns a theme's hex colors into escape sequences. An empty color
// is the empty sequence; a color that is not "#rrggbb" is an error naming
// its key.
func ParseTheme(t appconfig.Theme) (Colors, error) {
	var p colorParser
	segments := make(map[string]SegmentColors, len(t.Segments))
	for key, s := range t.Segments {
		unavailable := ""
		if s.UnavailableFg != nil {
			unavailable = *s.UnavailableFg
		}
		prefix := "segments." + key + "."
		segments[key] = SegmentColors{
			ValueFg:       p.fg(prefix+"value_fg", s.ValueFg),
			FocusBg:       p.bg(prefix+"focus_bg", s.FocusBg),
			FocusFg:       p.fg(prefix+"focus_fg", s.FocusFg),
			OptionFg:      p.fg(prefix+"option_fg", s.OptionFg),
			UnavailableFg: p.fg(prefix+"unavailable_fg", unavailable),
		}
	}

	// Derived from the lifecycle state list, so a new state cannot be missed:
	// a state the theme has no field for is an error from StateFg.
	stateFg := make(map[string]string)
	for _, state := range lifecycle.States() {
		hex, err := t.Sessions.StateFg(state)
		if err != nil {
			return Colors{}, err
		}
		stateFg[state] = p.fg("sessions.state_"+strings.ReplaceAll(state, "-", "_")+"_fg", hex)
	}

	c := Colors{
		GlobalFg:       p.fg("global.fg", t.Global.Fg),
		LabelFg:        p.fg("global.label_fg", t.Global.LabelFg),
		SeparatorFg:    p.fg("global.separator_fg", t.Global.SeparatorFg),
		SeparatorChar:  t.Global.SeparatorChar,
		EmptyValueFg:   p.fg("global.empty_value_fg", t.Global.EmptyValueFg),
		EmptyValueText: t.Global.EmptyValueText,

		segments: segments,

		SearchCursorFg:  p.fg("search.cursor_fg", t.Search.CursorFg),
		SearchMatchFg:   p.fg("search.match_fg", t.Search.MatchFg),
		SearchNoMatchFg: p.fg("search.no_match_fg", t.Search.NoMatchFg),

		OverflowArrowFg:          p.fg("overflow.arrow_fg", t.Overflow.ArrowFg),
		OverflowMinimapFg:        p.fg("overflow.minimap_fg", t.Overflow.MinimapFg),
		OverflowMinimapFocusedBg: p.bg("overflow.minimap_focused_bg", t.Overflow.MinimapFocusedBg),
		OverflowMinimapChar:      t.Overflow.MinimapChar,

		FormsTitleFg:    p.fg("forms.title_fg", t.Forms.TitleFg),
		FormsFocusBg:    p.bg("forms.focus_bg", t.Forms.FocusBg),
		FormsFocusFg:    p.fg("forms.focus_fg", t.Forms.FocusFg),
		FormsFieldFg:    p.fg("forms.field_fg", t.Forms.FieldFg),
		FormsErrorFg:    p.fg("forms.error_fg", t.Forms.ErrorFg),
		FormsHintFg:     p.fg("forms.hint_fg", t.Forms.HintFg),
		FormsCursorFg:   p.fg("forms.cursor_fg", t.Forms.CursorFg),
		FormsReadonlyFg: p.fg("forms.readonly_fg", t.Forms.ReadonlyFg),

		SessionsFrameFg:   p.fg("sessions.frame_fg", t.Sessions.FrameFg),
		SessionsHeaderFg:  p.fg("sessions.header_fg", t.Sessions.HeaderFg),
		SessionsRowFg:     p.fg("sessions.row_fg", t.Sessions.RowFg),
		SessionsFocusBg:   p.bg("sessions.focus_bg", t.Sessions.FocusBg),
		SessionsFocusFg:   p.fg("sessions.focus_fg", t.Sessions.FocusFg),
		SessionsDetailFg:  p.fg("sessions.detail_fg", t.Sessions.DetailFg),
		SessionsHintFg:    p.fg("sessions.hint_fg", t.Sessions.HintFg),
		SessionsMessageFg: p.fg("sessions.message_fg", t.Sessions.MessageFg),

		stateFg: stateFg,
	}
	if p.err != nil {
		return Colors{}, fmt.Errorf("theme %q: %w", t.Name, p.err)
	}
	return c, nil
}

// TerminalBackground asks the terminal for its background, in the form
// appconfig.ResolveThemeName takes: "light", "dark", or "" when the terminal
// gave no answer. Call it before the terminal enters cbreak mode.
func TerminalBackground(ctx context.Context) func() (string, error) {
	return func() (string, error) {
		scheme, err := terminal.DetectBackground(ctx)
		if err != nil {
			return "", err
		}
		return string(scheme), nil
	}
}

// LoadColors resolves the configured theme name (asking the terminal when it
// is "auto"), loads that theme file, and parses it.
func LoadColors(ctx context.Context, ws workspace.Workspace, configured string) (Colors, error) {
	name, err := appconfig.ResolveThemeName(configured, TerminalBackground(ctx))
	if err != nil {
		return Colors{}, err
	}
	theme, err := appconfig.LoadTheme(ws, name)
	if err != nil {
		return Colors{}, err
	}
	return ParseTheme(theme)
}
