#!/usr/bin/env python3
"""Rewrite one literal string in every file of one session store dir.

A repair tool for session data a buggy path migration corrupted, for example a
path compounded into one that never existed. It touches exactly one directory
(recursively: top-level transcripts and nested subagent transcripts alike) and
nothing else.

    scripts/rewrite-store-dir-path.py --store-dir DIR --old OLD --new NEW --dry-run
    scripts/rewrite-store-dir-path.py --store-dir DIR --old OLD --new NEW \\
        --apply --expected-occurrences N

--dry-run counts the occurrences of OLD per file and in total and writes
nothing. --apply requires the total the dry run printed: when the count found
at apply time differs, it refuses before writing anything. After writing it
recounts and fails unless no occurrence of OLD is left. Each file is replaced
atomically with its mode and modification time kept (session pickers order by
modification time), and a file whose size or modification time changed while
it was being rewritten (a live session appending to it) stops the run.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


def count_occurrences(store_dir: Path, old: bytes) -> dict[Path, int]:
    """Occurrences of *old* in every regular file under *store_dir*, by file."""
    counts: dict[Path, int] = {}
    for path in sorted(store_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        n = path.read_bytes().count(old)
        if n:
            counts[path] = n
    return counts


def rewrite_file(path: Path, old: bytes, new: bytes) -> None:
    """Replace *old* with *new* in *path*, atomically, keeping mode and times."""
    before = path.stat()
    data = path.read_bytes().replace(old, new)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, before.st_mode & 0o7777)
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
        now = path.stat()
        if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise RuntimeError(f"{path} changed while it was being rewritten")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store-dir", required=True, type=Path)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-occurrences", type=int)
    args = parser.parse_args()

    store_dir: Path = args.store_dir
    if not store_dir.is_dir():
        print(f"error: not a directory: {store_dir}", file=sys.stderr)
        return 1
    if not args.old or args.old == args.new:
        print("error: --old must be non-empty and differ from --new", file=sys.stderr)
        return 1
    if args.new.find(args.old) != -1:
        print("error: --new contains --old; a rerun would compound it", file=sys.stderr)
        return 1
    if args.apply and args.expected_occurrences is None:
        print("error: --apply requires --expected-occurrences", file=sys.stderr)
        return 1
    if args.dry_run and args.expected_occurrences is not None:
        print("error: --expected-occurrences belongs to --apply", file=sys.stderr)
        return 1

    old = args.old.encode()
    new = args.new.encode()
    counts = count_occurrences(store_dir, old)
    total = sum(counts.values())
    for path, n in counts.items():
        print(f"{n:8d}  {path.relative_to(store_dir)}")
    print(f"total: {total} occurrences in {len(counts)} files")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    if total != args.expected_occurrences:
        print(
            f"error: found {total} occurrences, expected "
            f"{args.expected_occurrences}; nothing written",
            file=sys.stderr,
        )
        return 1
    for path in counts:
        rewrite_file(path, old, new)
    left = sum(count_occurrences(store_dir, old).values())
    if left:
        print(f"error: {left} occurrences remain after the rewrite", file=sys.stderr)
        return 1
    print(f"rewrote {total} occurrences in {len(counts)} files; 0 remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
