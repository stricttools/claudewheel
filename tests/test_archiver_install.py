"""13.3: the offer to install saferm, and the verified download behind it.

Three claims, each asserted on both delete paths:

* the offer never runs where there is nobody to ask -- no terminal, and no
  preview, gets the hard refusal instead;
* a declined offer aborts the deletion, and nothing is removed;
* an accepted one installs, re-runs detection, and only then proceeds.

The download itself is stubbed at the HTTP seam, so the checksum arithmetic,
the asset selection and the unpack are exercised for real without the network.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import archiver, cli
from claudewheel.archiver import InstallError, Saferm, Unavailable
from tests.wheelhelpers import STUB_SAFERM_SOURCE, SandboxHomeTestCase

ASSET = "saferm_0.9.0_linux_amd64.tar.gz"


def tarball(payload: bytes = b"#!/bin/sh\nexit 0\n") -> bytes:
    """A release tarball holding one ``saferm`` entry."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("saferm")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _DownloadStub:
    """Stands in for the two declared reads an install performs."""

    def __init__(
        self, *, blob: bytes | None = None, checksum: str | None = None
    ) -> None:
        self.blob = tarball() if blob is None else blob
        self.checksum = (
            hashlib.sha256(self.blob).hexdigest() if checksum is None else checksum
        )
        self.urls: list[str] = []

    def __call__(self, url: str, **kwargs: Any) -> bytes:
        self.urls.append(url)
        if url.endswith("checksums.txt"):
            return f"{self.checksum}  {ASSET}\n".encode()
        return self.blob


