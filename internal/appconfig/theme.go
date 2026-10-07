package appconfig

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Theme names with a meaning of their own. Any other name selects the custom
// theme file themes/<name>.json.
const (
	// ThemeAuto follows the terminal's background color.
	ThemeAuto  = "auto"
	ThemeDark  = "dark"
	ThemeLight = "light"
)

// Theme is one themes/<name>.json file: hex colors ("#rrggbb") and literal
// strings for every part of the interface. Turning it into terminal escape
// sequences is the interface's job.
type Theme struct {
	Name     string                  `json:"name"`
	Global   GlobalTheme             `json:"global"`
	Segments map[string]SegmentTheme `json:"segments"`
	Search   SearchTheme             `json:"search"`
	Overflow OverflowTheme           `json:"overflow"`
	Forms    FormsTheme              `json:"forms"`
	Sessions SessionsTheme           `json:"sessions"`
}

// GlobalTheme holds the colors shared by the whole bar. Bg is null when the
// terminal's own background is kept.
type GlobalTheme struct {
	Bg             *string `json:"bg"`
	Fg             string  `json:"fg"`
	LabelFg        string  `json:"label_fg"`
	SeparatorFg    string  `json:"separator_fg"`
	SeparatorChar  string  `json:"separator_char"`
	EmptyValueFg   string  `json:"empty_value_fg"`
	EmptyValueText string  `json:"empty_value_text"`
}

// SegmentTheme holds one segment's colors, keyed in Theme.Segments by the
// segment key.
type SegmentTheme struct {
	ValueFg       string  `json:"value_fg"`
	FocusBg       string  `json:"focus_bg"`
	FocusFg       string  `json:"focus_fg"`
	OptionFg      string  `json:"option_fg"`
	UnavailableFg *string `json:"unavailable_fg,omitempty"`
}

// SearchTheme holds the colors of the search line.
type SearchTheme struct {
	CursorFg  string `json:"cursor_fg"`
	MatchFg   string `json:"match_fg"`
	NoMatchFg string `json:"no_match_fg"`
}

// OverflowTheme holds the edge arrows' and the minimap's colors.
type OverflowTheme struct {
	ArrowFg          string `json:"arrow_fg"`
	MinimapFg        string `json:"minimap_fg"`
	MinimapFocusedBg string `json:"minimap_focused_bg"`
	MinimapChar      string `json:"minimap_char"`
}

// FormsTheme holds the form pages' colors.
type FormsTheme struct {
	TitleFg    string `json:"title_fg"`
	FocusBg    string `json:"focus_bg"`
	FocusFg    string `json:"focus_fg"`
	FieldFg    string `json:"field_fg"`
	ErrorFg    string `json:"error_fg"`
	HintFg     string `json:"hint_fg"`
	CursorFg   string `json:"cursor_fg"`
	ReadonlyFg string `json:"readonly_fg"`
}

// SessionsTheme holds the sessions table's colors, including one color per
// session lifecycle state (see StateFg).
type SessionsTheme struct {
	FrameFg           string `json:"frame_fg"`
	HeaderFg          string `json:"header_fg"`
	RowFg             string `json:"row_fg"`
	FocusBg           string `json:"focus_bg"`
	FocusFg           string `json:"focus_fg"`
	DetailFg          string `json:"detail_fg"`
	HintFg            string `json:"hint_fg"`
	MessageFg         string `json:"message_fg"`
	StateWorkingFg    string `json:"state_working_fg"`
	StateShellFg      string `json:"state_shell_fg"`
	StateIdleFg       string `json:"state_idle_fg"`
	StateWaitingFg    string `json:"state_waiting_fg"`
	StateRunningFg    string `json:"state_running_fg"`
	StateUnverifiedFg string `json:"state_unverified_fg"`
	StateStartingFg   string `json:"state_starting_fg"`
	StateOnHoldFg     string `json:"state_on_hold_fg"`
	StateBlockedFg    string `json:"state_blocked_fg"`
	StateDoneFg       string `json:"state_done_fg"`
	StateCrashedFg    string `json:"state_crashed_fg"`
	StateExitedFg     string `json:"state_exited_fg"`
}

