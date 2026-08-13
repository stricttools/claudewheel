"""Claude Code's plugin tree inside a profile: inventory it, and remove it.

Left alone, Claude Code clones the official plugin marketplace into a profile
on first launch and installs three language-server plugins from it -- six to
ten megabytes per profile, the clone being nearly all of it, for something no
claudewheel profile asked for.  New launches no longer collect it (the launch
environment suppresses the auto-install), but a profile that already has the
tree keeps it.

Removing those is deliberately NOT part of the canonical settings
reconciliation.  That reconciliation is exact by design -- it rewrites each
managed target to the canonical model on every run -- so folding a plugin purge
into it would mean deleting plugin state every time anyone reconciled anything,
including state a user installed on purpose.  This is a separate opt-in
operation instead, and it names what it is about to remove first.

The layout, as observed on real profiles::

    <config_dir>/plugins/
      marketplaces/<marketplace>/     the git clone -- the megabytes
      cache/<marketplace>/<plugin>/   the installed plugin
      data/<plugin>-<marketplace>/    per-plugin state
      installed_plugins.json, known_marketplaces.json, ...

A ``plugins`` entry that is a symlink is not this tree: it points at something
the profile does not own, and neither the size nor the removal follows it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import effects

#: The plugin tree's directory name inside a Claude Code config dir.
PLUGINS_DIRNAME = "plugins"

#: Where the marketplace clones live, under the plugin tree.
MARKETPLACES_DIRNAME = "marketplaces"

#: Where the installed plugins live, one directory per marketplace.
CACHE_DIRNAME = "cache"


@dataclass(frozen=True)
class PluginInventory:
    """What one profile's plugin tree holds, read without touching it."""

    config_dir: Path
    exists: bool
    marketplaces: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    size_bytes: int = 0

    @property
    def path(self) -> Path:
        """The plugin tree's path, whether or not it exists."""
        return self.config_dir / PLUGINS_DIRNAME


def _names(directory: Path) -> tuple[str, ...]:
    """The sorted names of the subdirectories of *directory*, or nothing."""
    if not directory.is_dir():
        return ()
    try:
        return tuple(sorted(p.name for p in directory.iterdir() if p.is_dir()))
    except OSError:
        return ()


def _tree_size(root: Path) -> int:
    """The bytes of every regular file under *root*, symlinks not followed."""
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def inventory(config_dir: Path) -> PluginInventory:
    """Read *config_dir*'s plugin tree.  Changes nothing.

    A missing tree, and a ``plugins`` entry that is a symlink, both answer "no
    tree here": the second is a pointer at data the profile does not own, and
    reporting its size as the profile's would be reporting someone else's.
    """
    root = Path(config_dir) / PLUGINS_DIRNAME
    if root.is_symlink() or not root.is_dir():
        return PluginInventory(config_dir=Path(config_dir), exists=False)

    marketplaces = _names(root / MARKETPLACES_DIRNAME)
    installed: set[str] = set()
    for marketplace in _names(root / CACHE_DIRNAME):
        installed.update(_names(root / CACHE_DIRNAME / marketplace))
    return PluginInventory(
        config_dir=Path(config_dir),
        exists=True,
        marketplaces=marketplaces,
        plugins=tuple(sorted(installed)),
        size_bytes=_tree_size(root),
    )


def purge(config_dir: Path) -> bool:
    """Remove *config_dir*'s plugin tree.  True when there was one to remove.

    A symlinked ``plugins`` entry is left exactly as it is: following it would
    delete a tree belonging to whatever it points at.
    """
    root = Path(config_dir) / PLUGINS_DIRNAME
    if root.is_symlink() or not root.is_dir():
        return False
    effects.rmtree(root)
    return True
