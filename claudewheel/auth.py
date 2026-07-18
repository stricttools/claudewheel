"""Validate OAuth tokens against the Anthropic API and extract them from captured output."""

from __future__ import annotations

import re
import urllib.error
import urllib.request

# validate_token() result states.
VALID = "valid"  # API accepted the token (HTTP 200)
INVALID = "invalid"  # API rejected the token (HTTP 401)
UNREACHABLE = "unreachable"  # network failure: DNS, timeout, refused
INDETERMINATE = "indeterminate"  # any other HTTP status (400/429/5xx/...)

_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1"
_ANTHROPIC_VERSION = "2023-06-01"

# Terminal escape sequences to strip before scanning for a token:
# OSC (incl. OSC-8 hyperlinks) terminated by BEL or ST, CSI sequences,
# charset selects, keypad mode switches, and carriage returns.
_ANSI_RE = re.compile(
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    rb"|\x1b\[[0-9;?]*[ -/]*[@-~]"
    rb"|\x1b[()][A-Za-z0-9]"
    rb"|\x1b[=>]"
    rb"|\r"
)

# Loosened token pattern: the live validation probe is the real gate, so we
# only anchor on the stable "sk-ant-" prefix, not the current "oat01" infix.
_TOKEN_RE = re.compile(rb"sk-ant-[A-Za-z0-9_-]{30,}")

_LABEL = b"valid for 1 year"
_MIN_TOKEN_LEN = 50


def validate_token(token: str, timeout: float = 5.0) -> str:
    """Probe the Anthropic API with the token; return one of the four states.

    Never logs, prints, or embeds the token in any raised exception.
    """
    req = urllib.request.Request(
        _MODELS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": _ANTHROPIC_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return VALID
            return INDETERMINATE
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return INVALID
        return INDETERMINATE
    except (urllib.error.URLError, TimeoutError, OSError):
        # URLError covers DNS failures and connection errors; TimeoutError
        # covers socket timeouts raised directly.
        return UNREACHABLE


def extract_token(captured: bytes) -> str | None:
    """Scrape an OAuth token from raw PTY-captured terminal output.

    Strips terminal escape sequences, joins lines (to defeat hard-wrapping
    mid-token), anchors the search after the last "valid for 1 year" label
    when present (Ink re-renders frames; the last frame wins), and applies a
    length sanity check. Returns the best candidate or None.
    """
    clean = _ANSI_RE.sub(b"", captured)
    joined = clean.replace(b"\n", b"")

    label_pos = joined.rfind(_LABEL)
    if label_pos != -1:
        region = joined[label_pos + len(_LABEL) :]
        candidate = _best_candidate(region)
        if candidate is not None:
            return candidate
        # Label present but nothing after it -- fall back to the full buffer
        # (the token may have been rendered before the label).
    return _best_candidate(joined)


def _best_candidate(data: bytes) -> str | None:
    """Pick the best token-looking match in data, or None."""
    matches = [
        m.group(0)
        for m in _TOKEN_RE.finditer(data)
        if len(m.group(0)) >= _MIN_TOKEN_LEN
    ]
    if not matches:
        return None
    # Prefer candidates with the known "oat01" infix; among ties take the
    # last one (later output wins).
    preferred = [m for m in matches if b"oat01" in m]
    pool = preferred or matches
    return pool[-1].decode("ascii")
