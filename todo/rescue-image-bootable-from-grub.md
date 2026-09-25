# A rescue image, bootable from the GRUB menu, that runs Claude Code

## Context

On a Linux machine whose desktop can crash or hang (for example because of a
GPU driver fault), the moment Claude Code is most useful for diagnosis is the
moment the normal system cannot be trusted to boot into a desktop, or cannot
boot at all. A text-mode boot entry of the installed distribution (booting to
`multi-user.target`, with no display server) covers the case where the
installed system still boots. It does not cover the case where the installed
system itself is broken: a bad kernel update, a broken initramfs, a filesystem
the normal boot refuses to mount, or a driver that hangs early boot.

GRUB itself cannot host Claude Code. It has networking and plain HTTP, but no
TLS, so it cannot reach the Anthropic API, and its script language has no way
to run an agent loop. The practical route is a separate, tiny Linux system
that GRUB boots from its own menu entry.

## Proposal

claudewheel builds and maintains a small Alpine Linux image (kernel plus
initramfs, running entirely from RAM) that the machine's GRUB menu can boot,
and that launches Claude Code with a chosen claudewheel profile.

Alpine fits because it is built on musl and BusyBox, boots into RAM in
seconds, and packages what the image needs: `wpa_supplicant` and Wi-Fi
firmware, CA certificates, `btrfs-progs`, `e2fsprogs`, and `cryptsetup`.

What the image would contain:
- a kernel and initramfs from Alpine's `linux-lts`, plus firmware for the
  machine's network card;
- networking: DHCP on Ethernet, and Wi-Fi with credentials supplied at build
  time or typed at boot;
- the Claude Code native binary in its musl build (needs verification: from
  memory, Anthropic ships a musl build and documents `libgcc`, `libstdc++`,
  and `ripgrep` as the Alpine requirements);
- claudewheel itself, or the minimum needed to launch Claude Code with a
  profile's `CLAUDE_CONFIG_DIR`;
- a boot script that mounts the installed system's disks read-only, so the
  session can read the journal of a failed boot, logs, and configuration
  without being able to change anything unless the user remounts read-write.

claudewheel's part:
- a command that builds (and rebuilds) the image for this machine, so the
  frozen image does not drift behind Claude Code releases;
- a command that installs the image into `/boot` and writes a GRUB boot entry
  for it (needs root), and one that removes both;
- a decision about how the image obtains credentials (see below).

## Open questions

1. **Credentials.** Either the image reads the profile's config directory from
   the installed system's disk at boot (secrets never leave the encrypted or
   at least non-`/boot` storage, but this fails if that disk is unreadable), or
   the image carries a copy (works when the disk is broken, but places an OAuth
   token on the unencrypted `/boot` partition). Possibly an explicit per-build
   choice between the two.
2. **Wi-Fi credentials** have the same trade-off as the Claude credentials.
3. **Encrypted root filesystems** need `cryptsetup` and a passphrase prompt in
   the image.
4. **Secure Boot.** An unsigned kernel will not boot with Secure Boot enabled.
   Options: sign with a machine owner key, or document the requirement.
5. **Size.** Roughly 100 to 200 MB with firmware and Claude Code; `/boot` must
   have room for it next to the distribution's kernels.
6. **Terminal.** The Linux text console has 16 colors, no mouse wheel without
   `gpm`, and a limited font. Check how Claude Code's interface renders there,
   and whether a larger console font should be part of the image.
7. **Distribution scope.** GRUB with Boot Loader Specification entries
   (Fedora) and GRUB with a generated `grub.cfg` (Debian, Arch) need different
   entry installation; systemd-boot is a third case.

## Alternatives considered

| Alternative | For | Against |
| --- | --- | --- |
| Text-mode boot entry of the installed distribution only | Hours of work, uses the normal Claude Code and profiles unchanged | Useless when the installed system does not boot |
| Alpine image (this proposal) | Independent of the installed system, boots in seconds, packaged Wi-Fi firmware and filesystem tools | Days of work, credentials and update questions |
| Buildroot image built from source | Smallest possible image, full control | Much more build machinery to maintain |
| Tiny Core Linux | About 16 MB | Unusual package system, thin package selection |
| A static Go client instead of Claude Code inside the image | No musl or glibc question, tiny | A second agent implementation, not Claude Code |
| Code running inside GRUB or as a UEFI application | No separate OS | Needs a TLS stack, HTTP streaming, JSON, and a UI written for a bootloader: months of work |

## Effort

A few days for a first working image on one machine (Fedora, GRUB with Boot
Loader Specification entries, unencrypted btrfs, Wi-Fi), plus the credential
decision. Supporting other distributions and boot loaders is additional work
per case.
