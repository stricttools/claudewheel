// Package effects is the one place claudewheel changes the world: every
// subprocess, filesystem mutation, signal, process replacement, and network
// request made by the rest of the module goes through an explicit *FX.
//
// The behavior is split by mode, decided when the FX is built and identical
// on every call:
//
//   - Live (a mutating command without --dry-run, or Standalone): operations
//     run directly through os, os/exec, syscall, and net/http with their full
//     semantics: per-call timeouts, the atomic staged write that keeps the
//     target's mode, the 0600-from-creation secret write, chunked downloads,
//     and the distinction between "missing is an error" and "missing is fine".
//   - Preview (a mutating command under --dry-run): every mutation is recorded
//     on the strictcli context's effects handle and nothing runs. Operations
//     the handle has a method for use it; the others are recorded as the
//     command that performs them (ln -s, kill -N, touch -d, and a process
//     replacement as a run). A recorded run returns a Result whose accessors
//     end the preview through strictcli's truncation when read.
//   - Read-only (a read-only command): every mutation is refused with an
//     error wrapping ErrReadOnly.
//
// Reads are never effects: a Cmd with Read set, HTTPRead, and Follow run in
// every mode, read-only included, and are never recorded. File reads use os
// directly and need no FX.
package effects

import (
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"
)

// RedactedMarker replaces every redacted value wherever this package renders
// an operation: dry-run records and returned errors. It is strictcli's marker,
// so a redacted value reads the same in both.
const RedactedMarker = "«redacted»"

// ErrReadOnly is wrapped by the error every mutation returns on an FX built
// by ReadOnly.
var ErrReadOnly = errors.New("this command is read-only and changes nothing")

// FX performs or records the effects of one command dispatch. Build it with
// New, ReadOnly, or Standalone; the zero value is unusable.
type FX struct {
	// ctx is the dispatch context, nil for Standalone.
	ctx *strictcli.Context
	// handle is the effects handle a preview records on, nil outside a preview.
	handle *strictcli.Effects
	// readOnly refuses every mutation.
	readOnly bool
	// out receives Info lines when there is no dispatch context.
	out io.Writer
}

// New builds the FX of a mutating command's dispatch: it records every
// mutation on ctx's effects handle under --dry-run and performs it otherwise.
func New(ctx *strictcli.Context) *FX {
	fx := &FX{ctx: ctx}
	if ctx.DryRun() {
		fx.handle = ctx.Effects()
	}
	return fx
}

// ReadOnly builds the FX of a read-only command's dispatch: reads run, and
// every mutation is refused with an error wrapping ErrReadOnly.
func ReadOnly(ctx *strictcli.Context) *FX {
	return &FX{ctx: ctx, readOnly: true}
}

// Standalone builds an FX for code running outside any command dispatch,
// such as tests: every operation is performed directly and nothing is
// recorded. Info lines are written to out.
func Standalone(out io.Writer) *FX {
	return &FX{out: out}
}

// Previewing reports whether this FX records mutations instead of performing
// them (a --dry-run dispatch).
func (fx *FX) Previewing() bool {
	return fx.handle != nil
}

// Info writes one informational line through the dispatch context, which
// suppresses it under --quiet and records it as a diagnostic under --json.
// A Standalone FX writes it to its writer.
func (fx *FX) Info(msg string) {
	if fx.ctx != nil {
		fx.ctx.Info(msg)
		return
	}
	fmt.Fprintln(fx.out, msg)
}

// admit refuses a mutation on a read-only FX. what names the operation and
// its operands, and is redacted before it reaches the error.
func (fx *FX) admit(what string, redact []string) error {
	if fx.readOnly {
		return fmt.Errorf("%w: refused %s", ErrReadOnly, Redact(what, redact...))
	}
	return nil
}

// Redact returns text with every occurrence of each secret replaced by
// RedactedMarker, longest secret first so a secret containing another is
// hidden whole. Empty secrets are skipped.
func Redact(text string, secrets ...string) string {
	sorted := make([]string, 0, len(secrets))
	for _, s := range secrets {
		if s != "" {
			sorted = append(sorted, s)
		}
	}
	if len(sorted) == 0 {
		return text
	}
	sort.SliceStable(sorted, func(i, j int) bool { return len(sorted[i]) > len(sorted[j]) })
	pairs := make([]string, 0, 2*len(sorted))
	for _, s := range sorted {
		pairs = append(pairs, s, RedactedMarker)
	}
	return strings.NewReplacer(pairs...).Replace(text)
}

// RedactError returns err with its message redacted. The result still
// answers errors.Is and errors.As for err, but its message never carries a
// secret. A nil err, or one whose message holds no secret, is returned as is.
func RedactError(err error, secrets ...string) error {
	if err == nil {
		return nil
	}
	msg := Redact(err.Error(), secrets...)
	if msg == err.Error() {
		return err
	}
	return &redactedError{msg: msg, orig: err}
}

// redactedError carries a redacted message for an error whose own message
// holds a secret.
type redactedError struct {
	msg  string
	orig error
}

func (r *redactedError) Error() string { return r.msg }

func (r *redactedError) Is(target error) bool { return errors.Is(r.orig, target) }

func (r *redactedError) As(target interface{}) bool { return errors.As(r.orig, target) }

// validateRedact refuses an empty redaction value, which redacts nothing.
func validateRedact(values []string, what string) error {
	for _, v := range values {
		if v == "" {
			return fmt.Errorf("%s: an empty Redact value redacts nothing", what)
		}
	}
	return nil
}

// recordOptions are the strictcli options every recorded operation carries
// from its declaration: the resource and skip-if-current tokens, the grant,
// and the redacted values.
func recordOptions(resource, skipIfCurrent, grant string, redact []string) []strictcli.EffectOption {
	var opts []strictcli.EffectOption
	if resource != "" {
		opts = append(opts, strictcli.Resource(resource))
	}
	if skipIfCurrent != "" {
		opts = append(opts, strictcli.SkipIfCurrent(skipIfCurrent))
	}
	if grant != "" {
		opts = append(opts, strictcli.UseGrant(grant))
	}
	if len(redact) > 0 {
		opts = append(opts, strictcli.Redact(redact...))
	}
	return opts
}

// operands converts argv to the handle's operand list.
func operands(argv []string) []interface{} {
	out := make([]interface{}, len(argv))
	for i, a := range argv {
		out[i] = a
	}
	return out
}
