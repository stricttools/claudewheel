"""Tests for the OptionsFile and StateFile atomic accessors."""

from __future__ import annotations

import json

from claudewheel.appdata import OptionsFile, StateFile
from tests.wheelhelpers import SandboxHomeTestCase


class OptionsFileTests(SandboxHomeTestCase):
    """OptionsFile add_pinned / set_metadata / load semantics."""

    def _opts(self) -> OptionsFile:
        return OptionsFile(self.launcher_dir / "options.json")

    def test_load_missing_returns_default(self) -> None:
        of = OptionsFile(self.launcher_dir / "nope.json")
        default = {"model": {"values": ["opus"]}}
        self.assertEqual(of.load(default), default)

    def test_load_corrupt_returns_default(self) -> None:
        path = self.launcher_dir / "bad.json"
        path.write_text("{not json")
        of = OptionsFile(path)
        default = {"x": 1}
        self.assertEqual(of.load(default), default)

    def test_add_pinned_new_segment(self) -> None:
        of = self._opts()
        of.path.write_text(json.dumps({}))
        result = of.add_pinned("profile", "work", {})
        self.assertEqual(result["profile"]["pinned"], ["work"])
        on_disk = json.loads(of.path.read_text())
        self.assertEqual(on_disk["profile"]["pinned"], ["work"])

    def test_add_pinned_missing_file_uses_default(self) -> None:
        of = OptionsFile(self.launcher_dir / "fresh.json")
        default = {"model": {"values": ["opus"], "pinned": []}}
        result = of.add_pinned("model", "custom", default)
        self.assertIn("custom", result["model"]["pinned"])
        self.assertTrue(of.path.exists())

    def test_add_pinned_no_duplicate(self) -> None:
        of = self._opts()
        of.path.write_text(json.dumps({"model": {"values": [], "pinned": ["opus"]}}))
        # Removing the file to prove no write happens on the duplicate path.
        of.add_pinned("model", "opus", {})
        on_disk = json.loads(of.path.read_text())
        self.assertEqual(on_disk["model"]["pinned"], ["opus"])

    def test_add_pinned_duplicate_does_not_rewrite(self) -> None:
        of = self._opts()
        of.path.write_text(json.dumps({"model": {"values": [], "pinned": ["opus"]}}))
        mtime_before = of.path.stat().st_mtime_ns
        result = of.add_pinned("model", "opus", {})
        self.assertEqual(result["model"]["pinned"], ["opus"])
        # No append -> no write -> mtime unchanged.
        self.assertEqual(of.path.stat().st_mtime_ns, mtime_before)

    def test_set_metadata_round_trip(self) -> None:
        of = self._opts()
        of.path.write_text(json.dumps({}))
        meta = {"config_dir": "~/.claudewheel/profiles/work"}
        result = of.set_metadata("profile", "work", meta, {})
        self.assertEqual(result["profile"]["metadata"]["work"], meta)
        on_disk = json.loads(of.path.read_text())
        self.assertEqual(on_disk["profile"]["metadata"]["work"], meta)

    def test_set_metadata_missing_file_uses_default(self) -> None:
        of = OptionsFile(self.launcher_dir / "fresh2.json")
        result = of.set_metadata("profile", "p", {"k": "v"}, {})
        self.assertEqual(result["profile"]["metadata"]["p"], {"k": "v"})
        self.assertEqual(result["profile"]["values"], [])

    def test_write_round_trip(self) -> None:
        of = self._opts()
        data = {"model": {"values": ["opus"], "pinned": ["x"]}}
        of.write(data)
        self.assertEqual(json.loads(of.path.read_text()), data)


class StateFileTests(SandboxHomeTestCase):
    """StateFile save / get_value / set_value semantics."""

    def _sf(self) -> StateFile:
        return StateFile(self.launcher_dir / "state.json")

    def test_save_out_of_band_key_from_disk_wins(self) -> None:
        sf = self._sf()
        # Out-of-band writer put auth_browser on disk after in-memory load.
        sf.path.write_text(json.dumps({"auth_browser": "/usr/bin/chrome"}))
        state = {"launch_count": 5}
        sf.save(state)
        on_disk = json.loads(sf.path.read_text())
        self.assertEqual(on_disk["auth_browser"], "/usr/bin/chrome")
        self.assertEqual(on_disk["launch_count"], 5)
        # Merge also mutated the in-memory dict.
        self.assertEqual(state["auth_browser"], "/usr/bin/chrome")

    def test_save_no_disk_key_keeps_memory(self) -> None:
        sf = self._sf()
        sf.path.write_text(json.dumps({"other": "stuff"}))
        state = {"auth_browser": "copy", "launch_count": 2}
        sf.save(state)
        on_disk = json.loads(sf.path.read_text())
        self.assertEqual(on_disk["auth_browser"], "copy")

    def test_save_missing_disk_file(self) -> None:
        sf = StateFile(self.launcher_dir / "no_state.json")
        state = {"launch_count": 1}
        sf.save(state)
        on_disk = json.loads(sf.path.read_text())
        self.assertEqual(on_disk, {"launch_count": 1})

    def test_get_value_round_trip(self) -> None:
        sf = self._sf()
        sf.set_value("k", "v1")
        self.assertEqual(sf.get_value("k"), "v1")
        sf.set_value("k", "v2")
        self.assertEqual(sf.get_value("k"), "v2")

    def test_get_value_missing_file(self) -> None:
        sf = StateFile(self.launcher_dir / "absent.json")
        self.assertIsNone(sf.get_value("k"))
        self.assertEqual(sf.get_value("k", "fallback"), "fallback")

    def test_get_value_corrupt_file(self) -> None:
        sf = self._sf()
        sf.path.write_text("{broken")
        self.assertIsNone(sf.get_value("k"))

    def test_set_value_preserves_other_keys(self) -> None:
        sf = self._sf()
        sf.path.write_text(json.dumps({"a": 1, "b": 2}))
        sf.set_value("b", 99)
        on_disk = json.loads(sf.path.read_text())
        self.assertEqual(on_disk, {"a": 1, "b": 99})

    def test_write_never_truncated(self) -> None:
        # Atomic write: after set_value the file is always complete/parseable.
        sf = self._sf()
        for i in range(50):
            sf.set_value("counter", i)
            data = json.loads(sf.path.read_text())
            self.assertEqual(data["counter"], i)
