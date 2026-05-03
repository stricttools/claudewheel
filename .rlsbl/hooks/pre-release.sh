#!/usr/bin/env bash
# Pre-release validation hook.
# Runs before rlsbl creates a release. Exit non-zero to abort.
# Detects project type and runs appropriate checks automatically.

set -euo pipefail

echo "Running pre-release checks..."

if [ -f go.mod ]; then
  echo "Detected Go project"
  go vet ./...
  go build ./...
  go test ./... -race -short -count=1
elif [ -f package.json ]; then
  echo "Detected npm project"
  npm test
elif [ -f pyproject.toml ]; then
  echo "Detected Python project"
  if command -v uv &>/dev/null; then
    uv run pytest
  elif command -v pytest &>/dev/null; then
    pytest
  fi
fi

echo "Pre-release checks passed."
