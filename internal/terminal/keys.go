package terminal

import (
	"context"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// Key is one decoded keypress: a named key below, a single typed character,
// or the raw text of an escape sequence with no name ("CSI<params><final>",
// "CSI<params>~", or "ESC[<byte>").
type Key string

// The named keys ReadKey returns.
const (
	KeyUp        Key = "UP"
	KeyDown      Key = "DOWN"
	KeyRight     Key = "RIGHT"
	KeyLeft      Key = "LEFT"
	KeyHome      Key = "HOME"
	KeyEnd       Key = "END"
	KeyInsert    Key = "INSERT"
	KeyDelete    Key = "DELETE"
	KeyPageUp    Key = "PGUP"
	KeyPageDown  Key = "PGDN"
	KeyShiftTab  Key = "SHIFT_TAB"
	KeyEsc       Key = "ESC"
	KeyEnter     Key = "ENTER"
	KeyTab       Key = "TAB"
	KeyBackspace Key = "BACKSPACE"
	KeyCtrlC     Key = "CTRL_C"
	KeyCtrlD     Key = "CTRL_D"
	// Mode 2031 notifications: the terminal switched to a dark or light scheme.
	KeyThemeDark  Key = "THEME_DARK"
	KeyThemeLight Key = "THEME_LIGHT"
	// The terminal was resized; Rows and Cols already hold the new size, and
	// the caller redraws.
	KeyResize Key = "RESIZE"
)

// escFollowTimeout is how long a lone ESC waits for the rest of an escape
// sequence before it counts as the Escape key.
const escFollowTimeout = 50 * time.Millisecond

// ReadKey waits for one keypress and decodes it. It returns KeyResize, after
// updating Rows and Cols, when SIGWINCH arrived before the key. When ctx is
// done it returns promptly with the cancellation cause (ErrInterrupted after
// Ctrl-C under WithSignals).
func (t *Terminal) ReadKey(ctx context.Context) (Key, error) {
	res, err := t.waitInput(ctx, -1, true)
	if err != nil {
		return "", err
	}
	if res == inputResized {
		rows, cols, err := t.Size()
		if err != nil {
			return "", err
		}
		t.Rows, t.Cols = rows, cols
		return KeyResize, nil
	}
	b, err := t.takeByte()
	if err != nil {
		return "", err
	}
	switch b {
	case 0x1b:
		return t.readEscape(ctx)
	case '\r', '\n':
		return KeyEnter, nil
	case '\t':
		return KeyTab, nil
	case 0x7f, 0x08:
		return KeyBackspace, nil
	case 0x03:
		return KeyCtrlC, nil
	case 0x04:
		return KeyCtrlD, nil
	}
	if b < utf8.RuneSelf {
		return Key(string(rune(b))), nil
	}
	return t.readRune(ctx, b)
}

// readEscape decodes what follows an ESC byte. Anything after ESC other than
// "[" is consumed and reported as the Escape key.
func (t *Terminal) readEscape(ctx context.Context) (Key, error) {
	res, err := t.waitInput(ctx, escFollowTimeout, false)
	if err != nil {
		return "", err
	}
	if res == inputTimedOut {
		return KeyEsc, nil
	}
	b2, err := t.takeByte()
	if err != nil {
		return "", err
	}
	if b2 != '[' {
		return KeyEsc, nil
	}
	b3, err := t.readByte(ctx)
	if err != nil {
		return "", err
	}
	if b3 >= 0x30 && b3 <= 0x3f {
		return t.readCSI(ctx, b3)
	}
	switch b3 {
	case 'A':
		return KeyUp, nil
	case 'B':
		return KeyDown, nil
	case 'C':
		return KeyRight, nil
	case 'D':
		return KeyLeft, nil
	case 'H':
		return KeyHome, nil
	case 'F':
		return KeyEnd, nil
	case 'Z':
		return KeyShiftTab, nil
	}
	return Key("ESC[" + byteText(b3)), nil
}

// readCSI consumes a multi-byte CSI sequence whose first parameter byte is
// first: parameter and intermediate bytes (0x20-0x3F) through the final
// byte, so nothing leaks into the next read.
func (t *Terminal) readCSI(ctx context.Context, first byte) (Key, error) {
	var params strings.Builder
	params.WriteByte(first)
	var final byte
	for {
		b, err := t.readByte(ctx)
		if err != nil {
			return "", err
		}
		if b >= 0x20 && b <= 0x3f {
			params.WriteByte(b)
			continue
		}
		final = b
		break
	}
	p := params.String()
	switch final {
	case '~':
		switch p {
		case "2":
			return KeyInsert, nil
		case "3":
			return KeyDelete, nil
		case "5":
			return KeyPageUp, nil
		case "6":
			return KeyPageDown, nil
		}
		return Key("CSI" + p + "~"), nil
	case 'Z':
		// Parametric Shift-Tab, such as ESC[1;2Z.
		return KeyShiftTab, nil
	case 'n':
		switch p {
		case "?997;1":
			return KeyThemeDark, nil
		case "?997;2":
			return KeyThemeLight, nil
		}
	}
	return Key("CSI" + p + byteText(final)), nil
}

// readRune completes a UTF-8 character whose lead byte is lead. An invalid
// sequence is the replacement character, and a byte that cannot continue it
// is kept for the next key.
func (t *Terminal) readRune(ctx context.Context, lead byte) (Key, error) {
	var size int
	switch {
	case lead >= 0xc2 && lead <= 0xdf:
		size = 2
	case lead >= 0xe0 && lead <= 0xef:
		size = 3
	case lead >= 0xf0 && lead <= 0xf4:
		size = 4
	default:
		return Key(string(utf8.RuneError)), nil
	}
	buf := []byte{lead}
	for len(buf) < size {
		b, err := t.readByte(ctx)
		if err != nil {
			return "", err
		}
		if b&0xc0 != 0x80 {
			t.pushback = append(t.pushback, b)
			return Key(string(utf8.RuneError)), nil
		}
		buf = append(buf, b)
	}
	r, _ := utf8.DecodeRune(buf)
	return Key(string(r)), nil
}

// byteText renders one sequence byte as text; a byte that is not ASCII is
// the replacement character.
func byteText(b byte) string {
	if b >= utf8.RuneSelf {
		return string(utf8.RuneError)
	}
	return string(rune(b))
}

// Char returns the typed character when the key is one character, and false
// for named keys and escape sequences.
func (k Key) Char() (rune, bool) {
	r, size := utf8.DecodeRuneInString(string(k))
	if size == 0 || size != len(k) {
		return 0, false
	}
	return r, true
}

// ReadMaskedLine reads a line with echo suppressed, writing mask for every
// typed character and never the character itself, so a secret can be
// entered without appearing on screen or in captured output. It enters
// cbreak mode (without the alternate screen) for the read when the terminal
// is not already raw, and restores it afterwards.
//
// A printable character accumulates; Backspace removes the last one; Enter
// ends the line and returns it; Escape, Ctrl-D, and a Ctrl-C byte return
// ErrEntryCancelled; every other key is ignored. When ctx is done it
// returns the cancellation cause.
func (t *Terminal) ReadMaskedLine(ctx context.Context, prompt, mask string) (line string, err error) {
	if !t.inRaw {
		if err := t.EnterRaw(false); err != nil {
			return "", err
		}
		defer func() {
			if exitErr := t.ExitRaw(); exitErr != nil && err == nil {
				line, err = "", exitErr
			}
		}()
	}
	if prompt != "" {
		if err := t.Write(prompt); err != nil {
			return "", err
		}
	}
	var chars []rune
	for {
		key, err := t.ReadKey(ctx)
		if err != nil {
			return "", err
		}
		switch key {
		case KeyEnter:
			if err := t.Write("\r\n"); err != nil {
				return "", err
			}
			return string(chars), nil
		case KeyCtrlC, KeyCtrlD, KeyEsc:
			return "", ErrEntryCancelled
		case KeyBackspace:
			if len(chars) > 0 {
				chars = chars[:len(chars)-1]
				if err := t.Write("\b \b"); err != nil {
					return "", err
				}
			}
			continue
		}
		if r, ok := key.Char(); ok && unicode.IsPrint(r) {
			chars = append(chars, r)
			if err := t.Write(mask); err != nil {
				return "", err
			}
		}
	}
}
