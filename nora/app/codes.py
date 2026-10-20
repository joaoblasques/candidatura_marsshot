"""Canonical NB command-code vocabulary.

Single source of truth for the shorthand codes a CEO can put on the first line
of a forward (e.g. ``NB-RE-MN.I`` or bare ``RE``). It drives two things so Nora
*understands every code* even when the drafts-only prototype can't *act* on all
of them:

1. The deterministic interpreter (`app/interpreter.py`) routes the **primary
   action** to a supported intent, or flags it "recognized but not available".
2. `CODE_REFERENCE` is injected into the drafting system prompt so the LLM
   applies the refinements (tone, length, my-name, targeting, confidentiality,
   notes) and knows the full grammar.

Safety invariant (unchanged): Nora never sends to a recipient and never books or
schedules anything. Codes that imply autonomous action (send/book/schedule) are
recognized and politely declined, never executed.

Grammar: a code sequence starts with ``NB`` and is split by ``-``; refinements
may be attached with dots (``RE.MN.I``). Free-text notes go in ``[...]``. A bare
single code (``RE``, ``S``) is also accepted for convenience.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PREFIX = "NB"

# --- Primary actions: head token -> intent name, or None when recognized but
#     not actionable in the drafts-only prototype (needs send/calendar/Drive/web).
ACTION_INTENT: dict[str, str | None] = {
    "RE": "DRAFT",        # reply email (draft)
    "WIYOW": "DRAFT",     # legacy alias for RE
    "SC": "SUMMARIZE",    # summarize content
    "S": "SUMMARIZE",     # summarize email & attachments
    "AC": "ANALYZE",      # analyze content (critical analysis)
    "AR": "ANALYZE",      # analyze report
    "PR": "REPORT",       # prepare report (as email text; file formats deferred)
    "FI": "RESEARCH",     # find info / research people and company (pluggable backend, default-off)
    "I": "RESEARCH",      # collect info on people (pluggable backend, default-off)
    # SM/BM activate the consent-gated meeting-proposal action (0.2.0): Nora drafts a
    # proposal and acts (emails the third party) ONLY after the CEO confirms (AG).
    # Email-proposal slice only; calendar booking / invites stay deferred.
    "BM": "PROPOSE_MEETING",  # propose a meeting (email the party, consent-gated)
    "SM": "PROPOSE_MEETING",  # propose/schedule a meeting (email the party, consent-gated)
    "SH": "PROPOSE_SHARE",    # share a file with the party (consent-gated, from Nora's address)
    "VP": "VOICE_PROFILE",    # view your saved voice profile; add FORGET to wipe it (Layer B)
    "RM": "RELATIONSHIP_MEMORY",  # view saved facts about this thread's counterparty; add FORGET to wipe (Layer C)
    "TR": "TRIAGE",           # triage a bundled forward of several threads into a ranked digest
    "HELP": "HELP",           # reply with the command-code reference (read-only)
}

# Concise, user-facing command reference — sent when the CEO emails HELP to their Nora address.
# (CODE_REFERENCE below is the longer LLM-facing grammar; this is the human cheat-sheet.)
HELP_TEXT = (
    "Nora — quick guide. Just tell me what you want in plain English, for example:\n"
    '  • "reply warmly and suggest Tuesday"\n'
    '  • "summarize this"  ·  "give me a critical analysis"  ·  "write a short report"\n'
    '  • "get me the Q4 deck and summarize it"  ·  "propose a meeting with Sarah"\n\n'
    "Two words always matter: reply AG to approve something I've proposed (ANG to cancel), and "
    'add KL ("keep local") to keep a message fully private / on-device.\n\n'
    "Optional shortcuts, if you like speed: RE reply · S summarize · AC analyze · PR report · "
    "SM propose a meeting · SH share a file · TR triage a batch. Refine in words "
    '("shorter", "formal", "in my name") or with dots (.Short / .F / .MN).\n\n'
    "To see what I've picked up about a contact, forward a thread from them and send RM "
    "(NB-RM FORGET to wipe it).\n\n"
    "You always send — I never send as you, and I only act (meeting, share) after your explicit AG."
)

# Human-readable label for a deferred action, used in the "not available" reply.
# FI/I have moved to the RESEARCH intent (pluggable backend); this dict remains for any
# future genuinely-deferred codes that need an "isn't available" explanation.
DEFERRED_LABEL: dict[str, str] = {}

# Approval reactions (in a reply to a Nora proposal): map the token to a decision.
APPROVAL_REACTION: dict[str, str] = {"AG": "confirm", "ANG": "decline"}

# Report file-format refinements: Nora renders the report into this Office file and
# attaches it (alongside the inline email-text report). Kept under the old name for
# import stability across modules.
DEFERRED_REPORT_FORMATS = {"PPT", "W", "X"}

# Recognized refinement/flag/approval tokens (effects applied by the LLM via the
# prompt; listed here so the parser recognizes a line as "codes"). Position
# disambiguates collisions: bare ``I`` is the info action above; dotted ``.I`` is
# the informal refinement here; bare ``NA`` is an approval reaction; dotted
# ``.NA`` is the skip-approval refinement.
REFINEMENT_TOKENS = {
    "MN",      # write in the user's name
    "F",       # formal tone
    "I",       # informal tone (dotted position)
    "LONG",    # long output
    "SHORT",   # short output
    "NA",      # dotted: skip-approval; bare: not-approved-incorporate-comments
    "A",       # SM.A — place a calendar hold (private; nobody notified)
    "IV",      # SM.A.IV — also invite the participant (consent-gated calendar invitation)
    "G",       # SH.G — share by GRANTing store access + a link (vs emailing an attachment)
    "SV",      # PR.SV — also save the generated report to Drive and reply with the link
    "PPT", "W", "X", "E",  # report formats: slides / word / sheet / email
    "C",       # confidential
    "KL",      # keep local — process on the on-device model; never send to the Cloud API
    "P",       # priority
    "AG",      # approval granted (reaction)
    "ANG",     # approval not granted (reaction)
}

ALL_CODE_TOKENS = set(ACTION_INTENT) | REFINEMENT_TOKENS


@dataclass
class CodeParse:
    """Result of parsing a first-line code sequence."""

    recognized: bool = False           # did this look like a code sequence at all?
    action: str | None = None          # primary action head token (canonical)
    intent: str | None = None          # mapped intent name for the action
    deferred_action: str | None = None # action recognized but not actionable
    approval: str | None = None        # "confirm"/"decline" when AG/ANG is present
    modifiers: list[str] = field(default_factory=list)   # refinement/flag tokens
    targets: list[str] = field(default_factory=list)     # @from / @cc / @email
    raw: str = ""                      # the original token, for echoing to the LLM


def _strip_prefix(up: str) -> tuple[str, bool]:
    if up.startswith(PREFIX):
        return up[len(PREFIX):].lstrip("-."), True
    return up, False


def parse_code_token(token: str) -> CodeParse:
    """Parse a single whitespace-free code token (e.g. ``NB-RE-MN.I`` or ``RE``).

    Returns ``recognized=False`` when the token is not NB-prefixed and contains
    no known code (so the caller treats the line as natural language).
    """
    raw = token.strip()
    up = raw.upper()
    body, nb = _strip_prefix(up)
    parse = CodeParse(recognized=nb, raw=raw)
    if not body:
        return parse  # bare "NB"
    for part in re.split(r"[-\s]+", body):
        if not part:
            continue
        if part.startswith("@"):
            parse.targets.append(part.lower())
            parse.recognized = True
            continue
        segments = part.split(".")
        head = segments[0]
        if head:
            if head in ACTION_INTENT:
                parse.recognized = True
                if parse.action is None:
                    parse.action = head
                    parse.intent = ACTION_INTENT[head]
                    if parse.intent is None:
                        parse.deferred_action = head
            elif head in REFINEMENT_TOKENS:
                parse.recognized = True
                parse.modifiers.append(head)
        for mod in segments[1:]:
            if mod in ALL_CODE_TOKENS:
                parse.recognized = True
                parse.modifiers.append(mod)
    # Surface an approval reaction (AG/ANG) as a structured decision, not just a
    # modifier — the consent gate reads this, never free text (KTD-F).
    for tok in parse.modifiers:
        if tok in APPROVAL_REACTION:
            parse.approval = APPROVAL_REACTION[tok]
            break
    return parse


# --- Reference injected into the drafting system prompt --------------------------
CODE_REFERENCE = """\
NORA COMMAND CODES. The CEO may put shorthand codes on the first line of a forward.
A code sequence starts with NB and is dash-split; refinements attach with dots
(e.g. NB-RE-MN.I). A bare single code (RE, S) is also valid. Free-text notes go in
[square brackets]. You understand ALL of the following:

