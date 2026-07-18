"""Download, verify, and install Claude Code binaries from Google Cloud Storage."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import urllib.request
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .binaries import BinaryLocator

GCS_BASE = (
    "https://storage.googleapis.com/"
    "claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/"
    "claude-code-releases"
)

# 5 minute timeout for the large binary download (~235MB)
DOWNLOAD_TIMEOUT = 300


def _detect_platform() -> str:
    """Detect the current platform string matching GCS manifest keys."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = machine

    system = sys.platform
    if system == "linux":
        return f"linux-{arch}"
    elif system == "darwin":
        return f"darwin-{arch}"
    elif system == "win32":
        return f"win32-{arch}"
    else:
        return f"{system}-{arch}"


def fetch_manifest(version: str) -> dict[str, Any]:
    """Fetch the version manifest from GCS. Returns the parsed JSON dict."""
    url = f"{GCS_BASE}/{version}/manifest.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "claudewheel"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            parsed: Any = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise OSError(f"Version {version} not found on server (HTTP {e.code})") from e
    except Exception as e:
        raise OSError(f"Failed to fetch manifest for {version}: {e}") from e

    # Validate the parsed structure so callers only ever see OSError (not
    # AttributeError/TypeError) when the server returns a well-formed-JSON but
    # semantically-malformed manifest.
    if not isinstance(parsed, dict):
        raise OSError(
            f"Manifest for {version} is malformed: expected a JSON object, "
            f"got {type(parsed).__name__}"
        )
    platforms = parsed.get("platforms")
    if platforms is not None and not isinstance(platforms, dict):
        raise OSError(
            f"Manifest for {version} is malformed: \"platforms\" must be a "
            f"JSON object, got {type(platforms).__name__}"
        )
    data: dict[str, Any] = parsed
    return data


def install_version(locator: "BinaryLocator", version: str,
                    progress_callback: Callable[[int, int], None] | None = None) -> Path:
    """Download and install a Claude Code version binary.

    Args:
        locator: The :class:`BinaryLocator` supplying the versions directory.
        version: The version string (e.g. "2.1.120")
        progress_callback: Optional callable(bytes_downloaded, total_bytes)

    Returns:
        Path to the installed binary.

    Raises:
        OSError on any failure (network, checksum, disk).
    """
    plat = _detect_platform()
    manifest = fetch_manifest(version)

    platforms = manifest.get("platforms", {})
    if plat not in platforms:
        available = ", ".join(sorted(platforms.keys()))
        raise OSError(
            f"Platform {plat} not available for {version}. "
            f"Available: {available}"
        )

    entry = platforms[plat]
    if not isinstance(entry, dict):
        raise OSError(
            f"Manifest entry for {plat} in {version} is malformed: "
            f"expected a JSON object, got {type(entry).__name__}"
        )
    if "checksum" not in entry:
        raise OSError(
            f"Manifest entry for {plat} in {version} is missing a checksum"
        )
    expected_checksum = entry["checksum"]
    binary_name = entry.get("binary", "claude")
    total_size = entry.get("size", 0)

    download_url = f"{GCS_BASE}/{version}/{plat}/{binary_name}"

    # Download with progress reporting
    req = urllib.request.Request(
        download_url, headers={"User-Agent": "claudewheel"}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT)
    except Exception as e:
        raise OSError(f"Failed to download {version}: {e}") from e

    versions_dir = locator.versions_dir
    versions_dir.mkdir(parents=True, exist_ok=True)
    dest = versions_dir / version
    tmp = dest.with_suffix(".downloading")

    sha256 = hashlib.sha256()
    downloaded = 0

    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                f.write(chunk)
                sha256.update(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total_size)

        actual_checksum = sha256.hexdigest()
        if actual_checksum != expected_checksum:
            tmp.unlink(missing_ok=True)
            raise OSError(
                f"Checksum mismatch for {version}: "
                f"expected {expected_checksum[:16]}..., "
                f"got {actual_checksum[:16]}..."
            )

        tmp.chmod(0o755)
        tmp.rename(dest)

    except OSError:
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise OSError(f"Failed to install {version}: {e}") from e

    return dest