// StateFg returns the color of the session lifecycle state named as the
// lifecycle store writes it ("on-hold", not "on_hold"). A name the theme has
// no color for is an error.
func (s SessionsTheme) StateFg(state string) (string, error) {
	switch state {
	case "working":
		return s.StateWorkingFg, nil
	case "shell":
		return s.StateShellFg, nil
	case "idle":
		return s.StateIdleFg, nil
	case "waiting":
		return s.StateWaitingFg, nil
	case "running":
		return s.StateRunningFg, nil
	case "unverified":
		return s.StateUnverifiedFg, nil
	case "starting":
		return s.StateStartingFg, nil
	case "on-hold":
		return s.StateOnHoldFg, nil
	case "blocked":
		return s.StateBlockedFg, nil
	case "done":
		return s.StateDoneFg, nil
	case "crashed":
		return s.StateCrashedFg, nil
	case "exited":
		return s.StateExitedFg, nil
	}
	return "", fmt.Errorf("the theme has no color for session state %q", state)
}

// ResolveThemeName returns the theme name to load for the configured value
// name. Any value but ThemeAuto is returned as it is. For ThemeAuto it asks
// background for the terminal's background, which reports ThemeLight,
// ThemeDark, or "" when the terminal gave no answer; no answer resolves to
// ThemeDark. An error from background (the terminal could not be opened or
// read) is returned. The caller queries the terminal: this package does no
// terminal I/O.
func ResolveThemeName(name string, background func() (string, error)) (string, error) {
	if name != ThemeAuto {
		return name, nil
	}
	detected, err := background()
	if err != nil {
		return "", fmt.Errorf("detecting the terminal background for the %q theme: %w", ThemeAuto, err)
	}
	switch detected {
	case ThemeLight, ThemeDark:
		return detected, nil
	case "":
		return ThemeDark, nil
	}
	return "", fmt.Errorf("terminal background detection reported %q, not %q, %q, or no answer", detected, ThemeLight, ThemeDark)
}

// DefaultTheme returns the built-in theme a theme file named name is
// completed from: the light theme for ThemeLight, the dark theme for every
// other name.
func DefaultTheme(name string) Theme {
	if name == ThemeLight {
		return defaultLightTheme()
	}
	return defaultDarkTheme()
}

// ThemeFile returns the path of the theme file named name. A name that is
// empty, ThemeAuto, or not a plain file name is an error.
func ThemeFile(ws workspace.Workspace, name string) (string, error) {
	if name == "" || name == ThemeAuto || name == "." || name == ".." || strings.ContainsRune(name, filepath.Separator) {
		return "", fmt.Errorf("%q is not a theme name: a theme is a file themes/<name>.json, with %q resolved first", name, ThemeAuto)
	}
	return filepath.Join(ws.ThemesDir(), name+".json"), nil
}

// LoadTheme reads the theme file named name (resolve ThemeAuto with
// ResolveThemeName first). A key the file lacks is filled from DefaultTheme
// for this read only, so a partial theme file still yields a complete theme;
// the file is not changed. A missing or invalid file is an error.
func LoadTheme(ws workspace.Workspace, name string) (Theme, error) {
	var theme Theme
	path, err := ThemeFile(ws, name)
	if err != nil {
		return theme, err
	}
	var data []byte
	if name == ThemeDark || name == ThemeLight {
		data, err = readOwnedFile(path)
	} else {
		// A workspace setup creates only the built-in theme files, so a
		// missing custom one is not repaired by `claudewheel launch`.
		data, err = os.ReadFile(path)
		if os.IsNotExist(err) {
			err = fmt.Errorf("config.json selects the theme %q, and its file %s does not exist: create it, or select another theme in config.json: %w", name, path, err)
		}
	}
	if err != nil {
		return theme, err
	}
	tree, err := jsonfile.Decode(data)
	if err != nil {
		return theme, fmt.Errorf("%s: %w", path, err)
	}
	if obj, ok := tree.(*jsonfile.Object); ok {
		mergeMissing(obj, defaultThemeTree(name))
	}
	merged, err := jsonfile.MarshalCompactASCII(tree)
	if err != nil {
		return theme, fmt.Errorf("%s: %w", path, err)
	}
	if err := jsonfile.DecodeStrict(merged, &theme); err != nil {
		return theme, fmt.Errorf("%s: %w", path, err)
	}
	return theme, nil
}

