package effects

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// downloadChunk is how much of a download is read and written at a time.
const downloadChunk = 1 << 20

// Request is one HTTP request.
type Request struct {
	Method string
	URL    string
	// Header holds the request headers, one value each.
	Header map[string]string
	// Body is the request body; nil sends none.
	Body []byte
	// Timeout is required. It bounds the whole request for HTTPRead; for
	// Download it bounds the wait for the response and every pause between
	// two chunks of the body.
	Timeout time.Duration
	// Resource and Grant annotate a recorded Download (see Cmd).
	Resource string
	Grant    string
	// Redact lists values (tokens) replaced with RedactedMarker in the
	// dry-run record and in every error the call returns.
	Redact []string
}

func (r Request) validate(method string) error {
	if r.Method == "" || r.URL == "" {
		return fmt.Errorf("effects.%s: a request needs a Method and a URL", method)
	}
	if r.Timeout <= 0 {
		return fmt.Errorf("effects.%s %s %s: Timeout is required and must be positive", method, r.Method, Redact(r.URL, r.Redact...))
	}
	return validateRedact(r.Redact, "effects."+method+" "+r.Method)
}

// shown is the request line, redacted.
func (r Request) shown() string {
	return Redact(r.Method+" "+r.URL, r.Redact...)
}

// Response is a successful (2xx) HTTP response.
type Response struct {
	Status int
	// Header holds the first value of each response header, by lower-case
	// name.
	Header map[string]string
	Body   []byte
}

// StatusError is a response whose status is not 2xx. Redirects have already
// been followed.
type StatusError struct {
	// Request is the request line, redacted.
	Request string
	Status  int
	Body    []byte
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("%s: HTTP status %d", e.Request, e.Status)
}

// HTTPRead performs a request that changes nothing on the far side (fetching
// a manifest, validating a token). It runs in every mode, read-only and
// --dry-run included, and is never recorded. A non-2xx status is a
// *StatusError.
func (fx *FX) HTTPRead(r Request) (Response, error) {
	if err := r.validate("HTTPRead"); err != nil {
		return Response{}, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), r.Timeout)
	defer cancel()
	resp, err := send(ctx, r)
	if err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return Response{}, fmt.Errorf("%s: %w after %s", r.shown(), ErrTimedOut, r.Timeout)
		}
		return Response{}, RedactError(err, r.Redact...)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return Response{}, fmt.Errorf("%s: %w after %s", r.shown(), ErrTimedOut, r.Timeout)
		}
		return Response{}, RedactError(err, r.Redact...)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return Response{}, &StatusError{Request: r.shown(), Status: resp.StatusCode, Body: body}
	}
	headers := make(map[string]string, len(resp.Header))
	for name, values := range resp.Header {
		if len(values) > 0 {
			headers[strings.ToLower(name)] = values[0]
		}
	}
	return Response{Status: resp.StatusCode, Header: headers, Body: body}, nil
}

// Download fetches r (a GET) into the file path, created or truncated, in
// chunks, calling onChunk (when not nil) with each chunk once it is written,
// so the caller can hash the body and report progress. A non-2xx status is a
// *StatusError. On failure path may hold a partial body; removing it is the
// caller's. Under --dry-run it records the request and the write of path
// from its output, transferring nothing and calling onChunk never.
func (fx *FX) Download(r Request, path string, onChunk func(chunk []byte)) error {
	if err := r.validate("Download"); err != nil {
		return err
	}
	if r.Method != http.MethodGet {
		return fmt.Errorf("effects.Download %s: a download is a GET", r.shown())
	}
	if err := fx.admit("download "+r.URL+" -> "+path, r.Redact); err != nil {
		return err
	}
	if fx.handle != nil {
		opts := recordOptions(r.Resource, "", r.Grant, r.Redact)
		for name, value := range r.Header {
			opts = append(opts, strictcli.Header(name, value))
		}
		body, err := fx.handle.HTTP(r.Method, r.URL, opts...)
		if err != nil {
			return err
		}
		_, err = fx.handle.Write(path, body)
		return err
	}
	return RedactError(download(r, path, onChunk), r.Redact...)
}

// download performs a Download, its Timeout restarted by every chunk.
func download(r Request, path string, onChunk func([]byte)) error {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var timedOut atomic.Bool
	watchdog := time.AfterFunc(r.Timeout, func() {
		timedOut.Store(true)
		cancel()
	})
	defer watchdog.Stop()
	stalled := func(err error) error {
		if timedOut.Load() {
			return fmt.Errorf("%s: %w: nothing arrived for %s", r.shown(), ErrTimedOut, r.Timeout)
		}
		return err
	}

	resp, err := send(ctx, r)
	if err != nil {
		return stalled(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, downloadChunk))
		return &StatusError{Request: r.shown(), Status: resp.StatusCode, Body: body}
	}
	watchdog.Reset(r.Timeout)

	out, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, newFileMode)
	if err != nil {
		return err
	}
	buf := make([]byte, downloadChunk)
	for {
		n, readErr := io.ReadFull(resp.Body, buf)
		if n > 0 {
			watchdog.Reset(r.Timeout)
			if _, err := out.Write(buf[:n]); err != nil {
				out.Close()
				return err
			}
			if onChunk != nil {
				onChunk(buf[:n])
			}
		}
		if readErr == io.EOF || readErr == io.ErrUnexpectedEOF {
			break
		}
		if readErr != nil {
			out.Close()
			return stalled(readErr)
		}
	}
	return out.Close()
}

// send issues r on a client with no overall timeout: the caller's context
// bounds it.
func send(ctx context.Context, r Request) (*http.Response, error) {
	var body io.Reader
	if r.Body != nil {
		body = bytes.NewReader(r.Body)
	}
	req, err := http.NewRequestWithContext(ctx, r.Method, r.URL, body)
	if err != nil {
		return nil, err
	}
	for name, value := range r.Header {
		req.Header.Set(name, value)
	}
	return http.DefaultClient.Do(req)
}
