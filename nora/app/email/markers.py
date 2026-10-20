"""Shared email-parsing marker patterns.

A single source of truth for the regexes/markers used to recognize email
structure (addresses, reply attributions, forward banners). Both
``app/email/parser.py`` (authored-region splitting) and
``app/drafting/voice.py`` (per-author exemplar segmentation) consume these, so a
client-format fix lands in one place instead of drifting between copies. This
module imports nothing from the app, so it cannot create an import cycle.
"""

from __future__ import annotations

import re

# An email address anywhere in a line.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# "On <date>, <name> wrote:" reply attribution (Gmail / Apple Mail).
ON_WROTE_RE = re.compile(r"^\s*on .+ wrote:\s*$", re.IGNORECASE)

# Banners that begin a quoted/forwarded region.
QUOTE_MARKERS = (
    "---------- forwarded message",
    "begin forwarded message:",
    "-----original message-----",
)