// defaultThemeTree returns DefaultTheme(name) as an ordered tree.
func defaultThemeTree(name string) *jsonfile.Object {
	tree, err := jsonfile.Normalize(DefaultTheme(name))
	if err != nil {
		panic(fmt.Sprintf("appconfig: the default theme is not representable as JSON: %v", err))
	}
	return tree.(*jsonfile.Object)
}

// mergeMissing adds to target, recursively, every key of defaults it lacks,
// as a copy, and reports the dotted paths it added. A key present in target
// is never replaced, and is descended into only when both sides hold objects.
func mergeMissing(target, defaults *jsonfile.Object) []string {
	var added []string
	for _, key := range defaults.Keys() {
		def, _ := defaults.Get(key)
		have, present := target.Get(key)
		if !present {
			target.Set(key, jsonfile.Clone(def))
			added = append(added, key)
			continue
		}
		haveObj, ok1 := have.(*jsonfile.Object)
		defObj, ok2 := def.(*jsonfile.Object)
		if ok1 && ok2 {
			for _, sub := range mergeMissing(haveObj, defObj) {
				added = append(added, key+"."+sub)
			}
		}
	}
	return added
}

// fileExists reports whether path exists, without following a final
// symbolic link. An error other than absence is returned.
func fileExists(path string) (bool, error) {
	_, err := os.Lstat(path)
	if err == nil {
		return true, nil
	}
	if os.IsNotExist(err) {
		return false, nil
	}
	return false, err
}

func defaultDarkTheme() Theme {
	return Theme{
		Name: ThemeDark,
		Global: GlobalTheme{
			Bg:             nil,
			Fg:             "#e0e0e0",
			LabelFg:        "#888888",
			SeparatorFg:    "#444444",
			SeparatorChar:  " | ",
			EmptyValueFg:   "#555555",
			EmptyValueText: "---",
		},
		Segments: map[string]SegmentTheme{
			"profile":     {ValueFg: "#7ec8e3", FocusBg: "#2a2a4e", FocusFg: "#ffffff", OptionFg: "#5a8ea3", UnavailableFg: strPtr("#555555")},
			"github":      {ValueFg: "#a8d8a8", FocusBg: "#2a4e2a", FocusFg: "#ffffff", OptionFg: "#6a9a6a"},
			"version":     {ValueFg: "#e8c87e", FocusBg: "#4e4a2a", FocusFg: "#ffffff", OptionFg: "#a8984e", UnavailableFg: strPtr("#555555")},
			"model":       {ValueFg: "#b8e8b8", FocusBg: "#2a4e3a", FocusFg: "#ffffff", OptionFg: "#6aaa7a"},
			"directory":   {ValueFg: "#c8a8e8", FocusBg: "#3a2a4e", FocusFg: "#ffffff", OptionFg: "#8a6aa8"},
			"mcp":         {ValueFg: "#e8a88e", FocusBg: "#4e3a2a", FocusFg: "#ffffff", OptionFg: "#a87a5e"},
			"permissions": {ValueFg: "#e88e8e", FocusBg: "#4e2a2a", FocusFg: "#ffffff", OptionFg: "#a85e5e", UnavailableFg: strPtr("#555555")},
		},
		Search: SearchTheme{
			CursorFg:  "#ffffff",
			MatchFg:   "#ffff00",
			NoMatchFg: "#ff4444",
		},
		Overflow: OverflowTheme{
			ArrowFg:          "#666666",
			MinimapFg:        "#444444",
			MinimapFocusedBg: "#ffffff",
			MinimapChar:      "▪",
		},
		Forms: FormsTheme{
			TitleFg:    "#7ec8e3",
			FocusBg:    "#2a2a4e",
			FocusFg:    "#ffffff",
			FieldFg:    "#e0e0e0",
			ErrorFg:    "#ff6464",
			HintFg:     "#555555",
			CursorFg:   "#ffffff",
			ReadonlyFg: "#888888",
		},
		Sessions: SessionsTheme{
			FrameFg:           "#555555",
			HeaderFg:          "#e0e0e0",
			RowFg:             "#d0d0d0",
			FocusBg:           "#2a2a4e",
			FocusFg:           "#ffffff",
			DetailFg:          "#999999",
			HintFg:            "#555555",
			MessageFg:         "#7ec8e3",
			StateWorkingFg:    "#e8c87e",
			StateShellFg:      "#e8a88e",
			StateIdleFg:       "#a8d8a8",
			StateWaitingFg:    "#e89ae8",
			StateRunningFg:    "#7ec8e3",
			StateUnverifiedFg: "#888888",
			StateStartingFg:   "#999999",
			StateOnHoldFg:     "#8ab4f8",
			StateBlockedFg:    "#6a8ab8",
			StateDoneFg:       "#777777",
			StateCrashedFg:    "#ff6464",
			StateExitedFg:     "#666666",
		},
	}
}

