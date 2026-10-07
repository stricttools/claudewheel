package health

import (
	"encoding/json"
	"fmt"
	"strings"
	"unicode"

	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// pyRepr renders a decoded JSON value as Python's repr renders what
// json.loads makes of it (None, True, 'text', [a, b], {'k': v}), the
// spelling the health details have always used. Numbers keep their text.
func pyRepr(v jsonfile.Value) string {
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
		return pyStringRepr(t)
	case []jsonfile.Value:
		parts := make([]string, len(t))
		for i, item := range t {
			parts[i] = pyRepr(item)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case *jsonfile.Object:
		keys := t.Keys()
		parts := make([]string, len(keys))
		for i, k := range keys {
			item, _ := t.Get(k)
			parts[i] = pyStringRepr(k) + ": " + pyRepr(item)
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	return fmt.Sprintf("%v", v)
}

// pyStringRepr quotes s as Python's repr of a str: single quotes unless s
// holds a single quote and no double quote; backslash, the quote, and
// non-printable characters escaped.
func pyStringRepr(s string) string {
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

// pyStringListRepr is pyRepr of a list of strings.
func pyStringListRepr(items []string) string {
	values := make([]jsonfile.Value, len(items))
	for i, s := range items {
		values[i] = s
	}
	return pyRepr(values)
}
