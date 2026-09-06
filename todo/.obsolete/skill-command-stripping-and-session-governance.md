# Skill/command stripping and session-state governance

## Context

claudewheel strips tools via `--disallowedTools`, but Claude Code's bundled
skills and slash commands are a separate surface with their own disable
mechanisms, currently unmanaged by the canonical model. Sessions also
accumulate invisible per-profile state (scheduled jobs) with no launcher-side
inventory. Facts below were verified empirically against Claude Code 2.1.236.

## 1. Bring skill/command disabling into the canonical model

Verified mechanics:

- `"disableBundledSkills": true` (settings) removes bundled skills and bundled
  workflows entirely. `/doctor` alone survives (hardcoded survivor) and needs
  `skillOverrides: {"doctor": "off"}` or `DISABLE_DOCTOR_COMMAND=1`.
- `"skillOverrides": {"<name>": "off"}` removes built-in prompt commands
  (`init`, `security-review`, `insights`, `team-onboarding`, `statusline`,
  `auto-mode-setup`): hidden from the model AND not typable.
- `"enableArtifact": false` removes the Artifact tool and every
  artifact-family skill; per the docs, a `false` in any scope wins and nothing
  re-enables it.
- `"disableRemoteControl": true` removes the Remote Control bridge (the
  claude.ai / mobile pairing feature) entirely.
- `permissions.deny: ["Skill(name)"]` is NOT a substitute: it blocks model
  invocation only; the command stays advertised and typable.
- With the Skill tool already stripped via `disallowedTools`, the skill
  listing costs zero tokens — the gain here is blocking typed invocation
  (typing `/code-review` otherwise still injects its skill prompt; the
  user-typed path never consults the tool list) plus menu decluttering.
- Native commands (`/clear`, `/compact`, `/model`, `/design`, `/recap`,
  `/goal`, ...) have no removal mechanism; they are inert until typed, which
  is tolerable.

Open decision to settle first: blanket vs surgical. The blanket switch also
kills `/deep-research` (the one skill that is a sandboxed workflow program,
not prose) and `/loop` (a thin front for the CronCreate/CronDelete/CronList
tools, which the strip list deliberately keeps). If either should survive,
use per-skill `"off"` overrides for the unwanted names instead of the blanket
key.

Mechanics: these are ordinary profile `settings.json` keys. The reconcile core
currently manages only hooks, `disallowedTools`, and `permissions` — extending
the canonical model means defaults, reconcile, health drift detection, and
wizard defaults all learn the new keys. Version floor: full
`skillOverrides: "off"` behavior needs Claude Code >= 2.1.205; a health check
could assert the installed versions satisfy it.

Affected: `claudewheel/defaults.py`, `claudewheel/guardrail.py` (if these join
the canonical model proper), `claudewheel/reconcile.py`,
`claudewheel/health.py`, `claudewheel/wizard.py`, docs.
Effort: medium.

## 2. Scheduled-jobs inventory

Cron jobs created by sessions are invisible per-profile state. A `health`
section or a dedicated screen enumerating active jobs — what fires, when, with
what prompt — turns the timer store into something reviewable. Read-only
first; stop/delete controls are a later step. Requires locating where the
harness stores jobs per version (investigate; storage location may shift
across versions, which is an argument for the read path living behind one
module).
Effort: small-medium.

## 3. Session forensics command

`claudewheel inspect-session <id>`: read a session's JSONL from the shared
store and summarize errors, token burn, tool-call anomalies, and stuck/retry
patterns. Complements the sessions overview screen (which lists sessions; this
analyzes one). Launcher-side analysis keeps working regardless of what the
harness's own debug tooling does across versions.
Effort: medium.