Actions (what to produce):
- RE  reply email — write a complete reply, ready for the CEO to send.
- SC / S  summarize the email and any attachments.
- TR  triage several forwarded threads at once — returns a priority-ranked digest (read-only).
- HELP  reply with this command-code guide (read-only).
- AC  analyze content — a critical, constructive analysis (not just a summary).
- AR  analyze a report.
- PR  prepare a report on the email's topic, as email text (default).
- FI / I  research the people/company on the thread. Declines gracefully if a
  research backend isn't configured in this version — say so; do not invent facts.
- BM / SM  propose a meeting. Nora drafts a meeting-proposal email to the other
  party IN HER OWN labeled voice (the EA writing on the CEO's behalf — never as the
  CEO) and returns it to the CEO for approval. Nora sends it ONLY after the CEO
  replies AG (confirm).
- SM.A  place a calendar HOLD after AG (private — nobody else is notified).
- SM.A.IV  also INVITE the participant: after AG, Nora sends a calendar invitation
  from HER OWN labeled calendar identity (never as the CEO). Without IV the hold stays
  private. Include a time, e.g. NB-SM-A-IV [meet 2026-07-01 15:00].
- SH  share a company file with the other party. Reference the file ([file: NAME]);
  Nora proposes sending it as an attachment FROM HER OWN labeled address, and sends it
  ONLY after the CEO replies AG. The file is read fresh at send time. e.g.
  NB-SH [file: Q3-deck.pdf].
