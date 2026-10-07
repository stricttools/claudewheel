package jsonfile

import (
	"bytes"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
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
	if err := checkNulls(input, reflect.TypeOf(dst).Elem(), "top level"); err != nil {
		return err
	}
	return compareKeys(input, output, "top level")
}

var unmarshalerType = reflect.TypeOf((*json.Unmarshaler)(nil)).Elem()

// checkNulls refuses a null decoded into a Go type that is neither a pointer
// nor an interface: encoding/json leaves such a field at its zero value, and
// a nil slice or map is written back as null too, so compareKeys cannot see
// it. Types that decode themselves (such as *Object) are not looked into.
func checkNulls(input Value, t reflect.Type, path string) error {
	if input == nil {
		if t.Kind() != reflect.Pointer && t.Kind() != reflect.Interface {
			return fmt.Errorf("%s: null is not allowed here", path)
		}
		return nil
	}
	if t.Implements(unmarshalerType) || reflect.PointerTo(t).Implements(unmarshalerType) {
		return nil
	}
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
		if t.Implements(unmarshalerType) || reflect.PointerTo(t).Implements(unmarshalerType) {
			return nil
		}
	}
	switch in := input.(type) {
	case *Object:
		switch t.Kind() {
		case reflect.Map:
			for _, key := range in.Keys() {
				item, _ := in.Get(key)
				if err := checkNulls(item, t.Elem(), path+"."+key); err != nil {
					return err
				}
			}
		case reflect.Struct:
			fields := jsonFields(t)
			for _, key := range in.Keys() {
				ft, ok := fields[key]
				if !ok {
					continue
				}
				item, _ := in.Get(key)
				if err := checkNulls(item, ft, path+"."+key); err != nil {
					return err
				}
			}
		}
	case []Value:
		if t.Kind() == reflect.Slice || t.Kind() == reflect.Array {
			for i, item := range in {
				if err := checkNulls(item, t.Elem(), fmt.Sprintf("%s[%d]", path, i)); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// jsonFields maps each JSON key of struct type t to its field's type, as
// encoding/json names them (the tag's name, else the field name), with the
// fields of embedded structs promoted.
func jsonFields(t reflect.Type) map[string]reflect.Type {
	fields := map[string]reflect.Type{}
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		tag := f.Tag.Get("json")
		if tag == "-" {
			continue
		}
		name, _, _ := strings.Cut(tag, ",")
		if f.Anonymous && name == "" {
			et := f.Type
			if et.Kind() == reflect.Pointer {
				et = et.Elem()
			}
			if et.Kind() == reflect.Struct {
				for k, v := range jsonFields(et) {
					if _, taken := fields[k]; !taken {
						fields[k] = v
					}
				}
				continue
			}
		}
		if !f.IsExported() {
			continue
		}
		if name == "" {
			name = f.Name
		}
		fields[name] = f.Type
	}
	return fields
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
