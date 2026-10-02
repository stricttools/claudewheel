# Root commands and package transactions run blind: no preview, -y by reflex, passwords in argv

## Context

A Claude Code session on this laptop (Fedora 44, profile `emergency`, started
with `--dangerously-skip-permissions`) was hardening the machine's NVIDIA
setup with root access the user had granted by typing the sudo password into
the chat. In one short stretch it did the following, each step looking
reasonable on its own:

1. **A removal it believed was isolated.** To stop RPM Fusion's
   `nvidia-settings` login autostart, the agent proposed uninstalling the
   package, telling the user "nothing on the system requires or recommends
   it". The evidence was `rpm -q --whatrequires nvidia-settings` and
   `rpm -q --whatrecommends nvidia-settings`, both empty. That was wrong:
   `dnf repoquery --whatrequires nvidia-settings` lists
   `xorg-x11-drv-nvidia`, the driver itself.
2. **`-y` instead of a preview.** The agent cannot answer interactive
   prompts, so it ran `sudo dnf remove -y nvidia-settings`. `-y` skipped the
   one screen that would have shown the problem: the transaction summary.
3. **dnf removed 36 packages.** The requirement pulled out the whole NVIDIA
   stack (`xorg-x11-drv-nvidia`, its libs, CUDA, and power packages,
   `akmod-nvidia`, both installed `kmod-nvidia` builds, `nvidia-modprobe`,
   `nvidia-persistenced`), and dnf's removal of unused dependencies
   (reason `Clean` in `dnf history info`) swept away akmods, kmodtool,
   kernel-devel for two kernels, bison, flex, openssl, rpmdevtools, and
   more. The on-demand GPU tooling stopped working.
4. **The repair also ran blind.** The agent reinstalled with `dnf install -y`
   and no preview. The locally built `kmod-nvidia` packages are not in any
   repository; they came back only because akmods keeps them in
   `/var/cache/akmods/nvidia/` (`dnf history undo` would have failed on
   them). `kernel-devel-matched` for the running kernel was no longer in the
   repositories, so dnf satisfied akmods with a newer one, and through it
   installed **a partial new kernel**: `kernel-core` and
   `kernel-modules-core` only, without `kernel-modules` and
   `kernel-modules-extra`. It also removed the oldest installed kernel
   (install-only limit) and **made the partial kernel the boot default**.
   That kernel lacks the Wi-Fi driver (`mt7921e`, checked with `modinfo -k`)
   and has no NVIDIA module. The next boot would have come up without
   Wi-Fi. The agent noticed only because it checked afterwards, and set the
   default back with `grubby --set-default`.
5. **The password travelled in argv.** Every root command was written as
   `printf '%s\n' '<password>' | sudo -S ...` inside the Bash tool's command
   string. The password is therefore in the command line of the shell
   processes (readable by every user through `/proc/<pid>/cmdline` while they
   run), in the session transcript, and in anything that records process
   command lines: a root-only exec-recording log installed on this machine
   the same day captured it in full.
6. **Guardrail regexes matched text, not commands.** The `rm` hard-deny rule
   (generated into `~/.claudewheel/scripts/hook-block-unsafe-commands` from
   `claudewheel/guardrail.py`) refused commands that only contained the
   characters `|rm ` or `rm -rf` inside a quoted string: a read-only
   `grep -E "sleep|rm |sudo" <file>`, and a Python heredoc whose string
   literal held a shell line. The agent had to restructure harmless commands
   to get past it, which teaches agents to route around the hook.

The written rules already covered the core of this ("Batch operations: the
dry run runs first, its output is examined, only then the operation runs";
"premises are unverified until probed"). They did not stop it: nothing
mechanical connects "this command mutates the system" to "a preview was read
first", and an agent that cannot answer a prompt reaches for `-y` by reflex.

## Problems, one per aspect

1. **No mechanical preview for package transactions.** dnf, rpm, and flatpak
   mutations run with `-y`/`--assumeyes` and no prior `--assumeno` preview.
   What the agent believed would happen and what dnf does are never compared.
2. **What was previewed is not what runs.** Even with a manual
   `--assumeno` first, the later `-y` run resolves again and can differ
   (repositories refresh, another session installs something).
