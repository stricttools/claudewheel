"""Tests for state.json value helpers and save_launch_state() in claudewheel.state."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from claudewheel.appdata import StateFile
from claudewheel.shared_store import SharedStore
from claudewheel.state import (
    AUTH_BROWSER_KEY,
    record_inode,
    save_launch_state,
)


class StateFileTestCase(unittest.TestCase):
    """Base class providing a temp directory and a state.json path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_file = self.tmp_path / "state.json"

    def _read(self) -> dict:
        return json.loads(self.state_file.read_text())


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

            # record_inode derives the inodes file from the SharedStore's
            # shared_dir (inodes.json lives in the shared store).
            store = SharedStore(Path(tmp), Path(tmp) / "skills")
            record_inode(store, str(project_dir))

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
    """Minimal AppConfigStore stand-in: a state dict plus save_state().

    Mirrors AppConfigStore.save_state()'s out-of-band merge logic so that
    tests exercise the same contract (auth_browser survives clobber).
    """

    def __init__(self, state_file: Path, state: dict | None = None) -> None:
        self._state_file = state_file
        self.state: dict = state if state is not None else {}

    def save_state(self) -> None:
        # Merge out-of-band keys (same as AppConfigStore.save_state)
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
        StateFile(self.state_file).set_value(AUTH_BROWSER_KEY, "/usr/bin/ff")
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
# AppConfigStore.save_state out-of-band merge
# ---------------------------------------------------------------------------


class AppConfigStoreSaveStateMergeTests(StateFileTestCase):
    """Test that AppConfigStore.save_state() merges out-of-band auth_browser.

    save_state() now targets the workspace-derived ``self._state_file``, so
    tests wire that instance attribute directly instead of patching a module
    constant.
    """

    def _make_cfg(self, state: dict | None = None):
        """Build a bare AppConfigStore wired to our temp state file."""
        from claudewheel.config import AppConfigStore

        cfg = object.__new__(AppConfigStore)
        cfg._state_file = self.state_file
        cfg.state = state if state is not None else {}
        return cfg

    def test_merges_auth_browser_from_disk(self) -> None:
        """save_state picks up auth_browser written by an external process."""
        cfg = self._make_cfg({"launch_count": 5})

        # Simulate out-of-band write by auth wizard
        self.state_file.write_text(json.dumps({"auth_browser": "/usr/bin/chrome"}))

        cfg.save_state()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk["auth_browser"], "/usr/bin/chrome")
        self.assertEqual(on_disk["launch_count"], 5)

    def test_no_clobber_when_disk_missing(self) -> None:
        """save_state works when state file doesn't exist yet."""
        cfg = self._make_cfg({"launch_count": 1})

        cfg.save_state()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk, {"launch_count": 1})
        self.assertNotIn("auth_browser", on_disk)

    def test_in_memory_auth_browser_not_clobbered_by_disk_none(self) -> None:
        """If auth_browser is already in memory and not on disk, it survives."""
        cfg = self._make_cfg({"auth_browser": "copy", "launch_count": 2})

        # Disk file exists but has no auth_browser key
        self.state_file.write_text(json.dumps({"other": "stuff"}))

        cfg.save_state()

        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk["auth_browser"], "copy")


if __name__ == "__main__":
    unittest.main()
