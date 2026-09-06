# Client-adapter capability declarations and client-aware preflight

## Context

`claudewheel/clients.py` is a real multi-client seam: a registry mapping client names to
argv builders, with two working adapters (the claude client and the miniclaude client,
which has its own permission-vocabulary translation and hard-errors on inputs it cannot
express). The generalization below is worth doing on its own merits — one adapter
already exercises it — and is the prerequisite for any future third client.

## The live defect (verified 2026-09-06)

`CLAUDE_ONLY_SELECTIONS` in clients.py declares itself the single source of truth for
three enforcement sites and has ZERO consumers — the sites carry their own copies, and
they disagree: the CLI rejects claude-only selections for a non-claude client
(`version`, and `mcp=strict`), while the TUI drops only the `version` segment and still
PRESENTS the MCP segment for miniclaude, silently ignoring a `strict` choice the CLI
would hard-error on. Same value, same client, opposite treatment by input path. Decide
the intended behavior, then make the three sites derivations of one declaration.

## The reduction design

Turn the registry values into an adapter record carrying per-client facts, with the
polarity inverted from "claude-only" (which privileges one client) to "inapplicable to
this adapter":

- Two declaration kinds are needed, not one: segments wholly inapplicable to the
  adapter (TUI hides the segment; CLI rejects any explicit value — the version segment
  for non-claude clients) versus individual values inapplicable (segment stays, one
  value rejected — mcp strict for miniclaude, whose default value remains meaningful).
  A single predicate cannot express both without either hiding a segment that has a
  usable default or keeping today's silent-ignore.
- `client_available` moves onto the adapter too (today a per-name if-chain ending in an
  unconditional available-for-unknown-names return; under a registry-driven design that
  fallback is unreachable — unknown names already hard-error at three call sites — and
  should be deleted rather than carried).

## Client-aware preflight

`PreflightContext` carries no client field, yet the client is already in scope at the
call site that builds it — plumbing it in is one line. Of the six registered steps,
four are claude-specific in substance (the vanilla-choice prompt writes claude hook
wiring; the guardrail reconcile operates on claude settings; the model-version guard
compares against claude binary versions and would mis-fire the moment its table gains a
non-claude entry; the plan-declaration step HARD-ABORTS any launch of a profile holding
a token with no declared Anthropic plan — the one step that outright breaks a
non-claude launch). Applicability belongs DECLARED on the step (alongside the existing
non-interactive flag, enforced by the runner) rather than as in-step conditionals — one
step's body is wrapped in a blanket exception swallow, where an in-step check could
fail silently.

The exception that must be parameterized rather than excluded: the approved-hooks step
(fingerprints the target project's claude-side settings and refuses unapproved hooks).
Excluding it for a future non-claude client would silently skip reviewing that client's
project-level hooks — the one direction where skipping is a security hole, not wasted
work.

## Duplications to collapse while there

- The claude versions directory path is hardcoded in both `claudewheel/binaries.py` and
  the version segment's discovery entry in `claudewheel/defaults.py` (plus README/docs
  copies) — a per-client version list drifts on the first divergence.
- The binary download base URL in `claudewheel/install.py` is claude-specific with no
  per-client story; fine today, named so nobody generalizes half of it.

## Affected files

`claudewheel/clients.py`, `claudewheel/cli.py`, `claudewheel/app.py`,
`claudewheel/preflight.py`, `claudewheel/binaries.py`, `claudewheel/defaults.py`;
tests: `tests/test_clients.py` (or wherever adapter tests live), `tests/test_cli.py`,
`tests/test_preflight.py`.

## Effort

Small-to-medium: the adapter record and three-site derivation are one pass; the
preflight applicability field is small; the mcp-segment behavior needs one ruling
(present-and-reject vs hide) before the refactor bakes one in.
