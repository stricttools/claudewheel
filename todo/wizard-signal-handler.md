# Signal handler conflict between TUI and wizard

## Problem

When the wizard launches from the TUI via `_launch_profile_wizard` (app.py), the app's SIGTERM/SIGHUP handlers remain active. These handlers call `self.terminal.exit_raw()` and `sys.exit(1)`. But the wizard has already called `self.terminal.exit_raw()` (app.py) and created its own Terminal instance in raw mode (wizard.py).

If SIGTERM arrives during the wizard, the app's handler tries to restore the app terminal's old attributes, which could interfere with the wizard's terminal state.

## Suggested fix

In `_launch_profile_wizard`, save and restore signal handlers around the wizard call, or install temporary handlers that are aware of the wizard's terminal state. Alternatively, have the wizard install its own SIGTERM handler that cleans up its own Terminal before re-raising.

## Scope

Low priority. The wizard runs for seconds at most, and SIGTERM during that window is rare. Pre-existing issue.
