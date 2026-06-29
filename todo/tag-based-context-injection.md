# Tag-based context injection for CC sessions

## Problem

CC's CLAUDE.md context inheritance is tree-based -- files are discovered by walking up the directory tree from the working directory. This forces a project directory layout where shared context maps to a tree hierarchy. Projects that need the same context but don't belong in the same subtree must duplicate instructions. Cross-cutting concerns (tool usage conventions, coding style, workflow rules) don't map to directory structure.

The user currently maintains hundreds of lines of manual instructions about tool usage (release tooling, safe deletion, safe git, etc.) in ancestor CLAUDE.md files. This is backwards -- the tools know how to be used and should ship that knowledge themselves. The user shouldn't be transcribing tool documentation into agent instructions.

## Solution

A tag-based context assembly system that replaces tree-based inheritance with composable, priority-ordered context fragments.

### Core design

- **Context fragments**: Markdown files in `~/.claudewheel/contexts/` with TOML frontmatter (`+++` delimiters). The filename stem is the tag name. Each fragment declares `priority` (numeric, higher = sorted higher in output) and optional `implies` (list of tag names that are transitively included).
- **Implication graph**: Tags can imply other tags, forming a DAG. Resolution: collect all tags (direct + transitive), deduplicate, sort descending by priority, concatenate stripped content.
- **Per-project tag lists**: Each project declares its tags in a `.claudewheel/tags` file (one tag per line, comments with `#`). This file is committed to the repo so contributors can see what contexts the project expects. Tags can also be auto-detected -- e.g., the presence of `.rlsbl/` in a project implies the `rlsbl` tag. Explicit tags in the file and auto-detected tags are unioned.
- **Default tags**: Tags can be marked as defaults in their frontmatter (`default = true`). Default tags apply to all projects unless explicitly opted out. A project opts out by listing `!tagname` in its `.claudewheel/tags` file. This avoids the maintenance burden of tagging every project with universal concerns.
- **Injection**: The assembled string is passed to CC via `--append-system-prompt` at launch time. This preserves CC's default system prompt and is additive. No files to manage, no cleanup, no git pollution.
- **Auto-generated manifest**: A read-only (chmod 444) TOML manifest is generated from all fragments' frontmatter, providing an at-a-glance view of the tag graph, priorities, and implications. Source of truth is the frontmatter; manifest is derived.

### Tool-shipped context

Tools installed as Python packages (e.g., release tooling, safe deletion tools) can ship their own context fragments via `importlib.resources`. At launch, the system discovers installed tools that provide agent context and includes their fragments alongside user-authored ones. The tool author maintains the context -- it updates when the tool updates.

Discovery mechanism: each tool that ships context exposes it via a known subpackage path (e.g., `<package>.agent.context`). The system scans installed packages for this path using `importlib.resources.files()`.

### Delivery mechanism

