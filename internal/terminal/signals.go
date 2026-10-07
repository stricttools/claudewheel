package terminal

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"

	"golang.org/x/sys/unix"
)

// ErrInterrupted is the cancellation cause when SIGINT (Ctrl-C under cbreak)
// arrives while WithSignals is watching. The command exits 130 on it.
var ErrInterrupted = fmt.Errorf("interrupted by Ctrl-C: %w", context.Canceled)

// ErrTerminated is the cancellation cause when SIGTERM or SIGHUP arrives
// while WithSignals is watching. The command exits 1 on it.
var ErrTerminated = fmt.Errorf("terminated by SIGTERM or SIGHUP: %w", context.Canceled)

// ErrEntryCancelled is returned by ReadMaskedLine when the user cancels the
// entry with Escape, Ctrl-D, or a Ctrl-C byte.
var ErrEntryCancelled = errors.New("entry cancelled")

// WithSignals returns a context cancelled with cause ErrInterrupted on SIGINT
// and ErrTerminated on SIGTERM or SIGHUP, so that the key loops return, the
// deferred terminal restore runs, and the command exits with the matching
// status. stop ends the watch and is safe to call more than once; until it is
// called, these signals no longer kill the process.
func WithSignals(parent context.Context) (ctx context.Context, stop func()) {
	ctx, cancel := context.WithCancelCause(parent)
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, unix.SIGINT, unix.SIGTERM, unix.SIGHUP)
	go func() {
		select {
		case sig := <-ch:
			if sig == unix.SIGINT {
				cancel(ErrInterrupted)
			} else {
				cancel(ErrTerminated)
			}
		case <-ctx.Done():
		}
	}()
	stop = func() {
		signal.Stop(ch)
		cancel(context.Canceled)
	}
	return ctx, stop
}

// cancelled returns the error a reader reports once ctx is done: the
// cancellation cause, which is ErrInterrupted or ErrTerminated when a signal
// did it and ctx.Err() otherwise. Both signal causes wrap context.Canceled.
func cancelled(ctx context.Context) error {
	return context.Cause(ctx)
}
