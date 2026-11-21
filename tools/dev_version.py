"""Compute the next development version tag: <base>-dev.<N>.

Rules:
  * base is the version currently in the repo (the next stable release);
  * N starts at 1 for a base version and increments per dev publication;
  * when the base version changes, N resets to 1 (there are no tags for the
    new base, so max+1 naturally yields 1).

N is derived from existing git tags, not from a global CI run counter, so
failed or skipped runs do not create gaps in the logical sequence.

Environment:
  GITHUB_REPOSITORY  owner/repo (set automatically by Actions)
  GH_TOKEN           token for the GitHub API (set automatically by Actions)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

from version import read_version


def main() -> int:
    base = read_version()
    repo = os.environ.get("GITHUB_REPOSITORY", "Vela-Language/Vela")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"User-Agent": "vela-dev-version", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/git/matching-refs/tags/v{base}-dev."
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            refs = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            refs = []
        else:
            print(f"GitHub API error {exc.code}: {exc.read()[:200]!r}", file=sys.stderr)
            return 2
    pattern = re.compile(rf"^refs/tags/v{re.escape(base)}-dev\.(\d+)$")
    numbers = []
    for ref in refs:
        m = pattern.match(ref.get("ref", ""))
        if m:
            numbers.append(int(m.group(1)))
    nxt = max(numbers, default=0) + 1
    print(f"{base}-dev.{nxt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