Two options for passing assembled context to CC:
- **`--append-system-prompt "<string>"`**: Inline. Simple but subject to shell argument length limits (typically 128KB-2MB depending on OS, but large strings are unwieldy).
- **`--append-system-prompt-file <path>`**: Write assembled content to a temp file, pass the path. Better for large assemblies. The file can be cleaned up after CC reads it (or left in the scratchpad dir for the session's lifetime).

Prefer `--append-system-prompt-file` as the default delivery mechanism. Fall back to inline only for very small assemblies.

### Injection channels

CC has two distinct channels for context injection, and they are complementary:

- **System prompt** (`--append-system-prompt`): Lands in the `system` field of the API request. Benefits from prompt caching (static prefix). Higher primacy bias (model sees it first). But competes with CC's own system prompt for the token budget.
- **Hook-injected context**: CC hooks (`SessionStart`, `UserPromptSubmit`) can return `additionalContext` which lands in the messages array alongside CLAUDE.md content. Per-turn injection, more dynamic, but no prompt caching benefit.

The tag-assembled context should go via the system prompt channel (static, cacheable, session-scoped). Dynamic per-turn context (if needed later) can use hooks as a separate concern.

Note: CLAUDE.md content also lands in the messages array, not the system prompt. So our assembled context via `--append-system-prompt` occupies a different position from CLAUDE.md. Both are visible to the model but with different attention characteristics.

### Token budget constraint

CC's own system prompt (built-in instructions + tool definitions) is already ~18-30K tokens. Our assembled context adds on top of that. Research showed that when CC's total system prompt grew to ~112K tokens, sessions became effectively unusable.

Practical ceiling: **keep assembled context under ~15K tokens** (not 30K as a naive estimate might suggest), since CC's baseline already consumes 18-30K. Total system prompt should stay well under 60K to avoid quality degradation. The validation step should measure and warn.

### Prompt caching awareness

CC's prompt caching uses byte-identical prefix matching with a 5-minute TTL, at the workspace level (not per-conversation). The assembly must be deterministic:
- Same tag set must always produce the exact same byte sequence
- Sort by priority (deterministic tiebreaking by tag name)
- Strip frontmatter identically
- Join with a consistent separator
- Never embed timestamps, session IDs, or any dynamic content

Same tag combination across sessions = cache hits. Different combinations = separate cache entries (acceptable).

### Fragment authoring

```
+++
priority = 900
implies = ["safegit"]
+++

Use `saferm` instead of `rm` for all file deletions...
```

- TOML over YAML: `tomllib` in stdlib (Python 3.11+, zero deps), no implicit type coercion, duplicate keys are hard errors
- Frontmatter stripped during assembly -- only content reaches the system prompt
- Individual fragments should stay under ~200 lines (adherence drops past that threshold)
- Total assembled output should stay under ~30K tokens for quality

### Auto-detection rules

Configurable rules that map project signals to tags:
- File/directory existence: `.rlsbl/` exists -> tag `rlsbl`
- Config file content: `pyproject.toml` exists -> tag `python`
- Explicit tag list overrides auto-detection for that tag

### Validation

Hard errors at launch time (not soft skips):
- Tag referenced in implies but no corresponding `.md` file exists
- Tag in project's tag list but no fragment found (neither user-authored nor tool-shipped)
- Cycle detection in implication graph (treat as set -- deduplicate, don't error, since cycles in implies are just redundant edges in a DAG)
- Fragment file exists but has no/invalid TOML frontmatter

### Coexistence with CC's `.claude/rules/`

CC has its own conditional rules system: `.claude/rules/*.md` files with optional `paths:` frontmatter for glob-based activation. These are a different composition axis from tags:

- **CC rules**: file-pattern-scoped, activated when the agent reads matching files, live in the repo
- **Tag contexts**: project-scoped, activated at session launch, assembled from user/tool sources

They don't conflict -- CC rules are project-specific conditional instructions, while tag contexts are cross-project tool/workflow knowledge. Both channels are active simultaneously. Project authors should put file-pattern-scoped instructions in `.claude/rules/` and declare cross-cutting tool concerns via tags.

### Repo sharing model

Tag list is committed to the repo (declares what the project needs). Context content is never committed -- always assembled from each user's own `~/.claudewheel/contexts/` and installed tool packages. Contributors see which tags a project expects. Missing user-authored tags surface as clear messages at launch. Missing tool-shipped tags are hard errors (the tool should be installed).

## Affected files

- `launch.py` (`resolve_launch_config`) -- add `--append-system-prompt` to argv
- `cli.py` (`_do_launch_sequence`) -- add context loading/validation step
- `config.py` -- add migration for new config keys (contexts dir, auto-detection rules)
- `defaults.py` -- new config section
- New module: context assembly engine (fragment discovery, frontmatter parsing, implication resolution, deterministic assembly)
- New CLI command: `claudewheel contexts` subcommands (list, gen, validate, graph)

## Effort

Medium-large. The core assembly engine (frontmatter parsing, DAG resolution, deterministic concatenation) is straightforward. The complexity is in: tool-shipped context discovery via importlib.resources, auto-detection rules, the manifest generator, validation, and the CLI commands. Roughly:
- Core engine: 1-2 sessions
- Tool-shipped discovery: 1 session
- Auto-detection: 1 session
- CLI + manifest: 1 session
- Tests: 1-2 sessions
