// Package jsonfile reads and writes JSON the way claudewheel's Python
// implementation did.
//
// Files owned by Claude Code and shared-settings.json are held as ordered
// trees (Value), keeping key order, number text, and unknown keys. Files
// claudewheel owns are decoded strictly into structs (DecodeStrict). The
// Marshal functions reproduce the byte layouts of Python's json.dumps calls.
//
// A Value is one of: nil (JSON null), bool, json.Number (the number's text as
// written), string, []Value, or *Object. A string holding a lone UTF-16
// surrogate (decoded from a \uXXXX escape with no partner, which Python keeps
// as a lone surrogate character) stores it as the three-byte generalized
// UTF-8 sequence for that code unit; every writer escapes it back as \uXXXX.
package jsonfile

import (
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"strconv"
)

// Value is one decoded JSON value: nil, bool, json.Number, string, []Value,
// or *Object.
type Value = any

// Object is a JSON object that keeps its keys in insertion order. Its zero
// value is an empty object ready to use.
type Object struct {
	keys   []string
	values []Value
	index  map[string]int
}

// NewObject returns an empty object.
func NewObject() *Object {
	return &Object{}
}

// Get returns the value stored under key, and whether the key is present.
func (o *Object) Get(key string) (Value, bool) {
	i, ok := o.index[key]
	if !ok {
		return nil, false
	}
	return o.values[i], true
}

// Set stores v under key: in place when the key is present, appended after
// the last key otherwise (Python's dict assignment).
func (o *Object) Set(key string, v Value) {
	if i, ok := o.index[key]; ok {
		o.values[i] = v
		return
	}
	if o.index == nil {
		o.index = map[string]int{}
	}
	o.index[key] = len(o.keys)
	o.keys = append(o.keys, key)
	o.values = append(o.values, v)
}

// Delete removes key, reporting whether it was present.
func (o *Object) Delete(key string) bool {
	i, ok := o.index[key]
	if !ok {
		return false
	}
	o.keys = append(o.keys[:i], o.keys[i+1:]...)
	o.values = append(o.values[:i], o.values[i+1:]...)
	delete(o.index, key)
	for j := i; j < len(o.keys); j++ {
		o.index[o.keys[j]] = j
	}
	return true
}

// Keys returns the keys in order, as a new slice.
func (o *Object) Keys() []string {
	return append([]string(nil), o.keys...)
}

// Len returns the number of keys.
func (o *Object) Len() int {
	return len(o.keys)
}

// StringArray returns items as a JSON array tree value.
func StringArray(items []string) []Value {
	out := make([]Value, len(items))
	for i, s := range items {
		out[i] = s
	}
	return out
}

// MarshalJSON writes the object in compact form, so an *Object can sit inside
// a struct handed to encoding/json or to the Marshal functions of this package.
func (o *Object) MarshalJSON() ([]byte, error) {
	return MarshalCompactASCII(o)
}

// UnmarshalJSON replaces the object's contents with the decoded JSON object,
// in its key order.
func (o *Object) UnmarshalJSON(data []byte) error {
	decoded, err := DecodeObject(data)
	if err != nil {
		return err
	}
	*o = *decoded
	return nil
}

// Normalize turns any Go value into a Value tree. Tree values (nil, bool,
// json.Number, string, []Value, *Object) are kept as they are, recursively;
// any other value is marshaled with encoding/json and decoded back, so a
// struct's fields keep their declaration order. A Go float is written in
// encoding/json's shortest form, which has the significant digits of
// Python's repr but not always its spelling (an integral value loses its
// ".0"); state.json's cache fetch times are such floats. DecodeStrict reads
// either spelling into a float64 field.
func Normalize(v any) (Value, error) {
	switch t := v.(type) {
	case nil, bool, string:
		return t, nil
	case json.Number:
		if !validNumber(string(t)) {
			return nil, fmt.Errorf("invalid JSON number %q", string(t))
		}
		return t, nil
	case []Value:
		out := make([]Value, len(t))
		for i, item := range t {
			n, err := Normalize(item)
			if err != nil {
				return nil, err
			}
			out[i] = n
		}
		return out, nil
	case *Object:
		if t == nil {
			return nil, fmt.Errorf("nil *Object in a JSON tree")
		}
		out := NewObject()
		for i, key := range t.keys {
			n, err := Normalize(t.values[i])
			if err != nil {
				return nil, err
			}
			out.Set(key, n)
		}
		return out, nil
	}
	data, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return Decode(data)
}

