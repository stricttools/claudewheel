"""Package version detection from package.json or installed metadata."""

import json as _json
from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version
from pathlib import Path as _Path


def _default_package_json() -> _Path:
    """Path to the repo-root package.json (one level above the package dir)."""
    return _Path(__file__).resolve().parent.parent / "package.json"


def _detect_version(package_json=None, metadata_version=_version) -> str:
    """Resolve the package version, preferring the repo-root package.json.

    Resolution order (exact, not heuristic):

    1. ``<pkg>/../package.json`` -- present only in a source checkout (the wheel
       packages only ``claudewheel/``). In an editable/source checkout this file
       is authoritative and wins over possibly-stale installed metadata.
    2. ``importlib.metadata.version("claudewheel")`` -- used for PyPI wheels,
       where the repo-root package.json is absent.
    3. ``"unknown"`` -- final fallback when neither source is available.

    ``package_json`` and ``metadata_version`` are injectable for testing.
    """
    if package_json is None:
        package_json = _default_package_json()
    if package_json.exists():
        try:
            return _json.loads(package_json.read_text())["version"]
        except (OSError, KeyError, _json.JSONDecodeError):
            pass

    try:
        return metadata_version("claudewheel")
    except _PackageNotFoundError:
        return "unknown"


__version__ = _detect_version()
