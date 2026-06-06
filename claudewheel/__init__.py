"""Package version detection from installed metadata or package.json."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("claudewheel")
except _PackageNotFoundError:
    import json as _json
    from pathlib import Path as _Path

    _package_json = _Path(__file__).resolve().parent.parent / "package.json"
    try:
        __version__ = _json.loads(_package_json.read_text())["version"]
    except (OSError, KeyError, _json.JSONDecodeError):
        __version__ = "unknown"
