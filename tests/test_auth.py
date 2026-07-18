"""Tests for auth: validate_token state mapping and extract_token scraping."""

from __future__ import annotations

import unittest
import urllib.error
from email.message import Message
from unittest import mock

from claudewheel.auth import (
    INDETERMINATE,
    INVALID,
    UNREACHABLE,
    VALID,
    extract_token,
    validate_token,
)

FAKE_TOKEN = "sk-ant-oat01-not-a-real-token-just-for-tests"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.anthropic.com/v1/models?limit=1",
        code,
        "message",
        Message(),
        None,
    )


class ValidateTokenTests(unittest.TestCase):
    def _run(self, urlopen_mock: mock.MagicMock) -> str:
        with mock.patch("urllib.request.urlopen", urlopen_mock):
            return validate_token(FAKE_TOKEN)

    def test_200_is_valid_and_request_shape(self) -> None:
        resp = mock.MagicMock()
        resp.status = 200
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        urlopen = mock.MagicMock(return_value=cm)

        self.assertEqual(self._run(urlopen), VALID)

        (req,), kwargs = urlopen.call_args
        self.assertEqual(kwargs.get("timeout"), 5.0)
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/models?limit=1")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_TOKEN}")
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")

    def test_custom_timeout_is_forwarded(self) -> None:
        resp = mock.MagicMock()
        resp.status = 200
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        urlopen = mock.MagicMock(return_value=cm)
        with mock.patch("urllib.request.urlopen", urlopen):
            validate_token(FAKE_TOKEN, timeout=2.5)
        self.assertEqual(urlopen.call_args.kwargs.get("timeout"), 2.5)

    def test_401_is_invalid(self) -> None:
        urlopen = mock.MagicMock(side_effect=_http_error(401))
        self.assertEqual(self._run(urlopen), INVALID)

    def test_timeout_is_unreachable(self) -> None:
        urlopen = mock.MagicMock(side_effect=TimeoutError("timed out"))
        self.assertEqual(self._run(urlopen), UNREACHABLE)

    def test_urlerror_is_unreachable(self) -> None:
        urlopen = mock.MagicMock(
            side_effect=urllib.error.URLError("name resolution failed")
        )
        self.assertEqual(self._run(urlopen), UNREACHABLE)

    def test_429_is_indeterminate(self) -> None:
        urlopen = mock.MagicMock(side_effect=_http_error(429))
        self.assertEqual(self._run(urlopen), INDETERMINATE)

    def test_500_is_indeterminate(self) -> None:
        urlopen = mock.MagicMock(side_effect=_http_error(500))
        self.assertEqual(self._run(urlopen), INDETERMINATE)

    def test_400_is_indeterminate(self) -> None:
        urlopen = mock.MagicMock(side_effect=_http_error(400))
        self.assertEqual(self._run(urlopen), INDETERMINATE)

    def test_token_never_in_propagated_exception(self) -> None:
        # An unexpected exception type propagates; the token must not appear
        # anywhere in it.
        urlopen = mock.MagicMock(side_effect=ValueError("unexpected failure"))
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(ValueError) as ctx:
                validate_token(FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        self.assertNotIn(FAKE_TOKEN, repr(ctx.exception))


# A realistic-length synthetic token (93 chars total).
TOKEN = b"sk-ant-oat01-" + b"Ab1_Cd2-Ef3g" * 6 + b"XYZwvuts"
LABEL_LINE = b" \x1b[32m\xe2\x9c\x93\x1b[39m OAuth token created (valid for 1 year):\n"


class ExtractTokenTests(unittest.TestCase):
    def test_plain_token(self) -> None:
        self.assertEqual(extract_token(b"Your token: " + TOKEN + b"\n"), TOKEN.decode())

    def test_hard_wrapped_token_rejoined(self) -> None:
        half = len(TOKEN) // 2
        wrapped = TOKEN[:half] + b"\n" + TOKEN[half:]
        self.assertEqual(extract_token(b"token:\n" + wrapped + b"\n"), TOKEN.decode())

    def test_token_inside_ansi_color_codes(self) -> None:
        buf = b"\x1b[1m\x1b[38;5;208m" + TOKEN + b"\x1b[39m\x1b[22m"
        self.assertEqual(extract_token(buf), TOKEN.decode())

    def test_token_inside_osc8_hyperlink_bel(self) -> None:
        buf = b"\x1b]8;;https://console.anthropic.com\x07" + TOKEN + b"\x1b]8;;\x07"
        self.assertEqual(extract_token(buf), TOKEN.decode())

    def test_token_inside_osc8_hyperlink_st(self) -> None:
        buf = b"\x1b]8;;https://console.anthropic.com\x1b\\" + TOKEN + b"\x1b]8;;\x1b\\"
        self.assertEqual(extract_token(buf), TOKEN.decode())

    def test_last_frame_wins_with_repeated_label(self) -> None:
        stale = b"sk-ant-oat01-STALE" + b"0" * 60
        frame1 = LABEL_LINE + b" " + stale + b"\n"
        frame2 = LABEL_LINE + b" " + TOKEN + b"\n"
        # Ink redraws: two frames, each with the label; last frame wins.
        self.assertEqual(
            extract_token(frame1 + b"\x1b[2K\x1b[1A" + frame2), TOKEN.decode()
        )

    def test_no_token_returns_none(self) -> None:
        self.assertIsNone(extract_token(b"no credentials here\nnothing\n"))
        self.assertIsNone(extract_token(b""))

    def test_short_match_rejected(self) -> None:
        # Matches the loosened pattern but is under the 50-char sanity floor.
        short = b"sk-ant-" + b"x" * 35
        self.assertIsNone(extract_token(b"stub: " + short + b" end\n"))

    def test_prefers_oat01_candidate(self) -> None:
        # Note: joining removes newlines, so candidates must be delimited by
        # a non-token character (space) or they would merge into one run.
        other = b"sk-ant-" + b"z" * 60  # long enough, but no oat01 infix
        buf = other + b" \n" + TOKEN + b"\n"
        self.assertEqual(extract_token(buf), TOKEN.decode())
        # Order-independent: oat01 wins even when it comes first.
        buf = TOKEN + b" \n" + other + b"\n"
        self.assertEqual(extract_token(buf), TOKEN.decode())

    def test_label_present_but_token_before_label_falls_back(self) -> None:
        buf = TOKEN + b" \nThis token is valid for 1 year.\n"
        self.assertEqual(extract_token(buf), TOKEN.decode())

    def test_ink_style_output(self) -> None:
        # Mimic Ink output: cursor-control redraw junk, a label line, then
        # the token line wrapped in color codes (POC's known format).
        buf = (
            b"\x1b[2K\x1b[1A\x1b[2K\x1b[G"
            b"\x1b[?25l\r\n" + LABEL_LINE.replace(b"\n", b"\r\n") + b"\r\n"
            b"\x1b[1m" + TOKEN + b"\x1b[22m\r\n"
            b"\r\n\x1b[?25h"
        )
        self.assertEqual(extract_token(buf), TOKEN.decode())


if __name__ == "__main__":
    unittest.main()
