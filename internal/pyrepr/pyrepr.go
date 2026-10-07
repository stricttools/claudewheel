// Package pyrepr renders decoded JSON trees as Python renders what json.loads
// makes of them (None, True, 'text', [a, b], {'k': v}), the spelling the
// Python implementation's messages and listings used and the Go port keeps.
// Numbers keep their text as written.
package pyrepr

import (
	"encoding/json"
	"fmt"
	"strings"
	"unicode"

	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// Repr is Python's repr of the decoded value v.
func Repr(v jsonfile.Value) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case bool:
		if t {
			return "True"
		}
		return "False"
	case json.Number:
		return string(t)
	case string:
		return stringRepr(t)
	case []jsonfile.Value:
		parts := make([]string, len(t))
		for i, item := range t {
			parts[i] = Repr(item)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case *jsonfile.Object:
		keys := t.Keys()
		parts := make([]string, len(keys))
		for i, k := range keys {
			item, _ := t.Get(k)
			parts[i] = stringRepr(k) + ": " + Repr(item)
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	return fmt.Sprintf("%v", v)
}

// Str is Python's str of the decoded value v: a string as it is, anything
// else as Repr.
func Str(v jsonfile.Value) string {
	if s, ok := v.(string); ok {
		return s
	}
	return Repr(v)
}

// StringList is Repr of a list of strings.
func StringList(items []string) string {
	values := make([]jsonfile.Value, len(items))
	for i, s := range items {
		values[i] = s
	}
	return Repr(values)
}

// stringRepr quotes s as Python's repr of a str: single quotes unless s
// holds a single quote and no double quote; backslash, the quote, and
// non-printable characters escaped.
func stringRepr(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, r := range s {
		switch {
		case r == quote || r == '\\':
			b.WriteRune('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || (r >= 0x7f && r <= 0xff && !unicode.IsPrint(r)):
			fmt.Fprintf(&b, `\x%02x`, r)
		case !unicode.IsPrint(r) && r <= 0xffff:
			fmt.Fprintf(&b, `\u%04x`, r)
		case !unicode.IsPrint(r):
			fmt.Fprintf(&b, `\U%08x`, r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteRune(quote)
	return b.String()
}
