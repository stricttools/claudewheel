package sessionsview

import (
	"encoding/json"
	"os"
	"testing"
)

// pythonCall is one call of a pure TUI function the Python's tests made,
// with its result, recorded by a generator deleted with the Python.
type pythonCall struct {
	Fn     string            `json:"fn"`
	Args   []json.RawMessage `json:"args"`
	Kwargs map[string]any    `json:"kwargs"`
	Result json.RawMessage   `json:"result"`
}

func loadCalls(t *testing.T) []pythonCall {
	t.Helper()
	data, err := os.ReadFile("../testdata/python-tui-calls.json")
	if err != nil {
		t.Fatal(err)
	}
	var calls []pythonCall
	if err := json.Unmarshal(data, &calls); err != nil {
		t.Fatal(err)
	}
	return calls
}

func TestLayoutHelpersMatchThePython(t *testing.T) {
	checked := map[string]int{}
	for i, c := range loadCalls(t) {
		switch c.Fn {
		case "compute_viewport":
			var heights []int
			_ = json.Unmarshal(c.Args[0], &heights)
			focus, window := 0, 0
			if len(c.Args) > 1 {
				_ = json.Unmarshal(c.Args[1], &focus)
				_ = json.Unmarshal(c.Args[2], &window)
			}
			if v, ok := c.Kwargs["focus_idx"]; ok {
				focus = int(v.(float64))
			}
			if v, ok := c.Kwargs["window_height"]; ok {
				window = int(v.(float64))
			}
			var want struct {
				Start, Height, Total int
				Rows                 []struct {
					Index, ScreenTop, SkipTop, Lines, Height int
				}
				HiddenAbove, HiddenBelow int
			}
			var raw map[string]json.RawMessage
			_ = json.Unmarshal(c.Result, &raw)
			_ = json.Unmarshal(raw["start"], &want.Start)
			_ = json.Unmarshal(raw["height"], &want.Height)
			_ = json.Unmarshal(raw["total"], &want.Total)
			_ = json.Unmarshal(raw["hidden_above"], &want.HiddenAbove)
			_ = json.Unmarshal(raw["hidden_below"], &want.HiddenBelow)
			var rows []map[string]int
			_ = json.Unmarshal(raw["rows"], &rows)
			got, err := ComputeViewport(heights, focus, window)
			if err != nil {
				t.Errorf("call %d ComputeViewport(%v, %d, %d): %v", i, heights, focus, window, err)
				continue
			}
			ok := got.Start == want.Start && got.Height == want.Height && got.Total == want.Total &&
				got.HiddenAbove == want.HiddenAbove && got.HiddenBelow == want.HiddenBelow && len(got.Rows) == len(rows)
			for k := 0; ok && k < len(rows); k++ {
				r := got.Rows[k]
				ok = r.Index == rows[k]["index"] && r.ScreenTop == rows[k]["screen_top"] && r.SkipTop == rows[k]["skip_top"] &&
					r.Lines == rows[k]["lines"] && r.Height == rows[k]["height"]
			}
			if !ok {
				t.Errorf("call %d ComputeViewport(%v, %d, %d) = %+v, Python %s", i, heights, focus, window, got, c.Result)
			}
		case "format_uptime":
			var started *int64
			var now int64
			_ = json.Unmarshal(c.Args[0], &started)
			_ = json.Unmarshal(c.Args[1], &now)
			var want string
			_ = json.Unmarshal(c.Result, &want)
			if got := FormatUptime(started, now); got != want {
				t.Errorf("call %d FormatUptime(%s, %d) = %q, Python %q", i, c.Args[0], now, got, want)
			}
		case "format_memory":
			var kib int64
			_ = json.Unmarshal(c.Args[0], &kib)
			var want string
			_ = json.Unmarshal(c.Result, &want)
			if got, err := FormatMemory(kib); err != nil || got != want {
				t.Errorf("call %d FormatMemory(%d) = %q %v, Python %q", i, kib, got, err, want)
			}
		case "tildify":
			var path *string
			var home string
			_ = json.Unmarshal(c.Args[0], &path)
			_ = json.Unmarshal(c.Args[1], &home)
			p := ""
			if path != nil {
				p = *path
			}
			var want string
			_ = json.Unmarshal(c.Result, &want)
			if got := Tildify(p, home); got != want {
				t.Errorf("call %d Tildify(%q, %q) = %q, Python %q", i, p, home, got, want)
			}
		default:
			continue
		}
		checked[c.Fn]++
	}
	for _, fn := range []string{"compute_viewport", "format_uptime", "format_memory", "tildify"} {
		if checked[fn] == 0 {
			t.Errorf("no %s call was checked", fn)
		}
	}
}
