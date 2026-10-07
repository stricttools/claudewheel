package appconfig

import (
	"slices"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Options is options.json: each segment's option lists, keyed by segment key.
type Options map[string]OptionSegment

// OptionSegment is one segment's entry in options.json. Values holds the
// offered options (for discovery-backed segments, what discovery has
// accumulated); Pinned holds the options the user added, which are always
// offered. Metadata, keyed by option value, and Discovery are absent when the
// segment has none.
type OptionSegment struct {
	Values    []string                  `json:"values"`
	Pinned    []string                  `json:"pinned"`
	Metadata  *map[string]ValueMetadata `json:"metadata,omitempty"`
	Discovery *Discovery                `json:"discovery,omitempty"`
}

// ValueMetadata describes one option value. CreatedAt is a model's release
// date as the models endpoint reports it; ModelID is the model id a launch
// passes when it differs from the option value.
type ValueMetadata struct {
	ModelID   *string `json:"model_id,omitempty"`
	CreatedAt *string `json:"created_at,omitempty"`
}

// Discovery is a segment's discovery declaration: Type names the discovery
// and the other fields are the ones that discovery reads.
type Discovery struct {
	Type       string    `json:"type"`
	BaseDir    *string   `json:"base_dir,omitempty"`
	Path       *string   `json:"path,omitempty"`
	Count      *int      `json:"count,omitempty"`
	Parents    *[]string `json:"parents,omitempty"`
	StateField *string   `json:"state_field,omitempty"`
	Field      *string   `json:"field,omitempty"`
}

// MetadataFor returns the metadata of value, and whether there is any.
func (o OptionSegment) MetadataFor(value string) (ValueMetadata, bool) {
	if o.Metadata == nil {
		return ValueMetadata{}, false
	}
	m, ok := (*o.Metadata)[value]
	return m, ok
}

// ReadOptions reads options.json fresh from disk.
func ReadOptions(ws workspace.Workspace) (Options, error) {
	return readOwned[Options](ws.OptionsFile(), planOptions)
}

// WriteDefaultOptions replaces options.json with DefaultOptions (what
// `reset-options` does).
func WriteDefaultOptions(fx *effects.FX, ws workspace.Workspace) error {
	return writeJSON(fx, ws.OptionsFile(), DefaultOptions())
}

// updateOptions reads options.json fresh, applies change, and writes the file
// when change reports a change. It returns the options as they now are.
func updateOptions(fx *effects.FX, ws workspace.Workspace, change func(Options) bool) (Options, error) {
	opts, err := ReadOptions(ws)
	if err != nil {
		return nil, err
	}
	if !change(opts) {
		return opts, nil
	}
	return opts, writeJSON(fx, ws.OptionsFile(), opts)
}

// segmentOrEmpty returns the entry of key, or an empty one.
func segmentOrEmpty(opts Options, key string) OptionSegment {
	seg, ok := opts[key]
	if !ok {
		return OptionSegment{Values: []string{}, Pinned: []string{}}
	}
	return seg
}

// AddPinned adds value to the pinned list of segment key, creating the
// segment's entry when absent. The file is written only when value was not
// pinned yet.
func AddPinned(fx *effects.FX, ws workspace.Workspace, key, value string) (Options, error) {
	return updateOptions(fx, ws, func(opts Options) bool {
		seg := segmentOrEmpty(opts, key)
		if slices.Contains(seg.Pinned, value) {
			return false
		}
		seg.Pinned = append(seg.Pinned, value)
		opts[key] = seg
		return true
	})
}

// RecordDiscovered appends the discovered values to segment key's values
// list and merges their metadata field by field. Nothing is ever removed: an
// option stays once its source stops listing it. A value already listed keeps
// its position. The file is written only when something changed.
func RecordDiscovered(fx *effects.FX, ws workspace.Workspace, key string, values []string, metadata map[string]ValueMetadata) (Options, error) {
	return updateOptions(fx, ws, func(opts Options) bool {
		seg := segmentOrEmpty(opts, key)
		changed := false
		for _, v := range values {
			if !slices.Contains(seg.Values, v) {
				seg.Values = append(seg.Values, v)
				changed = true
			}
		}
		if len(metadata) > 0 {
			if seg.Metadata == nil {
				seg.Metadata = &map[string]ValueMetadata{}
			}
			meta := *seg.Metadata
			for value, fields := range metadata {
				entry := meta[value]
				if mergeField(&entry.ModelID, fields.ModelID) {
					changed = true
				}
				if mergeField(&entry.CreatedAt, fields.CreatedAt) {
					changed = true
				}
				meta[value] = entry
			}
		}
		opts[key] = seg
		return changed
	})
}

// mergeField sets *dst to update when update is set and differs, reporting
// whether it did.
func mergeField(dst **string, update *string) bool {
	if update == nil || (*dst != nil && **dst == *update) {
		return false
	}
	v := *update
	*dst = &v
	return true
}

// RenameOptionValue replaces oldValue with newValue in segment key's values
// and pinned lists, in place, and moves oldValue's metadata to newValue. The
// file is written only when something changed; a segment with no entry is
// left alone.
func RenameOptionValue(fx *effects.FX, ws workspace.Workspace, key, oldValue, newValue string) (Options, error) {
	return updateOptions(fx, ws, func(opts Options) bool {
		seg, ok := opts[key]
		if !ok {
			return false
		}
		changed := false
		if i := slices.Index(seg.Values, oldValue); i >= 0 {
			seg.Values[i] = newValue
			changed = true
		}
		if i := slices.Index(seg.Pinned, oldValue); i >= 0 {
			seg.Pinned[i] = newValue
			changed = true
		}
		if seg.Metadata != nil {
			meta := *seg.Metadata
			if m, ok := meta[oldValue]; ok {
				delete(meta, oldValue)
				meta[newValue] = m
				changed = true
			}
		}
		opts[key] = seg
		return changed
	})
}

// RemoveOptionValue removes the first occurrence of name from segment key's
// values and pinned lists, and its metadata. The file is written only when
// something was removed; a segment with no entry is left alone.
func RemoveOptionValue(fx *effects.FX, ws workspace.Workspace, key, name string) (Options, error) {
	return updateOptions(fx, ws, func(opts Options) bool {
		seg, ok := opts[key]
		if !ok {
			return false
		}
		found := false
		if i := slices.Index(seg.Values, name); i >= 0 {
			seg.Values = slices.Delete(seg.Values, i, i+1)
			found = true
		}
		if i := slices.Index(seg.Pinned, name); i >= 0 {
			seg.Pinned = slices.Delete(seg.Pinned, i, i+1)
			found = true
		}
		if seg.Metadata != nil {
			meta := *seg.Metadata
			if _, ok := meta[name]; ok {
				delete(meta, name)
				found = true
			}
		}
		opts[key] = seg
		return found
	})
}

// AddOption pins value for segment key in options.json and refreshes the
// store's options from the file.
func (s *Store) AddOption(fx *effects.FX, key, value string) error {
	opts, err := AddPinned(fx, s.ws, key, value)
	if err != nil {
		return err
	}
	s.Options = opts
	return nil
}

// RecordDiscoveredModels records discovered model ids and their metadata in
// options.json (see RecordDiscovered) and refreshes the store's options. An
// empty discovery changes nothing and reads nothing.
func (s *Store) RecordDiscoveredModels(fx *effects.FX, values []string, metadata map[string]ValueMetadata) error {
	if len(values) == 0 && len(metadata) == 0 {
		return nil
	}
	opts, err := RecordDiscovered(fx, s.ws, SegmentKeyModel, values, metadata)
	if err != nil {
		return err
	}
	s.Options = opts
	return nil
}
