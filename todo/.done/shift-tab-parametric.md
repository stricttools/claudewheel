# Shift-Tab parametric escape sequence

## Problem

`terminal.py:read_key()` handles `\x1b[Z` for Shift-Tab (covers most terminals). However, xterm and some terminal emulators emit the parametric form `\x1b[1;2Z` (modified-key encoding). The current parser reads one byte after `[` and matches it, so `\x1b[1;2Z` would be parsed as `ESC[1` with the `;2Z` left in the buffer.

## Suggested fix

Extend the escape sequence parser to handle CSI sequences with numeric parameters (`\x1b[<params><final>`). This would cover not just Shift-Tab but also other modified keys (Shift-arrows, Ctrl-arrows, etc.).

## Scope

Low priority. The current `\x1b[Z` handling covers the vast majority of terminals (VTE-based, iTerm2, Windows Terminal, Apple Terminal). The parametric form is primarily xterm with `modifyOtherKeys` enabled.
