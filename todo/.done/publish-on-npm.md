# Publish claudewheel on npm

## Context

claudewheel is a pure Python 3.14+ TUI launcher for Claude Code. It has no npm presence yet. Since the target audience (Claude Code users) already has Node.js installed, npm is the natural distribution channel. The `rlsbl` tool can scaffold the release infrastructure.

## Problem

claudewheel is only installable by cloning the repo and symlinking the `c` script manually. It should be `npm i -g claude-launcher` (or similar name -- check availability with `scripts/check-name` in the rlsbl project).

## Steps

1. Choose an npm package name (check with rlsbl's `scripts/check-name`)
2. Create `package.json` with bin entry pointing to a wrapper script
3. Create a bin wrapper (Node.js or shell) that:
   - Checks Python 3.14+ is available
   - Sets PYTHONPATH to the installed package location
   - Execs `python3 -m claude_launcher "$@"`
4. Run `rlsbl init` to scaffold CI + publish workflows, CHANGELOG, LICENSE, etc.
5. Create GitHub repo: `gh repo create %~rOe/claude-launcher --public --source . --push`
6. First publish locally: `npm login && npm publish --access public` (with granular token)
7. Configure Trusted Publishing on npmjs.com
8. Verify `npm i -g <name>` works end-to-end on a clean machine

## Files that change

New files:

- `package.json`
- `bin/` wrapper
- `.github/workflows/`
- `CHANGELOG.md`
- `LICENSE`
- `.gitignore` (extended)
- `CLAUDE.md` (extended)

## Considerations

- Users need Python 3.14+ installed -- the npm package ships source, not binaries
- The `c` entry point script already exists but uses bash; the npm wrapper should be similar but resolve paths relative to the npm install location
- Need to decide: keep the `c` command name or use something else for the npm bin?

## Effort

Medium -- packaging wrapper + testing on clean environments.