3. **Unbounded side effects of a transaction are not flagged:** dependency
   removals (`Clean`, `Dependency` reasons), kernel installs and removals,
   a changed boot default, enabled or masked units from package scriptlets.
4. **Wrong evidence for "nothing depends on X".** `rpm -q --whatrequires`
   answers a narrower question than dnf's solver; an agent quoting it as a
   premise misleads the user.
5. **Root commands are not classified at all in this profile.** The `sudo`
   rule is an `ask` glob (`Bash(sudo:*)` in `guardrail.py`), which
   `--dangerously-skip-permissions` disables, so only hooks act, and no hook
   covers package managers, `grubby --set-default`, `systemctl mask`, or
   writes under `/etc`.
6. **The sudo password is handled as a command-line string** (argv,
   transcript, process logs).
7. **Hook rules match raw text**, producing false refusals on quoted
   strings and heredocs.
8. **Recovery is not planned before the risky step.** Nobody checked, before
   removing, that the removed packages could be reinstalled (locally built
   kmods, a `kernel-devel-matched` no longer published).

## Solutions to consider

1. **A preview-then-apply wrapper for package transactions**, shipped with
   claudewheel like `heavy` (`~/.claudewheel/scripts/`):
   `pkgtx plan <dnf arguments>` runs the transaction with `--assumeno` and
   dnf5's `--store=<dir>` (saves the resolved transaction; unverified on
   this dnf5 version), prints the summary grouped by reason (requested,
   dependency, unused-dependency cleanup, kernel), and records the boot
   default; `pkgtx apply <plan id>` runs `dnf5 replay <dir>` so the stored
   transaction runs as previewed, then compares the boot default and the
   kernel set before and after and fails loudly on any change the plan did
   not show. A hook hard-denies bare `dnf`/`dnf5`/`yum`/`rpm -e|-i|-U`/
   `flatpak` mutations with a message naming the wrapper.
   - Pro: the preview is structural, and "what ran" equals "what was shown".
   - Con: depends on dnf5's store and replay behaving as documented; a new
     script to maintain.
2. **Guardrail rule only:** hard-deny any package-manager mutation carrying
   `-y`/`--assumeyes`, with advice to run `--assumeno` first and relay the
   summary to the user.
   - Pro: tiny change in `guardrail.py`.
   - Con: leaves the agent no way to apply without a prompt, and does not
     bind the run to the preview.
3. **Classify system mutations as consequential:** package transactions,
   `grubby --set-default`/`--update-kernel`, `systemctl mask|enable|disable`
   for system units, and writes under `/etc` need the user's explicit
   approval in the chat, with the preview's summary in the approval request.
   Combines with 1 or 2.
4. **Password handling:** a `SUDO_ASKPASS` helper reading the password from
   the user's keyring (`secret-tool`), set up once by the user, so agents run
   `sudo -A ...` and the password never enters argv or the transcript; plus
   a hard-deny rule on `sudo -S` fed from a literal (`printf ... | sudo -S`,
   `echo ... | sudo -S`). Alternatives: narrow `NOPASSWD` sudoers rules for
   the commands a task needs, or a sudo timestamp the user refreshes.
5. **Parse commands instead of grepping them:** match guardrail rules
   against a shell parse of the command (command words, with quoted strings
   and heredoc bodies as data), so `grep "rm "` and a heredoc mentioning
   `rm -rf` are not refusals while `sudo rm`, `xargs rm`, and `find -exec rm`
   still are.
6. **A recovery check before removals:** the wrapper's plan step lists
   removed packages that no enabled repository can reinstall (locally built
   kmods, `kernel-devel-matched` versions no longer published) and refuses to
   apply until the user approves that specific loss.

## Affected files

- `claudewheel/guardrail.py` (the canonical guardrail model) and the
  generated `hook-block-unsafe-commands`.
- A new wrapper script beside `scripts/heavy` and its install path into
  profiles.
- Profile settings templates that register hooks, and the docs describing
  the guardrails.

## Effort

The wrapper with plan, apply, and the before/after checks: about a day,
plus probing dnf5's `--store` and `replay`. The guardrail rules and the
askpass helper: a few hours each. Parsing commands instead of grepping: about
a day, depending on the parser chosen.
