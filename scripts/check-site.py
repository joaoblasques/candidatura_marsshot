#!/usr/bin/env python3
"""Structural + safety checks for the static site. Exit 0 ok, 1 violations, 2 usage/preflight."""
import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

SECTIONS = ("abertura", "nora", "como-trabalho", "ideias", "contacto")
ALLOWED_PREFIXES = (
    "https://nora-bennett.com",
    "https://docs.nora-bennett.com",
    "https://github.com/joaoblasques/",
    "https://www.forecastfactor.ai/",
    "https://joaoblasques.com/",
    "mailto:talk@marsshot.eu",
)
ALLOWED_EXACT = (
    "https://github.com/joaoblasques",
    "https://joaoblasques.com",
    "https://www.linkedin.com/in/joaoblasques",
    "mailto:joaoeduardoblasques@gmail.com",
)
FORBIDDEN_TEXT = ("app.nora-bennett.com",)


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.refs, self.lang = set(), [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if "id" in a:
            self.ids.add(a["id"])
        for k in ("href", "src"):
            if a.get(k):
                self.refs.append(a[k])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--names", default="private/names-site.txt")
    args = ap.parse_args()
    root, names = Path(args.root), Path(args.names)
    index = root / "index.html"
    if not index.is_file() or not names.is_file() or not names.read_text().strip():
        print("preflight: need index.html and a non-empty names file", file=sys.stderr)
        return 2

    text = index.read_text()
    scan = Scan()
    scan.feed(text)
    errs = []
    errs += [f"missing section #{s}" for s in SECTIONS if s not in scan.ids]
    if scan.lang != "pt-PT":
        errs.append(f'<html lang> must be "pt-PT", got {scan.lang!r}')
    for ref in scan.refs:
        if ref.startswith("#") or ref in ALLOWED_EXACT or ref.startswith(ALLOWED_PREFIXES):
            continue
        if ref.startswith("/") or re.match(r"^[a-z][a-z0-9+.-]*:", ref, re.I) or ref.startswith("//"):
            errs.append(f"disallowed link: {ref}")
        elif not (root / ref.split("#")[0].split("?")[0]).exists():
            errs.append(f"relative link target missing: {ref}")
    errs += [f"forbidden text: {t}" for t in FORBIDDEN_TEXT if t in text]
    for pat in filter(None, (p.strip() for p in names.read_text().splitlines())):
        if re.search(pat, text, re.I):
            errs.append("forbidden name pattern found in index.html")
    print("\n".join(errs) if errs else "site ok")
    return 1 if errs else 0


sys.exit(main())
