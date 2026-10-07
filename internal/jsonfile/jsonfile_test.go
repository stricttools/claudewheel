package jsonfile

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// writerCase is one document and the text each of the Python's json.dumps
// layouts writes for it, from scripts/python-expectations json-writers.
type writerCase struct {
	Input              string `json:"input"`
	Indented           string `json:"indented"`
	CompactASCII       string `json:"compact_ascii"`
	SortedCompactASCII string `json:"sorted_compact_ascii"`
	SpacedASCII        string `json:"spaced_ascii"`
	TranscriptLine     string `json:"transcript_line"`
}

// pythonNumberText lists the number texts Python's float repr rewrites, which
// the Go writers keep as written (see the deviations log).
var pythonNumberText = strings.NewReplacer("100000.0", "1e5")

func TestWritersMatchThePython(t *testing.T) {
	data, err := os.ReadFile("testdata/python-writers.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []writerCase
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		v, err := Decode([]byte(c.Input))
		if err != nil {
			t.Fatalf("Decode(%s): %v", c.Input, err)
		}
		writers := []struct {
			name  string
			write func(any) ([]byte, error)
			want  string
		}{
			{"MarshalIndented", MarshalIndented, c.Indented},
			{"MarshalCompactASCII", MarshalCompactASCII, c.CompactASCII},
			{"MarshalSortedCompactASCII", MarshalSortedCompactASCII, c.SortedCompactASCII},
			{"MarshalSpacedASCII", MarshalSpacedASCII, c.SpacedASCII},
			{"MarshalTranscriptLine", MarshalTranscriptLine, c.TranscriptLine},
		}
		for _, w := range writers {
			got, err := w.write(v)
			if err != nil {
				t.Fatalf("%s(%s): %v", w.name, c.Input, err)
			}
			if want := pythonNumberText.Replace(w.want); string(got) != want {
				t.Errorf("%s(%s)\n got: %q\nwant: %q", w.name, c.Input, got, want)
			}
		}
	}
}

func TestNumbersKeepTheirText(t *testing.T) {
	v, err := Decode([]byte(`{"a": 1.50, "b": 1e5, "c": -0, "d": 12345678901234567890123}`))
	if err != nil {
		t.Fatal(err)
	}
	got, err := MarshalCompactASCII(v)
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"a":1.50,"b":1e5,"c":-0,"d":12345678901234567890123}`; string(got) != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestDecodeRefuses(t *testing.T) {
	for _, doc := range []string{
		`NaN`, `[Infinity]`, `{"a": -Infinity}`,
		"\xef\xbb\xbf{}", `{"a": 1} x`, "\"\x01\"", "\"\xff\"", `{"a": }`, `[1,]`, `01`,
	} {
		if _, err := Decode([]byte(doc)); err == nil {
			t.Errorf("Decode(%q) succeeded", doc)
		}
	}
}

func TestDecodeKeepsKeyOrderAndTakesTheLastDuplicate(t *testing.T) {
	o, err := DecodeObject([]byte(`{"z": 1, "a": 2, "z": 3}`))
	if err != nil {
		t.Fatal(err)
	}
	if keys := strings.Join(o.Keys(), ","); keys != "z,a" {
		t.Fatalf("keys %s", keys)
	}
	if v, _ := o.Get("z"); v != json.Number("3") {
		t.Fatalf("z = %v", v)
	}
}

func TestLoneSurrogatesRoundTrip(t *testing.T) {
	line := `{"text":"\ud800 \udfff"}`
	v, err := Decode([]byte(line))
	if err != nil {
		t.Fatal(err)
	}
	for _, w := range []func(any) ([]byte, error){MarshalCompactASCII, MarshalTranscriptLine} {
		got, err := w(v)
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != line {
			t.Errorf("got %s, want %s", got, line)
		}
	}
}

func TestEqual(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{`1`, `1.0`, true},
		{`{"a":1,"b":2}`, `{"b":2,"a":1}`, true},
		{`true`, `1`, false},
		{`[1,2]`, `[2,1]`, false},
		{`null`, `null`, true},
		{`"x"`, `"y"`, false},
	}
	for _, c := range cases {
		a, _ := Decode([]byte(c.a))
		b, _ := Decode([]byte(c.b))
		if got := Equal(a, b); got != c.want {
			t.Errorf("Equal(%s, %s) = %v", c.a, c.b, got)
		}
	}
}

type strictDoc struct {
	Name     string   `json:"name"`
	Count    int      `json:"count"`
	Optional *string  `json:"optional,omitempty"`
	Nullable *string  `json:"nullable"`
	Items    []string `json:"items"`
}

func TestDecodeStrict(t *testing.T) {
	ok := []string{
		`{"name": "a", "count": 1, "nullable": null, "items": []}`,
		`{"name": "a", "count": 1, "optional": "x", "nullable": "y", "items": ["z"]}`,
	}
	for _, doc := range ok {
		var d strictDoc
		if err := DecodeStrict([]byte(doc), &d); err != nil {
			t.Errorf("DecodeStrict(%s): %v", doc, err)
		}
	}
	bad := []string{
		`{"name": "a", "count": 1, "nullable": null}`,                                // missing key
		`{"name": "a", "count": 1, "nullable": null, "items": [], "extra": 1}`,       // unknown key
		`{"name": "a", "name": "b", "count": 1, "nullable": null, "items": []}`,      // duplicate
		`{"name": "a", "count": "1", "nullable": null, "items": []}`,                 // wrong type
		`{"name": "a", "count": 1, "optional": null, "nullable": null, "items": []}`, // optional null
		`{"name": null, "count": 1, "nullable": null, "items": []}`,                  // null non-pointer
		`{"name": "a", "count": 1, "nullable": null, "items": null}`,                 // null list
	}
	for _, doc := range bad {
		var d strictDoc
		if err := DecodeStrict([]byte(doc), &d); err == nil {
			t.Errorf("DecodeStrict(%s) succeeded", doc)
		}
	}
}
