"""Tests for the plugin-tree inventory and the opt-in purge."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claudewheel import plugins


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name)

    def _populate(self) -> Path:
        root = self.config_dir / plugins.PLUGINS_DIRNAME
        (root / "marketplaces" / "claude-plugins-official" / ".claude-plugin").mkdir(
            parents=True
        )
        (root / "marketplaces" / "claude-plugins-official" / "README.md").write_text(
            "x" * 100
        )
        for name in ("swift-lsp", "rust-analyzer-lsp", "typescript-lsp"):
            (root / "cache" / "claude-plugins-official" / name).mkdir(parents=True)
            (
                root / "cache" / "claude-plugins-official" / name / "plugin.json"
            ).write_text("{}")
            (root / "data" / f"{name}-claude-plugins-official").mkdir(parents=True)
        (root / "installed_plugins.json").write_text("{}")
        return root

    def test_a_profile_with_no_plugin_tree(self) -> None:
        found = plugins.inventory(self.config_dir)
        self.assertFalse(found.exists)
        self.assertEqual(found.marketplaces, ())
        self.assertEqual(found.plugins, ())
        self.assertEqual(found.size_bytes, 0)

    def test_the_marketplace_clone_and_the_plugins_are_named(self) -> None:
        self._populate()
        found = plugins.inventory(self.config_dir)
        self.assertTrue(found.exists)
        self.assertEqual(found.marketplaces, ("claude-plugins-official",))
        self.assertEqual(
            found.plugins,
            ("rust-analyzer-lsp", "swift-lsp", "typescript-lsp"),
        )
        self.assertGreater(found.size_bytes, 0)

    def test_the_inventory_reads_and_removes_nothing(self) -> None:
        root = self._populate()
        plugins.inventory(self.config_dir)
        self.assertTrue(root.is_dir())
        self.assertTrue((root / "installed_plugins.json").exists())

    def test_the_size_is_the_tree_it_walked(self) -> None:
        root = self._populate()
        by_hand = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
        self.assertEqual(plugins.inventory(self.config_dir).size_bytes, by_hand)

    def test_a_symlinked_plugin_tree_is_not_followed(self) -> None:
        """A profile whose plugins/ is a link points at something it does not
        own; sizing it by following the link would report someone else's data."""
        elsewhere = self.config_dir / "elsewhere"
        (elsewhere / "cache").mkdir(parents=True)
        (elsewhere / "cache" / "big").write_text("x" * 5000)
        (self.config_dir / plugins.PLUGINS_DIRNAME).symlink_to(elsewhere)

        found = plugins.inventory(self.config_dir)
        self.assertFalse(found.exists)
        self.assertEqual(found.size_bytes, 0)


class PurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name)

    def test_purging_removes_the_whole_tree(self) -> None:
        root = self.config_dir / plugins.PLUGINS_DIRNAME
        (root / "cache" / "x").mkdir(parents=True)
        (root / "installed_plugins.json").write_text("{}")

        self.assertTrue(plugins.purge(self.config_dir))
        self.assertFalse(root.exists())
        # Nothing else in the profile is touched.
        self.assertTrue(self.config_dir.is_dir())

    def test_purging_a_profile_with_no_tree_is_a_no_op(self) -> None:
        self.assertFalse(plugins.purge(self.config_dir))

    def test_a_symlinked_tree_is_left_alone(self) -> None:
        elsewhere = self.config_dir / "elsewhere"
        elsewhere.mkdir()
        link = self.config_dir / plugins.PLUGINS_DIRNAME
        link.symlink_to(elsewhere)

        self.assertFalse(plugins.purge(self.config_dir))
        self.assertTrue(link.is_symlink())
        self.assertTrue(elsewhere.is_dir())


if __name__ == "__main__":
    unittest.main()
