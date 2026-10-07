package sessionsview

import (
	"fmt"
	"strings"

	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// The ticks a list row carries when it is selected, and when it is not.
const (
	SelectorOn  = "[x]"
	SelectorOff = "[ ]"
)

// ListStyle names what one line of a session list is; RenderFrame is the
// only place one becomes an escape sequence.
type ListStyle string

// The list styles. ListRunning and ListStopped are the styles a caller may
// put on a row's state line: the indicator's live and dead colors.
const (
	ListTitle   ListStyle = "title"
	ListHint    ListStyle = "hint"
	ListRowLine ListStyle = "row"
	ListFocus   ListStyle = "focus"
	ListEmpty   ListStyle = "empty"
	ListBlank   ListStyle = "blank"
	ListRunning ListStyle = "running"
	ListStopped ListStyle = "stopped"
)

// listChromeLines are the lines reserved around the scrolling window: the
// title, a blank line under it, a blank line above the hint, and the hint.
const listChromeLines = 4

// FrameLine is one laid-out line of a session list and its style.
type FrameLine struct {
	Text  string
	Style ListStyle
}

// Selection is a list row's tick state.
type Selection int

// The tick states. NoSelector draws no toggle column at all.
const (
	NoSelector Selection = iota
	Selected
	Unselected
)

// SessionBlock is one session in the list, with what the screen decided
// about it.
type SessionBlock struct {
	Record    sessions.SessionRecord
	Selection Selection
	// State is the per-row state line, nil for none; StateStyle names its
	// color and must be set when State is.
	State      *string
	StateStyle ListStyle
	// RSSKiB is the resident memory measured for the record's pid, nil when
	// nothing measured it.
	RSSKiB *int64
}

// Selector is the toggle drawn for the row, empty for NoSelector.
func (b SessionBlock) Selector() string {
	switch b.Selection {
	case Selected:
		return SelectorOn
	case Unselected:
		return SelectorOff
	}
	return ""
}

func blockLines(b SessionBlock, highlighted bool, nowMS int64, identity *Identity) ([]string, error) {
	return FormatRow(b.Record, BlockOptions{
		Highlighted: highlighted,
		NowMS:       nowMS,
		Identity:    identity,
		RSSKiB:      b.RSSKiB,
		State:       b.State,
		Selector:    b.Selector(),
	})
}

// BlockHeights returns the block height of each row, which is what the
// viewport scrolls over.
func BlockHeights(blocks []SessionBlock, focus int, nowMS int64, identity *Identity) ([]int, error) {
	heights := make([]int, len(blocks))
	for i, b := range blocks {
		lines, err := blockLines(b, i == focus, nowMS, identity)
		if err != nil {
			return nil, err
		}
		heights[i] = len(lines)
	}
	return heights, nil
}

// MoveFocus moves the focus by step within count rows, clamped at both ends
// rather than wrapping. An empty list has no focus at all, which is -1.
func MoveFocus(focus, count, step int) int {
	if count <= 0 {
		return -1
	}
	return max(0, min(count-1, focus+step))
}

// ListSpec is what BuildFrame lays out.
type ListSpec struct {
	Blocks    []SessionBlock
	Focus     int
	NowMS     int64
	Title     string
	Hint      string
	Height    int
	Width     int
	Identity  *Identity
	EmptyText string
}

// BuildFrame lays the list out into at most Height lines of at most Width
// columns. The window is centered on the focused row and clamped to the
// content, and a Height too small even for the chrome yields the prefix of
// it that fits.
//
// A row only partly inside the window is left out, its lines drawn blank so
// the hint keeps its place: at the top edge its indented detail line would
// sit directly above the next session's header and read as that session's.
// The focused row is the exception, drawn clipped, because a window too
// short for it would otherwise show nothing at all.
func BuildFrame(spec ListSpec) ([]FrameLine, error) {
	frame := []FrameLine{
		{widgets.RunePrefix(spec.Title, spec.Width), ListTitle},
		{"", ListBlank},
	}
	window := max(0, spec.Height-listChromeLines)

	if len(spec.Blocks) == 0 {
		frame = append(frame, FrameLine{widgets.RunePrefix(spec.EmptyText, spec.Width), ListEmpty})
	} else {
		heights, err := BlockHeights(spec.Blocks, spec.Focus, spec.NowMS, spec.Identity)
		if err != nil {
			return nil, err
		}
		viewport, err := ComputeViewport(heights, spec.Focus, window)
		if err != nil {
			return nil, err
		}
		for _, s := range viewport.Rows {
			block := spec.Blocks[s.Index]
			highlighted := s.Index == spec.Focus
			if s.Clipped() && !highlighted {
				for range s.Lines {
					frame = append(frame, FrameLine{"", ListBlank})
				}
				continue
			}
			lines, err := blockLines(block, highlighted, spec.NowMS, spec.Identity)
			if err != nil {
				return nil, err
			}
			stateIndex := -1
			if block.State != nil {
				if block.StateStyle == "" {
					return nil, fmt.Errorf("session block for pid %d has a state line and no state style", block.Record.PID)
				}
				stateIndex = len(lines) - 1
			}
			for offset, text := range lines[s.SkipTop : s.SkipTop+s.Lines] {
				style := ListRowLine
				switch {
				case s.SkipTop+offset == stateIndex:
					style = block.StateStyle
				case highlighted:
					style = ListFocus
				}
				frame = append(frame, FrameLine{widgets.RunePrefix(text, spec.Width), style})
			}
		}
	}

	frame = append(frame, FrameLine{"", ListBlank}, FrameLine{widgets.RunePrefix(spec.Hint, spec.Width), ListHint})
	if spec.Height >= 0 && len(frame) > spec.Height {
		frame = frame[:spec.Height]
	}
	return frame, nil
}

// listStyleSequence is the escape sequence a list style is drawn in. The two
// indicator colors are the terminal's own green and red rather than theme
// entries: "still up" and "gone" mean the same under every palette. An
// unknown style is an error.
func listStyleSequence(c widgets.Colors, style ListStyle) (string, error) {
	switch style {
	case ListTitle:
		return terminal.Bold + c.FormsTitleFg, nil
	case ListHint:
		return c.FormsHintFg, nil
	case ListRowLine:
		return c.FormsFieldFg, nil
	case ListFocus:
		return terminal.Bold + c.FormsFocusFg, nil
	case ListEmpty:
		return c.FormsReadonlyFg, nil
	case ListBlank:
		return "", nil
	case ListRunning:
		return terminal.Green, nil
	case ListStopped:
		return terminal.Red, nil
	}
	return "", fmt.Errorf("unknown session list style: %q", style)
}

// RenderFrame draws frame over a cleared screen, one line per terminal row,
// starting at column leftCol.
func RenderFrame(t *terminal.Terminal, c widgets.Colors, frame []FrameLine, leftCol int) error {
	var buf strings.Builder
	buf.WriteString(terminal.ClearScreen)
	for i, line := range frame {
		seq, err := listStyleSequence(c, line.Style)
		if err != nil {
			return err
		}
		buf.WriteString(terminal.MoveTo(i+1, leftCol) + seq + line.Text + terminal.Reset)
	}
	return t.Write(buf.String())
}
