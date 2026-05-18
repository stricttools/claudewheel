# Replace LSP with per-language CLI tools via MCP

## Context

We disabled the LSP tool in Claude Code because it pollutes conversations with stale diagnostics. But code navigation (go-to-definition, find-references, type checking) is genuinely useful for large typed codebases.

## Problem

Without LSP, Claude relies on grep/Read/Agent exploration for code navigation, which works but is slower and less precise for type-heavy projects. The LSP tool's diagnostics noise made it a net negative, but the underlying capability has value.

## Solutions

### Option A: MCP server wrapping language CLI tools

Build an MCP server that exposes tools like `typecheck`, `lint`, `find-definition`, `find-references` by shelling out to language-specific CLIs:

- Python: mypy (type check), ruff (lint), pyright (find-definition/references)
- TypeScript/JavaScript: tsc --noEmit (type check), eslint (lint), tsserver queries
- Go: go vet, gopls
- Rust: cargo check, rust-analyzer

| Pros | Cons |
|------|------|
| Full control over what output reaches the conversation | Must maintain per-language adapters |
| No stale diagnostics -- tools run on demand | Initial development effort |
| Can filter/format output to be concise | Need to handle LSP server lifecycle for some tools |
| Reusable across all projects | Different languages have different tool maturity |

### Option B: Curated LSP wrapper MCP

Build an MCP server that starts a real LSP server (pyright, tsserver, gopls) behind the scenes but only exposes specific operations (definition, references, hover) and filters out diagnostics.

| Pros | Cons |
|------|------|
| Full LSP capability without the noise | More complex (managing LSP server lifecycle) |
| Consistent interface across languages | LSP servers can be resource-heavy |
| Leverages existing LSP ecosystem | Stale state is still possible, just hidden |

### Option C: CLAUDE.md guidance only

Don't build anything. Add guidance to CLAUDE.md about using CLI tools directly via Bash (e.g., "run mypy to type-check before committing Python").

| Pros | Cons |
|------|------|
| Zero development effort | No structured tool interface |
| Works immediately | Claude must know which CLI to use per language |
| No maintenance burden | Output parsing is ad-hoc |

## Files/dirs that would change

- New MCP server project (likely ~/Projects/claude-mcp-lint or similar)
- ~/.claude/settings.json or .mcp.json for MCP server registration
- ~/CLAUDE.md (guidance on using the new tools)
- claudewheel wizard if MCP config needs per-profile setup

## Effort

- Option A: Medium (2-3 sessions per language, starting with Python)
- Option B: High (LSP lifecycle management is tricky)
- Option C: Low (30 minutes)
