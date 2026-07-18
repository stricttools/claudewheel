"""Smoke tests for the install module (platform detection, manifest fetch, install)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from claudewheel import install
from claudewheel.binaries import BinaryLocator


class DetectPlatformTests(unittest.TestCase):
    def test_returns_nonempty_hyphenated_string(self) -> None:
        """The detected platform is a non-empty 'os-arch' string."""
        plat = install._detect_platform()
        self.assertIsInstance(plat, str)
        self.assertTrue(plat)
        self.assertIn("-", plat)


class _FakeResponse:
    """Tiny urlopen-result stand-in: supports read([n]) and the context manager protocol."""

    def __init__(self, payload: bytes) -> None:
        self._buf = io.BytesIO(payload)

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            return self._buf.read()
        return self._buf.read(n)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self._buf.close()


class FetchManifestTests(unittest.TestCase):
    def test_404_raises_oserror_with_version_in_message(self) -> None:
        """An HTTP 404 from the server is wrapped as OSError mentioning the version."""
        http_err = urllib.error.HTTPError(
            url="http://example/manifest.json",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            side_effect=http_err,
        ):
            with self.assertRaises(OSError) as ctx:
                install.fetch_manifest("99.99.99")
        self.assertIn("99.99.99", str(ctx.exception))

    def test_valid_manifest_parsed_as_json(self) -> None:
        """A 200 response body is parsed and returned as a dict."""
        manifest = {
            "platforms": {
                "linux-x64": {"checksum": "abc", "binary": "claude", "size": 123}
            }
        }
        payload = json.dumps(manifest).encode("utf-8")
        with mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            return_value=_FakeResponse(payload),
        ):
            result = install.fetch_manifest("2.1.110")
        self.assertEqual(result, manifest)

    def test_invalid_json_body_raises_oserror(self) -> None:
        """Invalid JSON in the response body is normalized to OSError (contract)."""
        payload = b"not-valid-json{{{"
        with mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            return_value=_FakeResponse(payload),
        ):
            with self.assertRaises(OSError) as ctx:
                install.fetch_manifest("2.1.110")
        self.assertIn("2.1.110", str(ctx.exception))

    def test_non_dict_manifest_body_raises_oserror(self) -> None:
        """A valid-JSON body that is not an object is rejected as malformed."""
        payload = json.dumps([1, 2, 3]).encode("utf-8")
        with mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            return_value=_FakeResponse(payload),
        ):
            with self.assertRaises(OSError) as ctx:
                install.fetch_manifest("2.1.110")
        msg = str(ctx.exception)
        self.assertIn("malformed", msg)
        self.assertIn("2.1.110", msg)

    def test_non_dict_platforms_value_raises_oserror(self) -> None:
        """A manifest whose 'platforms' value is not an object is rejected as malformed."""
        payload = json.dumps({"platforms": [1, 2, 3]}).encode("utf-8")
        with mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            return_value=_FakeResponse(payload),
        ):
            with self.assertRaises(OSError) as ctx:
                install.fetch_manifest("2.1.110")
        msg = str(ctx.exception)
        self.assertIn("malformed", msg)
        self.assertIn("2.1.110", msg)


class InstallVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.versions_dir = Path(self._tmp.name) / "versions"
        # Locator points versions_dir at a tmp path -- avoids touching ~/.local/share
        self.locator = BinaryLocator(
            versions_dir=self.versions_dir,
            claude_symlink=Path(self._tmp.name) / "claude",
        )

    def _patch_urlopen_returning(
        self, manifest_payload: bytes, binary_payload: bytes
    ) -> "mock._patch[mock.MagicMock]":
        """urlopen returns the manifest first, then the binary."""

        def side_effect(*args: object, **kwargs: object) -> _FakeResponse:
            # Each successive call returns the next planned response.
            return next(responses)

        responses = iter(
            [_FakeResponse(manifest_payload), _FakeResponse(binary_payload)]
        )
        return mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            side_effect=side_effect,
        )

    def test_successful_install_writes_binary(self) -> None:
        """A correctly-checksummed download lands at VERSIONS_DIR/<version>."""
        plat = install._detect_platform()
        binary_bytes = b"FAKE-CLAUDE-BINARY-CONTENT" * 1000
        checksum = hashlib.sha256(binary_bytes).hexdigest()

        manifest = {
            "platforms": {
                plat: {
                    "checksum": checksum,
                    "binary": "claude",
                    "size": len(binary_bytes),
                }
            }
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        with self._patch_urlopen_returning(manifest_bytes, binary_bytes):
            dest = install.install_version(self.locator, "2.1.999")

        self.assertEqual(dest, self.versions_dir / "2.1.999")
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), binary_bytes)
        # Should be executable (chmod 0o755)
        mode = dest.stat().st_mode & 0o777
        self.assertEqual(mode, 0o755)
        # No leftover .downloading file
        self.assertFalse(dest.with_suffix(".downloading").exists())

    def test_checksum_mismatch_raises_oserror_and_cleans_up(self) -> None:
        """A mismatching SHA-256 raises OSError and removes the partial download."""
        plat = install._detect_platform()
        binary_bytes = b"actual-bytes"
        wrong_checksum = "0" * 64  # definitely not the sha256 of binary_bytes

        manifest = {
            "platforms": {
                plat: {
                    "checksum": wrong_checksum,
                    "binary": "claude",
                    "size": len(binary_bytes),
                }
            }
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        with self._patch_urlopen_returning(manifest_bytes, binary_bytes):
            with self.assertRaises(OSError) as ctx:
                install.install_version(self.locator, "2.1.999")

        self.assertIn("Checksum mismatch", str(ctx.exception))
        # Partial download must be cleaned up
        self.assertFalse((self.versions_dir / "2.1.999.downloading").exists())
        # And the final destination must not exist
        self.assertFalse((self.versions_dir / "2.1.999").exists())

    def test_unsupported_platform_raises_before_download(self) -> None:
        """If detected platform isn't in the manifest, we raise OSError listing alternatives."""
        manifest = {
            "platforms": {
                "made-up-platform": {
                    "checksum": "x",
                    "binary": "claude",
                    "size": 0,
                }
            }
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        # Only the manifest call should happen; the binary call would raise StopIteration
        # via our iter() side-effect if reached -- which is itself a useful safety net.
        with mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            return_value=_FakeResponse(manifest_bytes),
        ):
            with self.assertRaises(OSError) as ctx:
                install.install_version(self.locator, "2.1.999")
        msg = str(ctx.exception)
        self.assertIn("not available", msg)
        self.assertIn("made-up-platform", msg)

    def _patch_urlopen_manifest_only(
        self, manifest_payload: bytes
    ) -> "mock._patch[mock.MagicMock]":
        """Patch urlopen (autospec) to return only the manifest response.

        Malformed-manifest errors must be raised before any binary download, so a
        second urlopen call would be a bug; return_value serves every call, but the
        test asserts the error fires first.
        """
        return mock.patch(
            "claudewheel.install.urllib.request.urlopen",
            autospec=True,
            return_value=_FakeResponse(manifest_payload),
        )

    def test_entry_missing_checksum_raises_oserror(self) -> None:
        """A platform entry lacking a 'checksum' key raises OSError, not KeyError."""
        plat = install._detect_platform()
        manifest = {"platforms": {plat: {"binary": "claude", "size": 0}}}
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        with self._patch_urlopen_manifest_only(manifest_bytes):
            with self.assertRaises(OSError) as ctx:
                install.install_version(self.locator, "2.1.999")
        msg = str(ctx.exception)
        self.assertIn("checksum", msg)
        self.assertIn("2.1.999", msg)

    def test_entry_not_a_dict_raises_oserror(self) -> None:
        """A platform entry that is not an object raises OSError, not TypeError."""
        plat = install._detect_platform()
        manifest = {"platforms": {plat: "not-a-dict"}}
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        with self._patch_urlopen_manifest_only(manifest_bytes):
            with self.assertRaises(OSError) as ctx:
                install.install_version(self.locator, "2.1.999")
        msg = str(ctx.exception)
        self.assertIn("malformed", msg)
        self.assertIn("2.1.999", msg)

    def test_platforms_not_a_dict_raises_oserror(self) -> None:
        """A non-object 'platforms' value raises OSError, not AttributeError/TypeError."""
        manifest: dict[str, object] = {"platforms": []}
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        with self._patch_urlopen_manifest_only(manifest_bytes):
            with self.assertRaises(OSError) as ctx:
                install.install_version(self.locator, "2.1.999")
        self.assertIn("2.1.999", str(ctx.exception))

    def test_non_dict_manifest_body_raises_oserror(self) -> None:
        """A valid-JSON, non-object manifest body raises OSError, not AttributeError."""
        manifest_bytes = json.dumps([1, 2, 3]).encode("utf-8")

        with self._patch_urlopen_manifest_only(manifest_bytes):
            with self.assertRaises(OSError) as ctx:
                install.install_version(self.locator, "2.1.999")
        self.assertIn("2.1.999", str(ctx.exception))

    def test_null_platforms_value_raises_oserror(self) -> None:
        """A JSON-null 'platforms' value raises OSError, not TypeError.

        JSON null parses to Python None. install_version's
        manifest.get("platforms", {}) returns the *present* None (the {} default
        only applies when the key is absent), so a `plat not in None` membership
        test would raise an uncaught TypeError. The manifest-boundary validation
        must reject a null platforms value up front.
        """
        manifest_bytes = json.dumps({"platforms": None}).encode("utf-8")

        with self._patch_urlopen_manifest_only(manifest_bytes):
            with self.assertRaises(OSError) as ctx:
                install.install_version(self.locator, "2.1.999")
        msg = str(ctx.exception)
        self.assertIn("malformed", msg)
        self.assertIn("2.1.999", msg)


if __name__ == "__main__":
    unittest.main()
