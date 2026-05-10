#!/usr/bin/env bash
# claudewheel entry point -- symlink this to ~/.local/bin/c

dir="${BASH_SOURCE[0]%/*}"

# Read minimum Python version from pyproject.toml (default 3.10)
req_major=3 req_minor=10
if req_line=$(grep -oP 'requires-python\s*=\s*">=\K\d+\.\d+' "$dir/pyproject.toml" 2>/dev/null); then
  req_major="${req_line%%.*}" req_minor="${req_line##*.}"
else
  echo "Warning: could not read requires-python from pyproject.toml, defaulting to $req_major.$req_minor" >&2
fi

# Check python3 version
py_ver=$(python3 --version 2>/dev/null) || { echo "claudewheel requires Python $req_major.$req_minor+, but python3 was not found in PATH." >&2; exit 1; }
IFS=' .' read -r _ py_major py_minor _ <<< "$py_ver"
if (( py_major < req_major || (py_major == req_major && py_minor < req_minor) )); then
  echo "claudewheel requires Python $req_major.$req_minor+. Found: $py_ver" >&2
  exit 1
fi

exec env PYTHONPATH="$dir" python3 -m claudewheel "$@"
