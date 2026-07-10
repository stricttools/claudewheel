"""Tests for state.json value helpers and save_launch_state() in claudewheel.state."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.state import (
    AUTH_BROWSER_KEY,
    load_state_value,
    record_inode,
    save_launch_state,
    save_state_value,
)


class StateFileTestCase(unittest.TestCase):
    """Base class providing a temp directory and patched STATE_FILE."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_file = self.tmp_path / "state.json"
        self._patch = patch("claudewheel.state.STATE_FILE", self.state_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _read(self) -> dict:
        return json.loads(self.state_file.read_text())


# ---------------------------------------------------------------------------
# load_state_value / save_state_value
# ---------------------------------------------------------------------------


class LoadStateValueTests(StateFileTestCase):
    """Tests for state.load_state_value()."""

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(load_state_value("anything"))

    def test_missing_key_returns_none(self) -> None:
        self.state_file.write_text(json.dumps({"other": 1}))
        self.assertIsNone(load_state_value("anything"))

    def test_reads_existing_key(self) -> None:
        self.state_file.write_text(json.dumps({"auth_browser": "/usr/bin/ff"}))
        self.assertEqual(load_state_value("auth_browser"), "/usr/bin/ff")

    def test_corrupt_json_returns_none(self) -> None:
        self.state_file.write_text("{not json")
        self.assertIsNone(load_state_value("anything"))

    def test_non_dict_json_returns_none(self) -> None:
        self.state_file.write_text(json.dumps(["a", "list"]))
        self.assertIsNone(load_state_value("anything"))

    def test_reads_fresh_from_disk(self) -> None:
        """Each call re-reads the file -- no in-memory caching."""
        self.state_file.write_text(json.dumps({"k": "v1"}))
        self.assertEqual(load_state_value("k"), "v1")
        self.state_file.write_text(json.dumps({"k": "v2"}))
        self.assertEqual(load_state_value("k"), "v2")


class SaveStateValueTests(StateFileTestCase):
    """Tests for state.save_state_value()."""

    def test_creates_file_and_parents(self) -> None:
        nested = self.tmp_path / "deep" / "state.json"
        with patch("claudewheel.state.STATE_FILE", nested):
            save_state_value("k", "v")
        self.assertEqual(json.loads(nested.read_text()), {"k": "v"})

    def test_roundtrip(self) -> None:
        save_state_value("auth_browser", "copy")
        self.assertEqual(load_state_value("auth_browser"), "copy")

    def test_preserves_other_keys(self) -> None:
        self.state_file.write_text(
            json.dumps({"launch_count": 3, "recent_dirs": ["/x"]}))
        save_state_value("auth_browser", "/usr/bin/ff")
        self.assertEqual(self._read(), {
            "launch_count": 3,
            "recent_dirs": ["/x"],
            "auth_browser": "/usr/bin/ff",
        })

    def test_overwrites_existing_value(self) -> None:
        save_state_value("k", "old")
        save_state_value("k", "new")
        self.assertEqual(self._read(), {"k": "new"})

    def test_corrupt_file_starts_fresh(self) -> None:
        self.state_file.write_text("{not json")
        save_state_value("k", "v")
        self.assertEqual(self._read(), {"k": "v"})

    def test_no_tmp_file_left_behind(self) -> None:
        save_state_value("k", "v")
        self.assertFalse((self.tmp_path / "state.tmp").exists())

    def test_preserves_target_file_mode(self) -> None:
        """The atomic tmp-swap must preserve the existing file's permissions
        (the tmp file is created with umask-default perms and its mode wins
        after rename). Regression test for the tmp-swap perms bug."""
        old_umask = os.umask(0o022)  # pin umask so the tmp file defaults 0644
        self.addCleanup(os.umask, old_umask)
        self.state_file.write_text(json.dumps({"k": "old"}))
        self.state_file.chmod(0o640)

        save_state_value("k", "new")

        mode = self.state_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o640)


# ---------------------------------------------------------------------------
# record_inode permissions
# ---------------------------------------------------------------------------


class RecordInodePermissionTests(unittest.TestCase):
    """Mode-preservation test for record_inode()'s atomic tmp-swap.

    Functional record_inode() coverage lives in tests/test_inode.py; this
    class only covers the tmp-swap perms bug alongside its state.py siblings.
    """

    def test_preserves_target_file_mode(self) -> None:
        old_umask = os.umask(0o022)  # pin umask so the tmp file defaults 0644
        self.addCleanup(os.umask, old_umask)
        with tempfile.TemporaryDirectory() as tmp:
            inodes_file = Path(tmp) / "inodes.json"
            inodes_file.write_text(json.dumps({"/stale/path": 12345}) + "\n")
            inodes_file.chmod(0o640)
            project_dir = Path(tmp) / "proj"
            project_dir.mkdir()

            # record_inode derives the inodes file from SharedStore(SHARED_DIR),
            # so patch state.SHARED_DIR -> store.inodes_file == inodes_file.
            with patch("claudewheel.state.SHARED_DIR", Path(tmp)):
                record_inode(str(project_dir))

            # The write happened (new mapping recorded) ...
            data = json.loads(inodes_file.read_text())
            self.assertIn(os.path.abspath(str(project_dir)), data)
            # ... and the pre-existing mode survived the swap.
            mode = inodes_file.stat().st_mode & 0o777
            self.assertEqual(mode, 0o640)


# ---------------------------------------------------------------------------
# save_launch_state
# ---------------------------------------------------------------------------