func defaultLightTheme() Theme {
	return Theme{
		Name: ThemeLight,
		Global: GlobalTheme{
			Bg:             nil,
			Fg:             "#1a1a1a",
			LabelFg:        "#666666",
			SeparatorFg:    "#cccccc",
			SeparatorChar:  " | ",
			EmptyValueFg:   "#aaaaaa",
			EmptyValueText: "---",
		},
		Segments: map[string]SegmentTheme{
			"profile":     {ValueFg: "#1a6b8a", FocusBg: "#d0e8f0", FocusFg: "#000000", OptionFg: "#4a8ba3", UnavailableFg: strPtr("#bbbbbb")},
			"github":      {ValueFg: "#2a7a2a", FocusBg: "#d0f0d0", FocusFg: "#000000", OptionFg: "#4a9a4a"},
			"version":     {ValueFg: "#8a7a1a", FocusBg: "#f0e8d0", FocusFg: "#000000", OptionFg: "#a89a4a", UnavailableFg: strPtr("#bbbbbb")},
			"model":       {ValueFg: "#2a6a3a", FocusBg: "#d0f0d8", FocusFg: "#000000", OptionFg: "#4a8a5a"},
			"directory":   {ValueFg: "#6a3a8a", FocusBg: "#e8d0f0", FocusFg: "#000000", OptionFg: "#8a5aa8"},
			"mcp":         {ValueFg: "#8a5a2a", FocusBg: "#f0e0d0", FocusFg: "#000000", OptionFg: "#a87a4a"},
			"permissions": {ValueFg: "#8a2a2a", FocusBg: "#f0d0d0", FocusFg: "#000000", OptionFg: "#a85a5a", UnavailableFg: strPtr("#bbbbbb")},
		},
		Search: SearchTheme{
			CursorFg:  "#000000",
			MatchFg:   "#0066cc",
			NoMatchFg: "#cc0000",
		},
		Overflow: OverflowTheme{
			ArrowFg:          "#999999",
			MinimapFg:        "#cccccc",
			MinimapFocusedBg: "#000000",
			MinimapChar:      "▪",
		},
		Forms: FormsTheme{
			TitleFg:    "#1a6b8a",
			FocusBg:    "#d0e8f0",
			FocusFg:    "#000000",
			FieldFg:    "#1a1a1a",
			ErrorFg:    "#cc0000",
			HintFg:     "#aaaaaa",
			CursorFg:   "#000000",
			ReadonlyFg: "#666666",
		},
		Sessions: SessionsTheme{
			FrameFg:           "#aaaaaa",
			HeaderFg:          "#1a1a1a",
			RowFg:             "#2a2a2a",
			FocusBg:           "#d0e8f0",
			FocusFg:           "#000000",
			DetailFg:          "#666666",
			HintFg:            "#aaaaaa",
			MessageFg:         "#1a6b8a",
			StateWorkingFg:    "#8a6a1a",
			StateShellFg:      "#8a5a2a",
			StateIdleFg:       "#2a7a2a",
			StateWaitingFg:    "#8a2a8a",
			StateRunningFg:    "#1a6b8a",
			StateUnverifiedFg: "#999999",
			StateStartingFg:   "#888888",
			StateOnHoldFg:     "#1a4a9a",
			StateBlockedFg:    "#4a6a9a",
			StateDoneFg:       "#888888",
			StateCrashedFg:    "#cc0000",
			StateExitedFg:     "#999999",
		},
	}
}