func mustNormalize(v any) Value {
	n, err := Normalize(v)
	if err != nil {
		panic(fmt.Sprintf("jsonfile: value is not representable as JSON: %v", err))
	}
	return n
}

// Clone returns a deep copy of v. A value that is not a tree is normalized
// first; one encoding/json cannot marshal panics.
func Clone(v Value) Value {
	return mustNormalize(v)
}

// Equal reports whether a and b are equal the way Python compares the decoded
// values: arrays element by element in order; objects by their key sets and
// values, ignoring key order (Python's dict equality); numbers by numeric
// value, so 1, 1.0, and 1e0 are equal. Unlike Python, true is not equal to 1.
// A value that is not a tree is normalized first; one encoding/json cannot
// marshal panics.
func Equal(a, b Value) bool {
	return equalTrees(mustNormalize(a), mustNormalize(b))
}

func equalTrees(a, b Value) bool {
	switch x := a.(type) {
	case nil:
		return b == nil
	case bool:
		y, ok := b.(bool)
		return ok && x == y
	case string:
		y, ok := b.(string)
		return ok && x == y
	case json.Number:
		y, ok := b.(json.Number)
		return ok && numbersEqual(string(x), string(y))
	case []Value:
		y, ok := b.([]Value)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !equalTrees(x[i], y[i]) {
				return false
			}
		}
		return true
	case *Object:
		y, ok := b.(*Object)
		if !ok || x.Len() != y.Len() {
			return false
		}
		for i, key := range x.keys {
			other, present := y.Get(key)
			if !present || !equalTrees(x.values[i], other) {
				return false
			}
		}
		return true
	}
	panic(fmt.Sprintf("jsonfile: %T is not a JSON tree value", a))
}

func isIntegerText(s string) bool {
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '.', 'e', 'E':
			return false
		}
	}
	return true
}

// numbersEqual compares two valid JSON number texts as Python compares the
// int and float values json.loads makes of them: int with int exactly, float
// with float as float64, and int with float exactly.
func numbersEqual(a, b string) bool {
	aInt, bInt := isIntegerText(a), isIntegerText(b)
	if aInt && bInt {
		x, okX := new(big.Int).SetString(a, 10)
		y, okY := new(big.Int).SetString(b, 10)
		if !okX || !okY {
			panic(fmt.Sprintf("jsonfile: invalid JSON integers %q, %q", a, b))
		}
		return x.Cmp(y) == 0
	}
	if !aInt && !bInt {
		return parseFloat(a) == parseFloat(b)
	}
	intText, floatText := a, b
	if !aInt {
		intText, floatText = b, a
	}
	i, ok := new(big.Int).SetString(intText, 10)
	if !ok {
		panic(fmt.Sprintf("jsonfile: invalid JSON integer %q", intText))
	}
	f := parseFloat(floatText)
	if math.IsInf(f, 0) {
		return false
	}
	return new(big.Float).SetInt(i).Cmp(new(big.Float).SetFloat64(f)) == 0
}

// parseFloat converts float text as Python's float() does: an out-of-range
// value becomes an infinity.
func parseFloat(s string) float64 {
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		if ne, ok := err.(*strconv.NumError); ok && ne.Err == strconv.ErrRange {
			return f
		}
		panic(fmt.Sprintf("jsonfile: invalid JSON number %q", s))
	}
	return f
}

// validNumber reports whether s matches the JSON number grammar.
func validNumber(s string) bool {
	i := 0
	if i < len(s) && s[i] == '-' {
		i++
	}
	if i >= len(s) {
		return false
	}
	if s[i] == '0' {
		i++
	} else if s[i] >= '1' && s[i] <= '9' {
		for i < len(s) && isDigit(s[i]) {
			i++
		}
	} else {
		return false
	}
	if i < len(s) && s[i] == '.' {
		i++
		start := i
		for i < len(s) && isDigit(s[i]) {
			i++
		}
		if i == start {
			return false
		}
	}
	if i < len(s) && (s[i] == 'e' || s[i] == 'E') {
		i++
		if i < len(s) && (s[i] == '+' || s[i] == '-') {
			i++
		}
		start := i
		for i < len(s) && isDigit(s[i]) {
			i++
		}
		if i == start {
			return false
		}
	}
	return i == len(s)
}

func isDigit(b byte) bool {
	return b >= '0' && b <= '9'
}
