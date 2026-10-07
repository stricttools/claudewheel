package widgets

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/stricttools/claudewheel/internal/terminal"
)

// fieldAreaWidth is the width of the left-aligned field area, which is
// centered on the screen.
const fieldAreaWidth = 60

// FieldType is what a form field is and how it takes keys.
type FieldType int

const (
	// FieldText is a line of typed text in Value.
	FieldText FieldType = iota + 1
	// FieldRadio is one of Choices, held in Value.
	FieldRadio
	// FieldCheckbox is on or off, held in Checked.
	FieldCheckbox
	// FieldReadonly shows Value and takes no focus.
	FieldReadonly
	// FieldButton submits the form on Enter.
	FieldButton
	// FieldSelect is a vertical list of Options; Value is the focused
	// option's key.
	FieldSelect
)

// fieldHints are the keyboard hints shown for the focused field.
var fieldHints = map[FieldType]string{
	FieldText:     "type: edit   tab: next   enter: submit   esc: cancel",
	FieldRadio:    "left/right: cycle   tab: next   esc: cancel",
	FieldCheckbox: "space: toggle   tab: next   esc: cancel",
	FieldButton:   "enter: select   tab: next   esc: cancel",
	FieldSelect:   "up/down: navigate   enter: select   esc: cancel",
}

// Option is one entry of a selection list: the key it answers with and the
// label it shows.
type Option struct {
	Key   string
	Label string
}

// Field is one widget of a form. The form runner changes Value and Checked
// in place, so the caller reads the answers from its fields after a submit.
type Field struct {
	Key   string
	Type  FieldType
	Label string
	// Value is the text of a text or readonly field, the chosen entry of
	// Choices for a radio field, and the focused option key of a select
	// field.
	Value   string
	Checked bool
	// Choices are a radio field's entries.
	Choices []string
	// Options are a select field's entries.
	Options []Option
	// Visible, when set, hides the field while it returns false.
	Visible func(fields []*Field) bool
	// OnChange, when set, runs after every edit of a text field, so
	// dependent fields can follow it.
	OnChange func(fields []*Field)
}

// Get returns the field whose key is key. Fields are declared by the code
// that builds the form, so a missing key is a programming error and panics.
func Get(fields []*Field, key string) *Field {
	for _, f := range fields {
		if f.Key == key {
			return f
		}
	}
	panic(fmt.Sprintf("widgets: no form field with key %q", key))
}

// Form is a titled list of fields. Validate, when set, runs on submit; a
// non-empty message blocks the submit and is shown as the form's error.
type Form struct {
	Title    string
	Fields   []*Field
	Validate func(fields []*Field) string
}

// errNoFocusable is returned when no field can take the focus.
var errNoFocusable = errors.New("the form has no visible field that can take the focus")

func isVisible(fields []*Field, f *Field) bool {
	return f.Visible == nil || f.Visible(fields)
}

func focusableIndices(fields []*Field) []int {
	var out []int
	for i, f := range fields {
		if f.Type != FieldReadonly && isVisible(fields, f) {
			out = append(out, i)
		}
	}
	return out
}

// moveFocus moves the focus by step through the focusable fields, wrapping
// at both ends; a focus no longer focusable goes to the first one.
func moveFocus(fields []*Field, focus, step int) (int, error) {
	focusable := focusableIndices(fields)
	if len(focusable) == 0 {
		return 0, errNoFocusable
	}
	pos := slices.Index(focusable, focus)
	if pos < 0 {
		return focusable[0], nil
	}
	n := len(focusable)
	return focusable[((pos+step)%n+n)%n], nil
}

// cycle returns the entry step places from current in entries, wrapping; a
// current value not among them counts as the first.
func cycle(entries []string, current string, step int) string {
	n := len(entries)
	idx := max(slices.Index(entries, current), 0)
	return entries[((idx+step)%n+n)%n]
}

func cycleRadio(f *Field, step int) {
	if len(f.Choices) == 0 {
		return
	}
	f.Value = cycle(f.Choices, f.Value, step)
}

func cycleSelect(f *Field, step int) {
	if len(f.Options) == 0 {
		return
	}
	keys := make([]string, len(f.Options))
	for i, o := range f.Options {
		keys[i] = o.Key
	}
	f.Value = cycle(keys, f.Value, step)
}

func hintsFor(f *Field) string {
	if h, ok := fieldHints[f.Type]; ok {
		return h
	}
	return "esc: cancel"
}

