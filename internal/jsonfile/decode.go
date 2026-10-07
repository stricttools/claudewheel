package jsonfile

import (
	"bytes"
	"encoding/json"
	"fmt"
	"unicode/utf8"
)

// maxDepth bounds nesting, as Python's recursion limit bounds json.loads.
const maxDepth = 10000

// Decode parses one JSON document the way Python's json.loads parses the text
// of a file read as UTF-8, with these differences: NaN, Infinity, and
// -Infinity are refused, and numbers are kept as their text (json.Number).
// Invalid UTF-8, a byte order mark, a raw control character inside a string,
// and anything after the value other than whitespace are errors. A key that
// appears twice keeps its first position and takes its last value.
func Decode(data []byte) (Value, error) {
	return decodeDocument(data, false)
}

// DecodeObject is Decode for a document whose top level must be an object.
func DecodeObject(data []byte) (*Object, error) {
	v, err := Decode(data)
	if err != nil {
		return nil, err
	}
	o, ok := v.(*Object)
	if !ok {
		return nil, fmt.Errorf("JSON top level is %s, not an object", describe(v))
	}
	return o, nil
}

func decodeDocument(data []byte, refuseDuplicates bool) (Value, error) {
	d := &decoder{data: data, refuseDuplicates: refuseDuplicates}
	if bytes.HasPrefix(data, []byte("\xef\xbb\xbf")) {
		return nil, d.errorAt(0, "unexpected UTF-8 byte order mark")
	}
	if !utf8.Valid(data) {
		for i := 0; i < len(data); {
			r, size := utf8.DecodeRune(data[i:])
			if r == utf8.RuneError && size == 1 {
				return nil, d.errorAt(i, "invalid UTF-8")
			}
			i += size
		}
	}
	d.skipSpace()
	v, err := d.value()
	if err != nil {
		return nil, err
	}
	d.skipSpace()
	if d.pos != len(d.data) {
		return nil, d.errorAt(d.pos, "extra data after the JSON value")
	}
	return v, nil
}

// describe names the JSON type of a tree value, for error messages.
func describe(v Value) string {
	switch v.(type) {
	case nil:
		return "null"
	case bool:
		return "a boolean"
	case json.Number:
		return "a number"
	case string:
		return "a string"
	case []Value:
		return "an array"
	case *Object:
		return "an object"
	}
	return fmt.Sprintf("a Go %T", v)
}

type decoder struct {
	data             []byte
	pos              int
	depth            int
	refuseDuplicates bool
}

// errorAt reports a syntax error with Python's line and column numbering
// (both from 1; the column counts characters).
func (d *decoder) errorAt(pos int, msg string) error {
	line := 1 + bytes.Count(d.data[:pos], []byte("\n"))
	lineStart := bytes.LastIndexByte(d.data[:pos], '\n') + 1
	column := 1 + utf8.RuneCount(d.data[lineStart:pos])
	return fmt.Errorf("invalid JSON: %s: line %d column %d (char %d)", msg, line, column, utf8.RuneCount(d.data[:pos]))
}

func (d *decoder) skipSpace() {
	for d.pos < len(d.data) {
		switch d.data[d.pos] {
		case ' ', '\t', '\n', '\r':
			d.pos++
		default:
			return
		}
	}
}

func (d *decoder) value() (Value, error) {
	if d.pos >= len(d.data) {
		return nil, d.errorAt(d.pos, "expecting value")
	}
	switch c := d.data[d.pos]; {
	case c == '{':
		return d.object()
	case c == '[':
		return d.array()
	case c == '"':
		return d.str()
	case c == '-' || isDigit(c):
		return d.number()
	}
	for _, lit := range []struct {
		text  string
		value Value
	}{{"true", true}, {"false", false}, {"null", nil}} {
		if bytes.HasPrefix(d.data[d.pos:], []byte(lit.text)) {
			d.pos += len(lit.text)
			return lit.value, nil
		}
	}
	return nil, d.errorAt(d.pos, "expecting value")
}

func (d *decoder) enter() error {
	d.depth++
	if d.depth > maxDepth {
		return d.errorAt(d.pos, "nesting too deep")
	}
	return nil
}

func (d *decoder) object() (Value, error) {
	if err := d.enter(); err != nil {
		return nil, err
	}
	defer func() { d.depth-- }()
	d.pos++
	o := NewObject()
	d.skipSpace()
	if d.pos < len(d.data) && d.data[d.pos] == '}' {
		d.pos++
		return o, nil
	}
	for {
		if d.pos >= len(d.data) || d.data[d.pos] != '"' {
			return nil, d.errorAt(d.pos, "expecting property name enclosed in double quotes")
		}
		keyPos := d.pos
		key, err := d.str()
		if err != nil {
			return nil, err
		}
		d.skipSpace()
		if d.pos >= len(d.data) || d.data[d.pos] != ':' {
			return nil, d.errorAt(d.pos, "expecting ':' delimiter")
		}
		d.pos++
		d.skipSpace()
		v, err := d.value()
		if err != nil {
			return nil, err
		}
		if _, dup := o.Get(key); dup && d.refuseDuplicates {
			return nil, d.errorAt(keyPos, fmt.Sprintf("duplicate key %q", key))
		}
		o.Set(key, v)
		d.skipSpace()
		if d.pos >= len(d.data) {
			return nil, d.errorAt(d.pos, "expecting ',' delimiter")
		}
		switch d.data[d.pos] {
		case '}':
			d.pos++
			return o, nil
		case ',':
			d.pos++
			d.skipSpace()
		default:
			return nil, d.errorAt(d.pos, "expecting ',' delimiter")
		}
	}
}

