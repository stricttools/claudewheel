#!/usr/bin/env bash
# claudewheel entry point -- symlink this to ~/.local/bin/c
exec env PYTHONPATH="${BASH_SOURCE[0]%/*}" python3 -m claude_launcher "$@"
