"""Fail on unexpected `.pth` files in the virtualenv's site-packages (supply chain, T6).

A `.pth` file runs code at interpreter start-up. The only ones we expect are the
virtualenv bootstrap and our own editable install. Anything else means a dependency
slipped in start-up code, so the check fails and names the file.
"""

from __future__ import annotations

import site
import sys
from pathlib import Path

ALLOWED = frozenset(
    {
        "_virtualenv.pth",  # virtualenv bootstrap
        "_editable_impl_copenhagen.pth",  # `uv sync` editable install of this repo
        "distutils-precedence.pth",  # setuptools shim, sometimes present
    }
)


def unexpected_pth_files(dirs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        found.extend(p for p in sorted(directory.glob("*.pth")) if p.name not in ALLOWED)
    return found


def main() -> int:
    dirs = [Path(p) for p in site.getsitepackages()]
    bad = unexpected_pth_files(dirs)
    if bad:
        print("Unexpected .pth files (they run code at interpreter start-up):", file=sys.stderr)
        for path in bad:
            print(f"  {path}", file=sys.stderr)
        print(
            "Review the package that installed it; if it is legitimate, add it to ALLOWED.",
            file=sys.stderr,
        )
        return 1
    print(f"check_pth: ok ({', '.join(str(d) for d in dirs)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
