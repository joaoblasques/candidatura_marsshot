#!/usr/bin/env python3
"""Fill <!-- excerpt: path:start-end --> ... <!-- /excerpt --> blocks in index.html from real files.
--check: exit 1 if index.html would change (stale excerpts). Exit 2 on a missing file/bad range."""
import argparse
import html
import re
import sys
from pathlib import Path

def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


BASE = re.compile(r"<!-- link-base: (\S+) -->")
BLOCK = re.compile(r"<!-- excerpt: (\S+?):(\d+)-(\d+) -->.*?<!-- /excerpt -->", re.S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    index = root / "index.html"
    old = index.read_text()
    m = BASE.search(old)
    base = m.group(1) if m else ""  # optional: link excerpts to GitHub blob URLs instead of raw files

    def fill(m):
        path, a, b = m.group(1), int(m.group(2)), int(m.group(3))
        src = root / path
        if not src.is_file():
            die(f"excerpt source missing: {path}")
        lines = src.read_text().split("\n")
        if not 1 <= a <= b <= len(lines):
            die(f"excerpt range out of bounds: {path}:{a}-{b}")
        code = html.escape("\n".join(lines[a - 1 : b]))
        return (
            f"<!-- excerpt: {path}:{a}-{b} -->"
            f'<pre><code>{code}</code></pre>'
            f'<p class="src"><a href="{base}{path}">{path}</a>, linhas {a}–{b}</p>'
            "<!-- /excerpt -->"
        )

    new = BLOCK.sub(fill, old)
    if args.check:
        if new != old:
            print("excerpts are stale; run scripts/sync-excerpts.py", file=sys.stderr)
            return 1
        return 0
    index.write_text(new)
    return 0


sys.exit(main())