// fieldLines renders one field; a select spans one line per option. A
// focused widget is a focus background and foreground span, never bold.
func fieldLines(f *Field, focused bool, c Colors) []string {
	reset := terminal.Reset
	if f.Type == FieldSelect {
		lines := make([]string, 0, len(f.Options))
		for _, o := range f.Options {
			pointer := "  "
			if o.Key == f.Value {
				pointer = "> "
			}
			style := c.FormsFieldFg
			if focused && o.Key == f.Value {
				style = c.FormsFocusBg + c.FormsFocusFg
			}
			lines = append(lines, style+pointer+o.Label+reset)
		}
		return lines
	}

	labelStyle := c.FormsFieldFg
	if focused {
		labelStyle = c.FormsFocusBg + c.FormsFocusFg
	}
	switch f.Type {
	case FieldButton:
		return []string{labelStyle + "[ " + f.Label + " ]" + reset}
	case FieldText:
		cursor := ""
		if focused {
			cursor = c.FormsCursorFg + "_" + reset
		}
		return []string{
			labelStyle + f.Label + ":" + reset + " " +
				c.FormsFieldFg + "[" + f.Value + reset + cursor +
				c.FormsFieldFg + "]" + reset,
		}
	case FieldReadonly:
		return []string{c.FormsFieldFg + f.Label + ":" + reset + " " + c.FormsReadonlyFg + f.Value + reset}
	case FieldRadio:
		parts := make([]string, 0, len(f.Choices))
		for _, choice := range f.Choices {
			if choice == f.Value {
				parts = append(parts, terminal.Bold+c.FormsFieldFg+"(*) "+choice+reset)
			} else {
				parts = append(parts, c.FormsFieldFg+"( ) "+choice+reset)
			}
		}
		return []string{labelStyle + f.Label + ":" + reset + " " + strings.Join(parts, "  ")}
	case FieldCheckbox:
		marker := "[ ]"
		if f.Checked {
			marker = "[x]"
		}
		return []string{labelStyle + marker + " " + f.Label + reset}
	}
	return []string{labelStyle + f.Label + reset}
}

// centered returns the 1-based column that centers text of width w on a
// screen cols wide, at least column 1.
func centered(cols, w int) int {
	return max(1, (cols-w)/2)
}

func textWidth(s string) int {
	return utf8.RuneCountInString(s)
}

// renderForm draws the form centered on the whole screen.
func renderForm(t *terminal.Terminal, c Colors, form Form, focus int, errMsg string) error {
	rows, cols := t.Rows, t.Cols
	visible := make([]bool, len(form.Fields))
	lineCount := 0
	for i, f := range form.Fields {
		visible[i] = isVisible(form.Fields, f)
		if !visible[i] {
			continue
		}
		if f.Type == FieldSelect {
			lineCount += len(f.Options)
		} else {
			lineCount++
		}
	}

	startRow := max(1, (rows-(2+lineCount))/2)
	var buf strings.Builder
	buf.WriteString(terminal.ClearScreen)
	buf.WriteString(terminal.MoveTo(startRow, centered(cols, textWidth(form.Title))))
	buf.WriteString(terminal.Bold + c.FormsTitleFg + form.Title + terminal.Reset)

	leftCol := centered(cols, fieldAreaWidth)
	row := startRow + 2
	for i, f := range form.Fields {
		if !visible[i] {
			continue
		}
		col := leftCol
		if f.Type == FieldButton {
			col = centered(cols, textWidth(f.Label)+4)
		}
		for _, line := range fieldLines(f, i == focus, c) {
			buf.WriteString(terminal.MoveTo(row, col) + line)
			row++
		}
	}

	if errMsg != "" {
		buf.WriteString(terminal.MoveTo(rows-1, centered(cols, textWidth(errMsg))))
		buf.WriteString(terminal.Bold + c.FormsErrorFg + errMsg + terminal.Reset)
	}
	buf.WriteString(terminal.MoveTo(rows, 2) + c.FormsHintFg + hintsFor(form.Fields[focus]) + terminal.Reset)
	return t.Write(buf.String())
}

// requireRaw refuses a terminal its caller has not put in cbreak mode.
func requireRaw(t *terminal.Terminal) error {
	if !t.Raw() {
		return errors.New("the terminal is not in cbreak mode: enter it (terminal.EnterRaw) before drawing a widget")
	}
	return nil
}

