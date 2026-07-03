"""Run a child process under a PTY, proxying the real terminal and capturing its output."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import termios
import tty


def _copy_winsize(src_fd: int, dst_fd: int) -> None:
    """Copy the window size from src_fd to dst_fd; ignore failures."""
    try:
        packed = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def run_under_pty(
    argv: list[str],
    env: dict[str, str],
    *,
    input_bytes: bytes | None = None,
    proxy_terminal: bool = True,
) -> tuple[int, bytes]:
    """Run argv with env under a fresh PTY; return (exit_code, captured_bytes).

    With proxy_terminal=True (the default) the real terminal is opened via
    /dev/tty (stdin may be piped), put into full raw mode so every byte --
    including Ctrl-C -- is forwarded to the child, and window-size changes
    are propagated on SIGWINCH. Child output is both written through to the
    real terminal and captured. Raises RuntimeError if /dev/tty cannot be
    opened.

    With proxy_terminal=False no real terminal is touched: the child still
    runs under a PTY, but output is only captured. This is the headless mode
    used by tests and non-interactive callers.

    input_bytes, if given, is scripted input written to the PTY master right
    after the child starts -- it lets callers (and tests) drive an
    interactive child without a human typing.
    """
    tty_file = None
    if proxy_terminal:
        try:
            # Open /dev/tty directly so this works even when stdin is piped.
            tty_file = open("/dev/tty", "r+b", buffering=0)
        except OSError as exc:
            raise RuntimeError(
                "cannot open /dev/tty: a real terminal is required to proxy "
                "the child process (run with proxy_terminal=False for "
                "headless capture)"
            ) from exc

    try:
        pid, master_fd = pty.fork()
    except OSError:
        if tty_file is not None:
            tty_file.close()
        raise

    if pid == 0:
        # Child: stdin/stdout/stderr are already wired to the PTY slave.
        try:
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)

    # Parent: proxy real terminal <-> master_fd while capturing output.
    captured = bytearray()
    old_attrs = None
    prev_winch = None
    winch_installed = False
    tty_fd = tty_file.fileno() if tty_file is not None else None

    try:
        if tty_fd is not None:
            _copy_winsize(tty_fd, master_fd)

            old_attrs = termios.tcgetattr(tty_fd)
            # Full raw mode (not cbreak): Ctrl-C must reach the child as a
            # byte instead of SIGINT-ing this process.
            tty.setraw(tty_fd)

            def on_winch(signum, frame):
                _copy_winsize(tty_fd, master_fd)
                try:
                    os.kill(pid, signal.SIGWINCH)
                except ProcessLookupError:
                    pass

            prev_winch = signal.signal(signal.SIGWINCH, on_winch)
            winch_installed = True

        if input_bytes:
            # os.write may write fewer bytes than given (PTY buffer full);
            # loop so large payloads are never silently truncated.
            view = memoryview(input_bytes)
            while view:
                written = os.write(master_fd, view)
                view = view[written:]

        read_fds = [master_fd] if tty_fd is None else [master_fd, tty_fd]
        while True:
            try:
                rlist, _, _ = select.select(read_fds, [], [])
            except InterruptedError:
                continue
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    # EIO when the child closed the slave side -- normal
                    # end-of-stream behavior on Linux.
                    data = b""
                if not data:
                    break
                captured.extend(data)
                if tty_fd is not None:
                    os.write(tty_fd, data)
            if tty_fd is not None and tty_fd in rlist:
                data = os.read(tty_fd, 65536)
                if data:
                    os.write(master_fd, data)
    finally:
        if winch_installed:
            # prev_winch may be None if the previous handler was not
            # installed from Python; fall back to SIG_DFL (wizard.py pattern).
            signal.signal(signal.SIGWINCH, prev_winch or signal.SIG_DFL)
        if old_attrs is not None:
            termios.tcsetattr(tty_fd, termios.TCSADRAIN, old_attrs)
        if tty_file is not None:
            tty_file.close()
        os.close(master_fd)
        # Reap inside the finally so an exception escaping the proxy loop
        # (e.g. the real tty vanishing mid-session) never leaves a zombie.
        # Closing master_fd first hangs up the child's controlling terminal,
        # so this wait terminates even on the error path. This is the only
        # reap point, so a double-reap ChildProcessError cannot occur.
        _, status = os.waitpid(pid, 0)

    return os.waitstatus_to_exitcode(status), bytes(captured)