func (d *decoder) array() (Value, error) {
	if err := d.enter(); err != nil {
		return nil, err
	}
	defer func() { d.depth-- }()
	d.pos++
	items := []Value{}
	d.skipSpace()
	if d.pos < len(d.data) && d.data[d.pos] == ']' {
		d.pos++
		return items, nil
	}
	for {
		v, err := d.value()
		if err != nil {
			return nil, err
		}
		items = append(items, v)
		d.skipSpace()
		if d.pos >= len(d.data) {
			return nil, d.errorAt(d.pos, "expecting ',' delimiter")
		}
		switch d.data[d.pos] {
		case ']':
			d.pos++
			return items, nil
		case ',':
			d.pos++
			d.skipSpace()
		default:
			return nil, d.errorAt(d.pos, "expecting ',' delimiter")
		}
	}
}

func (d *decoder) number() (Value, error) {
	start := d.pos
	end := start
	if end < len(d.data) && d.data[end] == '-' {
		end++
	}
	digits := func() int {
		n := 0
		for end < len(d.data) && isDigit(d.data[end]) {
			end++
			n++
		}
		return n
	}
	if end < len(d.data) && d.data[end] == '0' {
		end++
	} else if digits() == 0 {
		return nil, d.errorAt(start, "expecting value")
	}
	if end+1 < len(d.data) && d.data[end] == '.' && isDigit(d.data[end+1]) {
		end++
		digits()
	}
	if end < len(d.data) && (d.data[end] == 'e' || d.data[end] == 'E') {
		mark := end
		end++
		if end < len(d.data) && (d.data[end] == '+' || d.data[end] == '-') {
			end++
		}
		if digits() == 0 {
			end = mark
		}
	}
	d.pos = end
	return json.Number(d.data[start:end]), nil
}

// str parses a string at d.pos (an opening quote). Escapes are decoded as
// Python decodes them; a \u escape for a surrogate without its partner is
// kept as that code unit's three-byte form.
func (d *decoder) str() (string, error) {
	d.pos++
	var out []byte
	for {
		if d.pos >= len(d.data) {
			return "", d.errorAt(d.pos, "unterminated string")
		}
		c := d.data[d.pos]
		switch {
		case c == '"':
			d.pos++
			return string(out), nil
		case c < 0x20:
			return "", d.errorAt(d.pos, "invalid control character in string")
		case c != '\\':
			out = append(out, c)
			d.pos++
			continue
		}
		escPos := d.pos
		d.pos++
		if d.pos >= len(d.data) {
			return "", d.errorAt(escPos, "unterminated string")
		}
		e := d.data[d.pos]
		d.pos++
		switch e {
		case '"', '\\', '/':
			out = append(out, e)
		case 'b':
			out = append(out, '\b')
		case 'f':
			out = append(out, '\f')
		case 'n':
			out = append(out, '\n')
		case 'r':
			out = append(out, '\r')
		case 't':
			out = append(out, '\t')
		case 'u':
			unit, ok := hex4(d.data, d.pos)
			if !ok {
				return "", d.errorAt(escPos, "invalid \\uXXXX escape")
			}
			d.pos += 4
			r := rune(unit)
			if unit >= 0xd800 && unit <= 0xdbff && d.pos+1 < len(d.data) && d.data[d.pos] == '\\' && d.data[d.pos+1] == 'u' {
				low, ok := hex4(d.data, d.pos+2)
				if !ok {
					return "", d.errorAt(d.pos, "invalid \\uXXXX escape")
				}
				if low >= 0xdc00 && low <= 0xdfff {
					r = 0x10000 + (rune(unit)-0xd800)<<10 + (rune(low) - 0xdc00)
					d.pos += 6
				}
			}
			out = appendCodePoint(out, r)
		default:
			return "", d.errorAt(escPos, "invalid \\escape")
		}
	}
}

func hex4(data []byte, pos int) (uint16, bool) {
	if pos+4 > len(data) {
		return 0, false
	}
	var v uint16
	for _, c := range data[pos : pos+4] {
		v <<= 4
		switch {
		case c >= '0' && c <= '9':
			v |= uint16(c - '0')
		case c >= 'a' && c <= 'f':
			v |= uint16(c - 'a' + 10)
		case c >= 'A' && c <= 'F':
			v |= uint16(c - 'A' + 10)
		default:
			return 0, false
		}
	}
	return v, true
}

// appendCodePoint appends r as UTF-8, or a lone surrogate as its three-byte
// generalized UTF-8 form (which utf8.AppendRune would replace with U+FFFD).
func appendCodePoint(out []byte, r rune) []byte {
	if r >= 0xd800 && r <= 0xdfff {
		return append(out, byte(0xe0|r>>12), byte(0x80|(r>>6)&0x3f), byte(0x80|r&0x3f))
	}
	return utf8.AppendRune(out, r)
}