class InstallTests(SandboxHomeTestCase):
    """The verified download, shaped on the Claude Code install."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.launcher_dir

    def _install(self, stub: _DownloadStub) -> Path:
        with (
            mock.patch("claudewheel.archiver.effects.http_read", stub),
            mock.patch(
                "claudewheel.archiver.asset_name", autospec=True, return_value=ASSET
            ),
        ):
            return archiver.install(self.root)

    def test_a_verified_download_lands_executable_where_detection_looks(self) -> None:
        stub = _DownloadStub()
        dest = self._install(stub)
        self.assertEqual(dest, archiver.bin_dir(self.root) / "saferm")
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.stat().st_mode & 0o777, 0o755)
        self.assertEqual(dest.read_bytes(), b"#!/bin/sh\nexit 0\n")

    def test_the_manifest_is_read_before_the_asset(self) -> None:
        stub = _DownloadStub()
        self._install(stub)
        self.assertTrue(stub.urls[0].endswith("checksums.txt"))
        self.assertTrue(stub.urls[1].endswith(ASSET))

    def test_a_checksum_mismatch_installs_nothing(self) -> None:
        stub = _DownloadStub(checksum="0" * 64)
        with self.assertRaises(InstallError) as ctx:
            self._install(stub)
        self.assertIn("checksum mismatch", str(ctx.exception))
        self.assertFalse((archiver.bin_dir(self.root) / "saferm").exists())

    def test_a_platform_with_no_asset_is_a_hard_error(self) -> None:
        stub = _DownloadStub()
        with (
            mock.patch("claudewheel.archiver.effects.http_read", stub),
            mock.patch(
                "claudewheel.archiver.asset_name",
                autospec=True,
                return_value="saferm_0.9.0_plan9_mips.tar.gz",
            ),
        ):
            with self.assertRaises(InstallError) as ctx:
                archiver.install(self.root)
        self.assertIn("publishes no asset", str(ctx.exception))

    def test_a_tarball_without_the_binary_is_a_hard_error(self) -> None:
        empty = io.BytesIO()
        with tarfile.open(fileobj=empty, mode="w:gz"):
            pass
        stub = _DownloadStub(blob=empty.getvalue())
        with self.assertRaises(InstallError):
            self._install(stub)
        self.assertFalse((archiver.bin_dir(self.root) / "saferm").exists())

    def test_the_version_comes_from_the_manifest_not_from_a_probe(self) -> None:
        stub = _DownloadStub()
        with (
            mock.patch("claudewheel.archiver.effects.http_read", stub),
            mock.patch("claudewheel.archiver.asset_name", autospec=True) as asset_name,
        ):
            asset_name.return_value = ASSET
            archiver.install(self.root)
        asset_name.assert_called_once_with("0.9.0")


class CliOfferTests(SandboxHomeTestCase):
    """The scripted door's offer: only at a terminal, and never a fallback."""

    def setUp(self) -> None:
        super().setUp()
        from claudewheel.workspace import Workspace

        self.ws = Workspace.default()

    def _resolve(
        self, *, tty: bool, answer: str = "n", stub: _DownloadStub | None = None
    ) -> tuple[Any, str, str]:
        out, err = io.StringIO(), io.StringIO()
        download = stub or _DownloadStub()
        with (
            mock.patch(
                "claudewheel.archiver.detect",
                autospec=True,
                return_value=Unavailable(kind="absent"),
            ),
            mock.patch("sys.stdin.isatty", autospec=True, return_value=tty),
            mock.patch("builtins.input", autospec=True, return_value=answer),
            mock.patch("claudewheel.archiver.effects.http_read", download),
            mock.patch(
                "claudewheel.archiver.asset_name", autospec=True, return_value=ASSET
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            found = cli._resolve_archiver(self.ws, "work")
        return found, out.getvalue(), err.getvalue()

    def test_no_terminal_means_no_offer_at_all(self) -> None:
        found, out, _err = self._resolve(tty=False)
        self.assertIsInstance(found, Unavailable)
        self.assertEqual(out, "")

    def test_a_preview_never_installs_even_at_a_terminal(self) -> None:
        with mock.patch(
            "claudewheel.cli.effects.previewing", autospec=True, return_value=True
        ):
            found, out, _err = self._resolve(tty=True, answer="y")
        self.assertIsInstance(found, Unavailable)
        self.assertEqual(out, "")

    def test_a_declined_offer_aborts_and_names_the_install(self) -> None:
        found, out, _err = self._resolve(tty=True, answer="n")
        self.assertIsInstance(found, Unavailable)
        self.assertIn("was not deleted", out)
        self.assertIn("go install github.com/smm-h/saferm@v0", out)

    def test_the_offer_states_the_stakes_before_it_asks(self) -> None:
        _found, out, _err = self._resolve(tty=True, answer="n")
        self.assertIn("saferm is not installed", out)
        self.assertIn("irreversible", out)
        self.assertIn("OAuth token", out)

    def test_an_accepted_offer_installs_and_re_runs_detection(self) -> None:
        stub = _DownloadStub(blob=self._stub_tarball())
        real_detect = archiver.detect
        seen: list[Any] = []

        def detect(root: Path) -> Any:
            # Absent the first time, really probed the second: the offer's
            # promise is that the deletion proceeds only against a binary that
            # has answered.
            if not seen:
                seen.append("first")
                return Unavailable(kind="absent")
            return real_detect(root)

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch(
                "claudewheel.archiver.detect", autospec=True, side_effect=detect
            ),
            mock.patch("sys.stdin.isatty", autospec=True, return_value=True),
            mock.patch("builtins.input", autospec=True, return_value="y"),
            mock.patch("claudewheel.archiver.effects.http_read", stub),
            mock.patch(
                "claudewheel.archiver.asset_name", autospec=True, return_value=ASSET
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            found = cli._resolve_archiver(self.ws, "work")
        self.assertIsInstance(found, Saferm)
        self.assertEqual(found.binary, archiver.bin_dir(self.ws.root) / "saferm")
        self.assertIn("Installed saferm at", out.getvalue())

    def test_a_failed_install_is_a_hard_error_not_a_fallback(self) -> None:
        stub = _DownloadStub(checksum="0" * 64)
        found, out, err = self._resolve(tty=True, answer="y", stub=stub)
        self.assertIsInstance(found, Unavailable)
        self.assertIn("checksum mismatch", err)
        self.assertNotIn("Installed saferm", out)

    def test_a_fresh_binary_that_still_cannot_answer_aborts(self) -> None:
        """The installed program is really probed, not assumed: this tarball
        holds a script that exits 0 and prints nothing, which is not an answer
        to `capabilities`."""
        stub = _DownloadStub()
        real_detect = archiver.detect
        seen: list[str] = []

        def detect(root: Path) -> Any:
            if not seen:
                seen.append("first")
                return Unavailable(kind="absent")
            return real_detect(root)

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch(
                "claudewheel.archiver.detect", autospec=True, side_effect=detect
            ),
            mock.patch("sys.stdin.isatty", autospec=True, return_value=True),
            mock.patch("builtins.input", autospec=True, return_value="y"),
            mock.patch("claudewheel.archiver.effects.http_read", stub),
            mock.patch(
                "claudewheel.archiver.asset_name", autospec=True, return_value=ASSET
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            found = cli._resolve_archiver(self.ws, "work")
        self.assertIsInstance(found, Unavailable)
        self.assertIn("still does not ship", err.getvalue())

    def _stub_tarball(self) -> bytes:
        return tarball(STUB_SAFERM_SOURCE.encode())


class TuiOfferTests(SandboxHomeTestCase):
    """The TUI's offer, on the same three outcomes."""

    def _app(self) -> Any:
        from claudewheel import app as app_mod
        from claudewheel.workspace import Workspace

        app = object.__new__(app_mod.App)
        app.workspace = Workspace.default()
        app.terminal = mock.MagicMock()
        app.theme = mock.MagicMock()
        return app

    def test_a_declined_offer_aborts_and_names_the_install(self) -> None:
        app = self._app()
        with (
            mock.patch(
                "claudewheel.archiver.detect",
                autospec=True,
                return_value=Unavailable(kind="absent"),
            ),
            mock.patch(
                "claudewheel.ui.run_selection", autospec=True, return_value="cancel"
            ),
            mock.patch("claudewheel.ui.show_page", autospec=True) as show_page,
            mock.patch("claudewheel.archiver.install", autospec=True) as install,
        ):
            self.assertIsNone(app._resolve_archiver("work"))
        install.assert_not_called()
        lines = "\n".join(show_page.call_args.args[1])
        self.assertIn("go install github.com/smm-h/saferm@v0", lines)

    def test_the_offer_never_lists_an_option_that_deletes_anyway(self) -> None:
        app = self._app()
        with (
            mock.patch(
                "claudewheel.archiver.detect",
                autospec=True,
                return_value=Unavailable(kind="absent"),
            ),
            mock.patch(
                "claudewheel.ui.run_selection", autospec=True, return_value="cancel"
            ) as run_selection,
            mock.patch("claudewheel.ui.show_page", autospec=True),
        ):
            app._resolve_archiver("work")
        keys = [key for key, _label in run_selection.call_args.args[1]]
        self.assertEqual(keys, ["cancel", "install"])

    def test_an_accepted_offer_installs_and_re_runs_detection(self) -> None:
        app = self._app()
        tool = Saferm(binary=Path("/tmp/saferm"), features=archiver.REQUIRED_FEATURES)
        answers = [Unavailable(kind="absent"), tool]
        with (
            mock.patch(
                "claudewheel.archiver.detect",
                autospec=True,
                side_effect=lambda root: answers.pop(0),
            ),
            mock.patch(
                "claudewheel.ui.run_selection", autospec=True, return_value="install"
            ),
            mock.patch("claudewheel.ui.show_page", autospec=True) as show_page,
            mock.patch(
                "claudewheel.archiver.install",
                autospec=True,
                return_value=Path("/tmp/saferm"),
            ) as install,
        ):
            self.assertIs(app._resolve_archiver("work"), tool)
        install.assert_called_once()
        show_page.assert_not_called()
        self.assertEqual(answers, [])

    def test_a_failed_install_aborts_rather_than_deleting(self) -> None:
        app = self._app()
        with (
            mock.patch(
                "claudewheel.archiver.detect",
                autospec=True,
                return_value=Unavailable(kind="absent"),
            ),
            mock.patch(
                "claudewheel.ui.run_selection", autospec=True, return_value="install"
            ),
            mock.patch("claudewheel.ui.show_page", autospec=True) as show_page,
            mock.patch(
                "claudewheel.archiver.install",
                autospec=True,
                side_effect=InstallError("checksum mismatch"),
            ),
        ):
            self.assertIsNone(app._resolve_archiver("work"))
        lines = "\n".join(show_page.call_args.args[1])
        self.assertIn("checksum mismatch", lines)

    def test_a_fresh_binary_that_still_cannot_answer_aborts(self) -> None:
        app = self._app()
        with (
            mock.patch(
                "claudewheel.archiver.detect",
                autospec=True,
                return_value=Unavailable(kind="absent"),
            ),
            mock.patch(
                "claudewheel.ui.run_selection", autospec=True, return_value="install"
            ),
            mock.patch("claudewheel.ui.show_page", autospec=True) as show_page,
            mock.patch(
                "claudewheel.archiver.install",
                autospec=True,
                return_value=Path("/tmp/saferm"),
            ),
        ):
            self.assertIsNone(app._resolve_archiver("work"))
        lines = " ".join(show_page.call_args.args[1])
        self.assertIn("still does", lines)
        self.assertIn("not ship what claudewheel needs", lines)


if __name__ == "__main__":
    unittest.main()
