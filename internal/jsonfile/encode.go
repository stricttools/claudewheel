package jsonfile

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"unicode/utf8"
)

// The Marshal functions accept any Go value: tree values are written as they
// are, numbers as their text; anything else (structs, maps, Go integers) is
// first passed through encoding/json and decoded back (see Normalize), so a
// struct is written with its fields in declaration order and a Go map with its
// keys sorted.

// MarshalIndented writes v as Python's json.dumps(v, indent=2) + "\n", the
// layout of every JSON file claudewheel writes: two-space indent, items
// separated by "," and a newline, ": " after keys, non-ASCII escaped as lowercase
// \uXXXX (surrogate pairs above the Basic Multilingual Plane), and a
// trailing newline.
func MarshalIndented(v any) ([]byte, error) {
	out, err := marshal(v, style{indented: true, itemSep: ",", keySep: ": ", asciiOnly: true})
	if err != nil {
		return nil, err
	}
	return append(out, '\n'), nil
}

// MarshalCompactASCII writes v as Python's
// json.dumps(v, separators=(",", ":")): no whitespace, non-ASCII escaped. The
// probe and lifecycle JSONL lines use this layout. No newline is added.
func MarshalCompactASCII(v any) ([]byte, error) {
	return marshal(v, style{itemSep: ",", keySep: ":", asciiOnly: true})
}

// MarshalSortedCompactASCII writes v as Python's
// json.dumps(v, sort_keys=True, separators=(",", ":")): MarshalCompactASCII
// with every object's keys sorted by code point.
func MarshalSortedCompactASCII(v any) ([]byte, error) {
	return marshal(v, style{itemSep: ",", keySep: ":", asciiOnly: true, sortKeys: true})
}

// MarshalSpacedASCII writes v as Python's json.dumps(v) with no other
// arguments: ", " between items, ": " after keys, non-ASCII escaped. Messages
// that quote a JSON value use this layout.
func MarshalSpacedASCII(v any) ([]byte, error) {
	return marshal(v, style{itemSep: ", ", keySep: ": ", asciiOnly: true})
}

// MarshalTranscriptLine writes one session transcript record the way
// claudewheel rewrites Claude Code's JSONL lines: Python's
// json.dumps(v, ensure_ascii=False, separators=(",", ":")) with every lone
// surrogate escaped as lowercase \uXXXX. Non-ASCII stays raw; only '"', '\',
// and control characters below 0x20 are escaped. No newline is added.
func MarshalTranscriptLine(v any) ([]byte, error) {
	return marshal(v, style{itemSep: ",", keySep: ":"})
}

type style struct {
	indented  bool
	itemSep   string
	keySep    string
	asciiOnly bool
	sortKeys  bool
}

type writer struct {
	style
	buf []byte
}

func marshal(v any, s style) ([]byte, error) {
	tree, err := Normalize(v)
	if err != nil {
		return nil, err
	}
	w := &writer{style: s}
	if err := w.value(tree, 0); err != nil {
		return nil, err
	}
	return w.buf, nil
}

func (w *writer) newline(level int) {
	w.buf = append(w.buf, '\n')
	w.buf = append(w.buf, strings.Repeat("  ", level)...)
}

func (w *writer) value(v Value, level int) error {
	switch t := v.(type) {
	case nil:
		w.buf = append(w.buf, "null"...)
	case bool:
		if t {
			w.buf = append(w.buf, "true"...)
		} else {
			w.buf = append(w.buf, "false"...)
		}
	case json.Number:
		w.buf = append(w.buf, string(t)...)
	case string:
		return w.str(t)
	case []Value:
		if len(t) == 0 {
			w.buf = append(w.buf, "[]"...)
			return nil
		}
		w.buf = append(w.buf, '[')
		for i, item := range t {
			w.separate(i, level)
			if err := w.value(item, level+1); err != nil {
				return err
			}
		}
		w.close(']', level)
	case *Object:
		if t.Len() == 0 {
			w.buf = append(w.buf, "{}"...)
			return nil
		}
		keys := t.keys
		if w.sortKeys {
			keys = slices.Clone(keys)
			slices.Sort(keys)
		}
		w.buf = append(w.buf, '{')
		for i, key := range keys {
			w.separate(i, level)
			if err := w.str(key); err != nil {
				return err
			}
			w.buf = append(w.buf, w.keySep...)
			item, _ := t.Get(key)
			if err := w.value(item, level+1); err != nil {
				return err
			}
		}
		w.close('}', level)
	default:
		return fmt.Errorf("jsonfile: %T is not a JSON tree value", v)
	}
	return nil
}

// separate writes what precedes the i-th item of a container at level.
func (w *writer) separate(i, level int) {
	if i > 0 {
		w.buf = append(w.buf, w.itemSep...)
	}
	if w.indented {
		w.newline(level + 1)
	}
}

func (w *writer) close(bracket byte, level int) {
	if w.indented {
		w.newline(level)
	}
	w.buf = append(w.buf, bracket)
}

const hexDigits = "0123456789abcdef"

func (w *writer) escapeUnit(u rune) {
	w.buf = append(w.buf, '\\', 'u',
		hexDigits[u>>12&0xf], hexDigits[u>>8&0xf], hexDigits[u>>4&0xf], hexDigits[u&0xf])
}

// str writes s quoted, escaping as Python's json encoder does for the
// writer's ensure_ascii setting.
func (w *writer) str(s string) error {
	w.buf = append(w.buf, '"')
	for i := 0; i < len(s); {
		c := s[i]
		if c < utf8.RuneSelf {
			switch {
			case c == '"':
				w.buf = append(w.buf, '\\', '"')
			case c == '\\':
				w.buf = append(w.buf, '\\', '\\')
			case c == '\n':
				w.buf = append(w.buf, '\\', 'n')
			case c == '\r':
				w.buf = append(w.buf, '\\', 'r')
			case c == '\t':
				w.buf = append(w.buf, '\\', 't')
			case c == '\b':
				w.buf = append(w.buf, '\\', 'b')
			case c == '\f':
				w.buf = append(w.buf, '\\', 'f')
			case c < 0x20 || (c == 0x7f && w.asciiOnly):
				w.escapeUnit(rune(c))
			default:
				w.buf = append(w.buf, c)
			}
			i++
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			unit, ok := loneSurrogate(s, i)
			if !ok {
				return fmt.Errorf("jsonfile: string holds invalid UTF-8 at byte %d: %q", i, s)
			}
			w.escapeUnit(unit)
			i += 3
			continue
		}
		switch {
		case !w.asciiOnly:
			w.buf = append(w.buf, s[i:i+size]...)
		case r >= 0x10000:
			v := r - 0x10000
			w.escapeUnit(0xd800 + v>>10)
			w.escapeUnit(0xdc00 + v&0x3ff)
		default:
			w.escapeUnit(r)
		}
		i += size
	}
	w.buf = append(w.buf, '"')
	return nil
}

// loneSurrogate decodes the three-byte form of a lone surrogate at s[i].
func loneSurrogate(s string, i int) (rune, bool) {
	if i+2 >= len(s) || s[i] != 0xed || s[i+1] < 0xa0 || s[i+1] > 0xbf || s[i+2] < 0x80 || s[i+2] > 0xbf {
		return 0, false
	}
	return 0xd000 | rune(s[i+1]&0x3f)<<6 | rune(s[i+2]&0x3f), true
}
