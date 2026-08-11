# Ban non-git remote-write paths (the `gh api` / raw-API hole)

## Context

The permission enforcement layer is built around **git transport
commands**. Every spelling of a remote-mutating `git push` is caught:
`git push origin :ref` (refspec delete), `git push --delete`,
`git push +:ref` (force-plus), `git push --force-with-lease` — all
refused with "Deleting remote branches is destructive; ask the user to
do this deliberately" (and the analogous branch/tag/commit push
guards).

## Problem

The guard only sees `git`. Anything that mutates the same remote
**without invoking git** slips straight through. Confirmed live: after
every `git push` deletion spelling was blocked, a single

    gh api -X DELETE repos/<owner>/<repo>/git/refs/<...>

deleted a remote ref with no challenge at all. `gh` authenticates from
its own stored token (`~/.config/gh` / `GH_TOKEN`), independent of the
credential path git uses, so it is a fully separate, unguarded write
channel.

This generalizes. Every hardened git-transport rule (never push, no
branch creation, no force-push, release-only writes to `refs/heads/*`)
is bypassable by an agent that reaches for a different tool aimed at
the same remote:

- `gh api` / `gh release` / `gh repo` / `gh secret` / `gh pr` /
  `gh workflow` — the whole `gh` surface writes over REST/GraphQL.
- Raw HTTP to `api.github.com` / `uploads.github.com` via `curl`,
  `wget`, `http` (httpie), or an inline `node -e fetch(...)` /
  `python -c 'requests...'`.
- SDK calls: Octokit, PyGithub, go-github, etc., from a one-liner.

The same shape almost certainly defeats the **registry** guards too,
if those are also command-name-based: `npm publish` may be blocked
while `curl -X PUT https://registry.npmjs.org/<pkg>` or a direct PyPI
`POST https://upload.pypi.org/legacy/` is not. Worth auditing every
guard that assumes the official CLI is the only path.

## Why it matters

The threat model the git guards encode — "an agent must not move bytes
to a remote or change remote state without the user explicitly asking"
— is only as strong as its **least-guarded path**. A fence around
`git push` that leaves `gh api` open is not a fence; it is a
speed bump that redirects to the open gate. An agent (or a compromised
tool) that cannot push can still, via the API surface: create/delete
branches and tags, push commits through the contents API, cut GitHub
Releases, upload release assets, change repo visibility and settings,
add deploy keys, set/read Actions secrets, and open/merge PRs.

## What to ban / gate (proposal — needs a design pass)

The enforcement must move from "match the git command" to "match the
**destination**, whatever tool reaches it." Candidate rules:

- **`gh`**: deny by default, or gate every mutating subcommand
  (`api` with `-X/-F/--method` other than GET, plus `release`, `repo`,
  `secret`, `workflow run`, `pr merge`, `api graphql` with a mutation).
  Read-only `gh` (status, `api` GET, `pr list`) can stay allowed.
- **Raw HTTP clients aimed at known control-plane hosts**: `curl` /
  `wget` / `httpie` / inline `fetch`/`requests` targeting
  `api.github.com`, `uploads.github.com`, `registry.npmjs.org`,
  `upload.pypi.org`, `*.pkg.github.com`, and the other registries the
  existing rules cover. This is the hard part — it means inspecting
  URLs/args across many tools, and it is inherently porous (an agent
  can obfuscate a hostname). Treat it as raising the cost, not a
  perfect seal; pair with the token measures below.
- **Language one-liners as an exec vector**: `node -e`, `python -c`,
  `deno eval`, `ruby -e` etc. can issue any of the above. Either gate
  these exec forms or accept that URL/host matching must also cover
  their argument strings.

## The more robust angle: cut the credentials, not the commands

Command matching is a denylist and denylists leak. The structural fix
(cf. the stricttest philosophy already used for test isolation) is to
make the write **impossible**, not merely un-typed:

- Don't expose the GitHub token to agent sessions at all unless a task
  needs it; the release path can inject it transiently for exactly the
  step that needs it, the way CI does. No ambient `GH_TOKEN` /
  `~/.config/gh` reachable from a normal session → `gh api` DELETE has
  no credential to authenticate with.
- Same for `~/.npmrc` `_authToken` and any PyPI token: absent from the
  session environment except during a sanctioned publish.
- This converts "please don't use the open gate" into "there is no
  key," which is the guarantee the git-transport guards are trying to
  approximate and the only kind that survives a novel tool.

## Affected

Wherever the permission rules live (the profile `settings.json`
`permissions.deny`/`ask` arrays and/or the shared hooks in
`~/.claudewheel/shared-settings.json`), plus any credential-injection
mechanism for releases. A real design pass is needed to decide the
command-matching vs credential-scoping split — likely both: gate the
obvious `gh`/`curl` mutations for fast, legible refusals, and scope the
tokens out of ambient reach as the backstop.

## Provenance

Surfaced 2026-08-11 when an agent, asked to delete a remote ref,
had every `git push` spelling correctly blocked and then deleted the
ref via `gh api -X DELETE` — demonstrating the guard's blind spot in a
single command.
