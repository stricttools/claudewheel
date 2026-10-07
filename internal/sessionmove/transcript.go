package sessionmove

import (
	"bytes"

	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// rewriteLines applies rewrite to every line of a transcript that holds
// needle and parses as JSON. A line whose tree comes back equal (as Python
// compares the decoded values) is kept as its original bytes; a changed one
// is written as Claude Code writes a record. Lines are split on "\n" only,
// since JSON strings may hold U+2028 and its kin raw. It returns the new
// text and how many lines changed.
func rewriteLines(data []byte, needle string, rewrite func(jsonfile.Value) jsonfile.Value) ([]byte, int, error) {
	lines := bytes.Split(data, []byte("\n"))
	changed := 0
	for i, line := range lines {
		if !bytes.Contains(line, []byte(needle)) {
			continue
		}
		record, err := jsonfile.Decode(line)
		if err != nil {
			// Not JSON, such as a live session's partial last line: kept.
			continue
		}
		rewritten := rewrite(record)
		if jsonfile.Equal(rewritten, record) {
			continue
		}
		dumped, err := jsonfile.MarshalTranscriptLine(rewritten)
		if err != nil {
			return nil, 0, err
		}
		lines[i] = dumped
		changed++
	}
	if changed == 0 {
		return data, 0, nil
	}
	return bytes.Join(lines, []byte("\n")), changed, nil
}

// mapTree returns a copy of v with every string passed through str and
// every object key through objectKey. key is the name of the field holding
// v ("" for none); a list passes its own key to its items, and an object
// passes each field's original name to its value.
func mapTree(v jsonfile.Value, key string, str func(s, key string) string, objectKey func(k, parent string) string) jsonfile.Value {
	switch t := v.(type) {
	case string:
		return str(t, key)
	case []jsonfile.Value:
		out := make([]jsonfile.Value, len(t))
		for i, item := range t {
			out[i] = mapTree(item, key, str, objectKey)
		}
		return out
	case *jsonfile.Object:
		out := jsonfile.NewObject()
		for _, k := range t.Keys() {
			value, _ := t.Get(k)
			// Set keeps the first position of a key two old keys map to,
			// with the last value, as Python's dict construction does.
			out.Set(objectKey(k, key), mapTree(value, k, str, objectKey))
		}
		return out
	}
	return v
}