class _StubCfg:
    """Minimal ConfigManager stand-in: a state dict plus save_state().

    Mirrors ConfigManager.save_state()'s out-of-band merge logic so that
    tests exercise the same contract (auth_browser survives clobber).
    """

    def __init__(self, state_file: Path, state: dict | None = None) -> None:
        self._state_file = state_file
        self.state: dict = state if state is not None else {}

    def save_state(self) -> None:
        # Merge out-of-band keys (same as ConfigManager.save_state)
        try:
            on_disk = json.loads(self._state_file.read_text())
            if isinstance(on_disk, dict):
                browser = on_disk.get("auth_browser")
                if browser is not None:
                    self.state["auth_browser"] = browser
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        self._state_file.write_text(json.dumps(self.state, indent=2) + "\n")


class SaveLaunchStateTests(StateFileTestCase):
    """Tests for state.save_launch_state()."""

    def _cfg(self, state: dict | None = None) -> _StubCfg:
        return _StubCfg(self.state_file, state)

    def test_saves_non_none_selections(self) -> None:
        cfg = self._cfg()
        save_launch_state(cfg, {"model": "opus", "profile": None})
        self.assertEqual(self._read()["last_config"], {"model": "opus"})

    def test_increments_launch_count(self) -> None:
        cfg = self._cfg({"launch_count": 4})
        save_launch_state(cfg, {})
        self.assertEqual(self._read()["launch_count"], 5)

    def test_recent_dirs_dedup_and_front_insert(self) -> None:
        cfg = self._cfg({"recent_dirs": ["/a", "/b"]})
        save_launch_state(cfg, {"directory": "/b"})
        self.assertEqual(self._read()["recent_dirs"], ["/b", "/a"])

    def test_recent_dirs_capped_at_20(self) -> None:
        cfg = self._cfg({"recent_dirs": [f"/d{i}" for i in range(20)]})
        save_launch_state(cfg, {"directory": "/new"})
        recent = self._read()["recent_dirs"]
        self.assertEqual(len(recent), 20)
        self.assertEqual(recent[0], "/new")

    def test_preserves_auth_browser_written_out_of_band(self) -> None:
        """Regression: the auth wizard writes auth_browser straight to disk
        while the TUI holds a stale in-memory state; save_launch_state must
        not clobber it."""
        cfg = self._cfg({"launch_count": 0})  # in-memory state predates the write
        save_state_value(AUTH_BROWSER_KEY, "/usr/bin/ff")
        save_launch_state(cfg, {"model": "opus"})
        self.assertEqual(self._read()[AUTH_BROWSER_KEY], "/usr/bin/ff")

    def test_no_auth_browser_key_when_absent_on_disk(self) -> None:
        cfg = self._cfg()
        save_launch_state(cfg, {})
        self.assertNotIn(AUTH_BROWSER_KEY, self._read())

    def test_in_memory_auth_browser_kept_when_disk_missing(self) -> None:
        """An auth_browser already in cfg.state survives when the disk file
        doesn't exist yet (disk read returns None -> no overwrite)."""
        cfg = self._cfg({AUTH_BROWSER_KEY: "copy"})
        save_launch_state(cfg, {})
        self.assertEqual(self._read()[AUTH_BROWSER_KEY], "copy")


# ---------------------------------------------------------------------------
# ConfigManager.save_state out-of-band merge
# ---------------------------------------------------------------------------


class ConfigManagerSaveStateMergeTests(StateFileTestCase):
    """Test that ConfigManager.save_state() merges out-of-band auth_browser."""

    def _make_cfg(self, state: dict | None = None):
        """Build a real ConfigManager with patched paths to use our temp dir."""
        from claudewheel.config import ConfigManager
        from unittest.mock import patch as _patch

        cfg_dir = self.tmp_path / "cfg"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = object.__new__(ConfigManager)
        cfg.state = state if state is not None else {}
        return cfg

    def test_merges_auth_browser_from_disk(self) -> None:
        """save_state picks up auth_browser written by an external process."""
        from claudewheel.config import ConfigManager
        from unittest.mock import patch as _patch

        cfg = object.__new__(ConfigManager)
        cfg.state = {"launch_count": 5}

        # Simulate out-of-band write by auth wizard
        self.state_file.write_text(json.dumps({"auth_browser": "/usr/bin/chrome"}))

        # Patch STATE_FILE in the config module too (save_state reads it)
        with _patch("claudewheel.config.STATE_FILE", self.state_file):
            cfg.save_state()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk["auth_browser"], "/usr/bin/chrome")
        self.assertEqual(on_disk["launch_count"], 5)

    def test_no_clobber_when_disk_missing(self) -> None:
        """save_state works when state file doesn't exist yet."""
        from claudewheel.config import ConfigManager
        from unittest.mock import patch as _patch

        cfg = object.__new__(ConfigManager)
        cfg.state = {"launch_count": 1}

        with _patch("claudewheel.config.STATE_FILE", self.state_file):
            cfg.save_state()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk, {"launch_count": 1})
        self.assertNotIn("auth_browser", on_disk)

    def test_in_memory_auth_browser_not_clobbered_by_disk_none(self) -> None:
        """If auth_browser is already in memory and not on disk, it survives."""
        from claudewheel.config import ConfigManager
        from unittest.mock import patch as _patch

        cfg = object.__new__(ConfigManager)
        cfg.state = {"auth_browser": "copy", "launch_count": 2}

        # Disk file exists but has no auth_browser key
        self.state_file.write_text(json.dumps({"other": "stuff"}))

        with _patch("claudewheel.config.STATE_FILE", self.state_file):
            cfg.save_state()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk["auth_browser"], "copy")


if __name__ == "__main__":
    unittest.main()