- SH.G  share by granting the other party ACCESS to the file in the store (a link)
  instead of emailing a copy — useful for large files. After AG, Nora grants read
  access silently (no store email) and sends the link from HER OWN address. e.g.
  NB-SH-G [file: Q3-deck.pdf].

Refinements (modify the output):
- .MN  write in the CEO's own name/voice.
- .F  formal tone / .I  informal tone (default: neutral).
- .Long  longer / .Short  shorter.
- @from / @cc / @<email>  who the reply or message is aimed at.
- C  the content is confidential — keep it internal; never address third parties.
- KL  keep local — process this entirely on Nora's on-device model; the content is
  never sent to the external Cloud AI service (for sensitive material). Routing only;
  combine with any action (e.g. NB-RE-KL, NB-S-KL).
- P  high priority.
- PR.PPT / PR.W / PR.X  slides / Word / spreadsheet output — the report is written
  as email text AND attached as the requested Office file.
- PR.E  report as email text (no file).
- PR.SV  also save the generated report into Drive and reply with the link (her own
  store; no third party). e.g. NB-PR-W-SV.
- [ ... ]  extra instructions in plain text.
- [file: NAME]  use a company file by name — Nora reads it from the connected file
  store (read-only) and works from its contents (e.g. NB-S [file: Q3-deck.pdf]). Needs
  a file source configured; otherwise Nora works from the forwarded thread alone.

Approval reactions (in replies to Nora):
- AG approved / ANG not approved / NA not approved, incorporate the comments /
  .NA skip approval. For a CEO-voiced DRAFT, 'approved' just means the CEO will send
  it themselves. For a meeting PROPOSAL (BM/SM), AG authorizes Nora to send the
  proposal email she showed the CEO, from her own labeled address — confirm only.

ABSOLUTE RULE: Nora never sends from or as the CEO's address (no impersonation,
ever), and never takes any action (sending a proposal, etc.) without the CEO's
explicit confirmation. CEO-voiced replies are always drafts the CEO sends himself.
The only mail Nora originates is from her own labeled assistant identity, after an
explicit AG confirmation. If a code asks for something not available (calendar
booking, web research, file output), note that in 'uncertainty' and provide the
closest available deliverable instead.
"""
