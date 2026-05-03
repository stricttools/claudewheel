#!/usr/bin/env bash
# redir-history.sh -- Replace an old project path with a new one in Claude Code
# history.jsonl files across all ~/.claude-* profiles.
#
# Usage:
#   redir-history.sh OLD_PATH NEW_PATH [--dry-run]
#
# Both paths must be absolute. In --dry-run mode, no files are modified.

set -euo pipefail

# -- Argument parsing --

if [[ $# -lt 2 || $# -gt 3 ]]; then
    echo "Usage: $0 OLD_PATH NEW_PATH [--dry-run]"
    exit 1
fi

old_path="$1"
new_path="$2"
dry_run=false

if [[ $# -eq 3 ]]; then
    if [[ "$3" == "--dry-run" ]]; then
        dry_run=true
    else
        echo "Error: unknown argument '$3' (expected --dry-run)"
        exit 1
    fi
fi

# Validate that both paths are absolute
if [[ "$old_path" != /* || "$new_path" != /* ]]; then
    echo "Error: both OLD_PATH and NEW_PATH must be absolute paths"
    exit 1
fi

if $dry_run; then
    echo "[dry-run] Would replace:"
else
    echo "Replacing:"
fi
echo "  old: $old_path"
echo "  new: $new_path"
echo

# -- Gather history files --

history_files=()
for f in "$HOME"/.claude-*/history.jsonl; do
    # Guard against unexpanded glob (no matches)
    [[ -f "$f" ]] && history_files+=("$f")
done

if [[ ${#history_files[@]} -eq 0 ]]; then
    echo "No history.jsonl files found under ~/.claude-*/"
    exit 0
fi

# -- Process each file --

total_files=0
total_lines=0

# Escape slashes and special sed chars in paths for use in sed patterns
sed_old=$(printf '%s\n' "$old_path" | sed 's/[&/\]/\\&/g')
sed_new=$(printf '%s\n' "$new_path" | sed 's/[&/\]/\\&/g')

for file in "${history_files[@]}"; do
    # Count lines containing the old path
    match_count=$(grep -cF "$old_path" "$file" 2>/dev/null || true)

    if [[ "$match_count" -gt 0 ]]; then
        total_files=$((total_files + 1))
        total_lines=$((total_lines + match_count))

        profile_dir=$(basename "$(dirname "$file")")

        if $dry_run; then
            echo "  [dry-run] $profile_dir/history.jsonl: $match_count lines match"
        else
            sed -i "s|${old_path}|${new_path}|g" "$file"
            echo "  $profile_dir/history.jsonl: $match_count lines updated"
        fi
    fi
done

# -- Summary --

echo
if $dry_run; then
    echo "Summary (dry-run): $total_lines lines in $total_files files would be updated."
else
    echo "Summary: $total_lines lines in $total_files files updated."
fi
