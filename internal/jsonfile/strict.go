package jsonfile

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// DecodeStrict decodes a file claudewheel owns into dst, a non-nil pointer to
// a struct (or another type encoding/json decodes into). It refuses what
// Decode refuses, plus a key appearing twice, a key no field names, a JSON
// type that does not fit its field, and any document that writing dst back
// would not reproduce key for key: a missing key, a null for a field that is
// not a pointer or interface, or an optional key holding the value its
// writer omits.
//
// Fields name their keys with json tags, matched case-sensitively. A key may
// be absent only when its field is optional: a pointer field whose tag has
// the omitempty option (`json:"name,omitempty"`). An absent optional key
// leaves the pointer nil, and every writer omits a nil one, so absent and
// present stay distinct; such a key present with null is refused, since
// writing it back would omit it. A required pointer field (no omitempty)
// accepts null. Fields of type *Object keep their key order and number text.
//
// Lone surrogates in strings decode to U+FFFD in string fields
// (encoding/json's behavior); *Object fields keep them.
func DecodeStrict(data []byte, dst any) error {
	input, err := decodeDocument(data, true)
	if err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	dec.UseNumber()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	written, err := json.Marshal(dst)
	if err != nil {
		return fmt.Errorf("jsonfile: decoded %T cannot be written back: %w", dst, err)
	}
	output, err := Decode(written)
	if err != nil {
		return fmt.Errorf("jsonfile: decoded %T was written back as invalid JSON: %w", dst, err)
	}
	return compareKeys(input, output, "top level")
}

// compareKeys checks that the decoded document input has the keys and nulls
// of output, the same value written back. Scalars are not compared:
// encoding/json has already checked their types, and number text may differ.
func compareKeys(input, output Value, path string) error {
	switch in := input.(type) {
	case nil:
		if output != nil {
			return fmt.Errorf("%s: null is not allowed here", path)
		}
	case *Object:
		out, ok := output.(*Object)
		if !ok {
			return nil
		}
		for _, key := range in.Keys() {
			inItem, _ := in.Get(key)
			outItem, present := out.Get(key)
			if !present {
				return fmt.Errorf("%s: key %q holds %s, which is never written: omit the key instead", path, key, Describe(inItem))
			}
			if err := compareKeys(inItem, outItem, path+"."+key); err != nil {
				return err
			}
		}
		for _, key := range out.Keys() {
			if _, present := in.Get(key); !present {
				return fmt.Errorf("%s: missing key %q", path, key)
			}
		}
	case []Value:
		out, ok := output.([]Value)
		if !ok || len(out) != len(in) {
			return nil
		}
		for i := range in {
			if err := compareKeys(in[i], out[i], fmt.Sprintf("%s[%d]", path, i)); err != nil {
				return err
			}
		}
	}
	return nil
}
