"""Keep the README tagline in sync with the source version."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INIT = ROOT / "vela" / "__init__.py"

TAGLINE = re.compile(r"^> v\d+\.\d+\.\d+", re.MULTILINE)


def source_version() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"),
                  re.MULTILINE)
    if not m:
        raise SystemExit("vela/__init__.py: __version__ not found")
    return m.group(1)


def rewrite(text: str, version: str) -> str:
    return TAGLINE.sub(f"> v{version}", text, count=1)


def main() -> int:
    version = source_version()
    text = README.read_text(encoding="utf-8")
    updated = rewrite(text, version)
    if updated == text:
        print(f"README already at v{version}")
        return 0
    README.write_text(updated, encoding="utf-8")
    print(f"README tagline updated to v{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