// isNotification reports keys that are the terminal speaking, not the user
// typing: a color-scheme change. Widgets ignore them.
func isNotification(key terminal.Key) bool {
	return key == terminal.KeyThemeDark || key == terminal.KeyThemeLight
}

// RunForm runs the form's key loop on the whole screen and reports whether
// it was submitted; the answers are in the fields. Escape (or a Ctrl-C byte)
// cancels and reports false. Tab, Shift-Tab, Up, and Down move the focus
// (Up and Down move within a select field instead); Left, Right, and Space
// cycle a radio field; Space toggles a checkbox; Enter on a text, button,
// or select field submits, unless Validate refuses. When ctx is done it
// returns the cancellation cause.
func RunForm(ctx context.Context, t *terminal.Terminal, c Colors, form Form) (bool, error) {
	if err := requireRaw(t); err != nil {
		return false, err
	}
	focusable := focusableIndices(form.Fields)
	if len(focusable) == 0 {
		return false, errNoFocusable
	}
	focus := focusable[0]
	errMsg := ""

	trySubmit := func() bool {
		if form.Validate != nil {
			if msg := form.Validate(form.Fields); msg != "" {
				errMsg = msg
				return false
			}
		}
		return true
	}
	move := func(step int) error {
		errMsg = ""
		next, err := moveFocus(form.Fields, focus, step)
		if err != nil {
			return err
		}
		focus = next
		return nil
	}

	for {
		if err := renderForm(t, c, form, focus, errMsg); err != nil {
			return false, err
		}
		key, err := t.ReadKey(ctx)
		if err != nil {
			return false, err
		}
		if key == terminal.KeyResize || isNotification(key) {
			continue
		}
		if key == terminal.KeyEsc || key == terminal.KeyCtrlC {
			return false, nil
		}

		f := form.Fields[focus]
		if f.Type == FieldSelect {
			switch key {
			case terminal.KeyEnter:
				if trySubmit() {
					return true, nil
				}
			case terminal.KeyDown:
				cycleSelect(f, 1)
			case terminal.KeyUp:
				cycleSelect(f, -1)
			case terminal.KeyTab:
				err = move(1)
			case terminal.KeyShiftTab:
				err = move(-1)
			}
			if err != nil {
				return false, err
			}
			continue
		}

		ch, isChar := key.Char()
		switch {
		case key == terminal.KeyTab || key == terminal.KeyDown:
			err = move(1)
		case key == terminal.KeyShiftTab || key == terminal.KeyUp:
			err = move(-1)
		case key == terminal.KeyEnter:
			if f.Type == FieldText || f.Type == FieldButton {
				if trySubmit() {
					return true, nil
				}
			}
		case key == " " && f.Type == FieldCheckbox:
			f.Checked = !f.Checked
			errMsg = ""
		case (key == terminal.KeyLeft || key == terminal.KeyRight || key == " ") && f.Type == FieldRadio:
			step := 1
			if key == terminal.KeyLeft {
				step = -1
			}
			cycleRadio(f, step)
			errMsg = ""
		case key == terminal.KeyBackspace && f.Type == FieldText:
			if r := []rune(f.Value); len(r) > 0 {
				f.Value = string(r[:len(r)-1])
			}
			errMsg = ""
			if f.OnChange != nil {
				f.OnChange(form.Fields)
			}
		case f.Type == FieldText && isChar && unicode.IsPrint(ch):
			f.Value += string(ch)
			errMsg = ""
			if f.OnChange != nil {
				f.OnChange(form.Fields)
			}
		}
		if err != nil {
			return false, err
		}
	}
}

// RunSelection runs a vertical selection list and returns the key of the
// option chosen with Enter, or false when Escape cancels it. initialKey
// focuses the option with that key first; "" or a key no option has
// focuses the first option. An empty option list is an error.
func RunSelection(ctx context.Context, t *terminal.Terminal, c Colors, title string, options []Option, initialKey string) (string, bool, error) {
	if len(options) == 0 {
		return "", false, fmt.Errorf("the selection %q has no options", title)
	}
	value := options[0].Key
	for _, o := range options {
		if o.Key == initialKey {
			value = initialKey
			break
		}
	}
	field := &Field{Key: "choice", Type: FieldSelect, Value: value, Options: options}
	submitted, err := RunForm(ctx, t, c, Form{Title: title, Fields: []*Field{field}})
	if err != nil || !submitted {
		return "", false, err
	}
	return field.Value, true, nil
}
