# The `sleep` denial does not match wrapper spellings

Informational. No decision has been made about whether or how to close this;
it records the gap so a future session can decide.

## Context

The pre-tool-use hook hard-denies `sleep` as a shell command word in a Bash
tool call: at the start of the command line, after a separator (`&&`, `;`,
`|`), and when it begins a subshell. The denial answers with a steering
message saying the harness notifies on completion, so waiting with `sleep` is
never needed. The permission rule `Bash(sleep:*)` covers only a command line
that starts with `sleep`; the hook widens that to the separator and subshell
positions. See `claudewheel/guardrail.py`, the `sleep` entry in the hard-deny
rules.

## The gap

Spellings that wrap `sleep` in another command pass through the hook
unmatched. Observed and reasoned examples:

- `bash -c "sleep 30"` and `sh -c 'sleep 30'` (the word sits inside a quoted
  string argument to another command)
- `timeout 40 sleep 30`, `nice sleep 30`, `env sleep 30` (a prefix command
  whose argument is the real command)
- `python -c "import time; time.sleep(30)"` and equivalent one-liners in other
  languages (the sleep is program text, not a shell word)
- a project script that sleeps internally (not visible on the command line at
  all)

An agent denied on the plain spelling may reach for the next spelling that
works, so the rule the hook enforces ("never wait with sleep") is only partly
mechanical today.

## Considerations for whoever decides

- Prefix commands and quoted `-c` strings are shell-level and can be matched
  the same way separators are matched now, with a test per form.
- Language one-liners are program text; matching them starts pattern-matching
  code and will produce false denials on legitimate commands.
- Scripts that sleep internally are not detectable from the command line.

## Affected files

- `claudewheel/guardrail.py` (the hard-deny rule and its command matcher)
- the guardrail tests beside it
