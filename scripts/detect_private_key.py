"""Refuse commits that contain private keys (local replacement for the upstream hook)."""

from __future__ import annotations

import sys
from pathlib import Path

# Built at import time so this file does not contain the markers it looks for.
_KEY = b"PRIVATE " + b"KEY"
MARKERS = tuple(
    b"BEGIN " + kind + _KEY
    for kind in (b"RSA ", b"DSA ", b"EC ", b"OPENSSH ", b"", b"ENCRYPTED ", b"PGP ")
)


def files_with_keys(paths: list[str]) -> list[str]:
    hits: list[str] = []
    for name in paths:
        path = Path(name)
        if path.is_file() and any(m in path.read_bytes() for m in MARKERS):
            hits.append(name)
    return hits


def main(argv: list[str]) -> int:
    hits = files_with_keys(argv)
    for name in hits:
        print(f"Private key found in {name}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
