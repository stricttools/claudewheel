package terminal

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

// Scheme is the terminal's color scheme as a query reports it.
type Scheme string

const (
	SchemeDark  Scheme = "dark"
	SchemeLight Scheme = "light"
	// SchemeUnknown: the terminal does not support the query, did not answer
	// in time, or answered something unparseable.
	SchemeUnknown Scheme = ""
)

const (
	// queryFirstTimeout bounds the wait for the first byte of an answer:
	// terminals answer these queries quickly or not at all.
	queryFirstTimeout = 500 * time.Millisecond
	// queryChunkTimeout ends the answer once no more bytes follow.
	queryChunkTimeout = 100 * time.Millisecond

	osc11Query    = "\x1b]11;?\x07"
	mode2031Query = "\x1b[?996n"
	// da1Query is a sentinel every terminal answers, so a terminal that
	// ignores the real query answers only this.
	da1Query = "\x1b[c"
)

// DetectBackground asks the terminal for its background color (OSC 11, with
// a DA1 sentinel) and classifies it as light or dark by luminance. Call it
// before the TUI enters cbreak mode, so typed keys cannot race the answer.
// Terminals known not to support the query (TERM dumb, Eterm, screen*) give
// SchemeUnknown without being asked.
func DetectBackground(ctx context.Context) (Scheme, error) {
	resp, err := queryTerminal(ctx, osc11Query+da1Query)
	if err != nil || resp == nil {
		return SchemeUnknown, err
	}
	return parseOSC11(resp), nil
}

// DetectMode2031 asks whether the terminal supports mode 2031 color-scheme
// notifications (CSI ?996n, with a DA1 sentinel), and returns the scheme it
// reports, or SchemeUnknown when it does not support them. Call it before
// the TUI enters cbreak mode.
func DetectMode2031(ctx context.Context) (Scheme, error) {
	resp, err := queryTerminal(ctx, mode2031Query+da1Query)
	if err != nil || resp == nil {
		return SchemeUnknown, err
	}
	return parseMode2031(resp), nil
}

// queryTerminal writes query to /dev/tty in cbreak mode and collects the
// answer. It returns nil without asking on terminals known not to support
// these queries, and nil when nothing answers in time.
func queryTerminal(ctx context.Context, query string) (resp []byte, err error) {
	termEnv := os.Getenv("TERM")
	if termEnv == "dumb" || termEnv == "Eterm" || strings.HasPrefix(termEnv, "screen") {
		return nil, nil
	}
	fd, err := unix.Open(ttyPath, unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", ttyPath, err)
	}
	defer unix.Close(fd)
	saved, err := setCbreak(fd)
	if err != nil {
		return nil, err
	}
	defer func() {
		if restoreErr := unix.IoctlSetTermios(fd, unix.TCSETSF, saved); restoreErr != nil {
			err = errors.Join(err, fmt.Errorf("restore the terminal attributes: %w", restoreErr))
		}
	}()
	if err := writeAll(fd, []byte(query)); err != nil {
		return nil, fmt.Errorf("write a terminal query: %w", err)
	}
	ready, err := pollReadable(ctx, fd, queryFirstTimeout)
	if err != nil || !ready {
		return nil, err
	}
	buf := make([]byte, 256)
	for {
		ready, err := pollReadable(ctx, fd, queryChunkTimeout)
		if err != nil {
			return nil, err
		}
		if !ready {
			break
		}
		n, err := unix.Read(fd, buf)
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("read a terminal answer: %w", err)
		}
		if n == 0 {
			break
		}
		resp = append(resp, buf[:n]...)
	}
	if len(resp) == 0 {
		return nil, nil
	}
	return resp, nil
}

// pollReadable waits up to timeout for fd to become readable. A done ctx
// returns its cause once the current poll ends.
func pollReadable(ctx context.Context, fd int, timeout time.Duration) (bool, error) {
	deadline := time.Now().Add(timeout)
	for {
		if ctx.Err() != nil {
			return false, cancelled(ctx)
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return false, nil
		}
		fds := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
		n, err := unix.Poll(fds, int((remaining+time.Millisecond-1)/time.Millisecond))
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			return false, fmt.Errorf("wait for a terminal answer: %w", err)
		}
		if n > 0 {
			return true, nil
		}
	}
}

// parseMode2031 finds CSI ?997;<n>n in resp: 1 is dark, 2 is light.
func parseMode2031(resp []byte) Scheme {
	marker := []byte("\x1b[?997;")
	idx := bytes.Index(resp, marker)
	if idx < 0 {
		return SchemeUnknown
	}
	rest := resp[idx+len(marker):]
	end := bytes.IndexByte(rest, 'n')
	if end < 0 {
		return SchemeUnknown
	}
	mode, err := strconv.Atoi(string(rest[:end]))
	if err != nil {
		return SchemeUnknown
	}
	switch mode {
	case 1:
		return SchemeDark
	case 2:
		return SchemeLight
	}
	return SchemeUnknown
}

// parseOSC11 finds the OSC 11 answer (ESC ] or the 8-bit 0x9D introducer,
// ending at BEL, ST, or the 8-bit 0x9C) in resp and classifies its
// rgb:RRRR/GGGG/BBBB color. An answer that starts with the DA1 reply means
// the terminal ignored the OSC 11 query.
func parseOSC11(resp []byte) Scheme {
	if bytes.HasPrefix(resp, []byte("\x1b[?")) {
		return SchemeUnknown
	}
	start := -1
	for i, b := range resp {
		if b == 0x1b && i+1 < len(resp) && resp[i+1] == ']' {
			start = i + 2
			break
		}
		if b == 0x9d {
			start = i + 1
			break
		}
	}
	if start < 0 {
		return SchemeUnknown
	}
	end := start
	for end < len(resp) {
		b := resp[end]
		if b == 0x07 || b == 0x9c || (b == 0x1b && end+1 < len(resp) && resp[end+1] == '\\') {
			break
		}
		end++
	}
	payload, ok := strings.CutPrefix(string(resp[start:end]), "11;rgb:")
	if !ok {
		return SchemeUnknown
	}
	return classifyRGB(payload)
}

// classifyRGB parses an R/G/B color of 1 to 4 hex digits per channel and
// returns light when its perceived luminance (sRGB linearized, ITU-R BT.709
// weights) is above one half, dark otherwise.
func classifyRGB(rgb string) Scheme {
	parts := strings.Split(rgb, "/")
	if len(parts) != 3 {
		return SchemeUnknown
	}
	var channels [3]float64
	for i, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" || len(part) > 4 {
			return SchemeUnknown
		}
		val, err := strconv.ParseUint(part, 16, 64)
		if err != nil {
			return SchemeUnknown
		}
		maxVal := math.Pow(16, float64(len(part))) - 1
		channels[i] = float64(val) / maxVal
	}
	linearize := func(c float64) float64 {
		if c <= 0.04045 {
			return c / 12.92
		}
		return math.Pow((c+0.055)/1.055, 2.4)
	}
	luminance := 0.2126*linearize(channels[0]) + 0.7152*linearize(channels[1]) + 0.0722*linearize(channels[2])
	if luminance > 0.5 {
		return SchemeLight
	}
	return SchemeDark
}
