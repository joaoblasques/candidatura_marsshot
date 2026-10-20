"""Postmark inbound-parse adapter (U2).

Maps Postmark's inbound webhook JSON into the normalized ``InboundEmail`` the
pipeline consumes, keeping Postmark behind the provider seam (KTD2). The notable
Postmark specifics:

- ``MailboxHash`` carries the plus-tag, i.e. the revise correlation token (KTD2).
- The secret-address local part is the Postmark server hash, recovered from
  ``OriginalRecipient`` (the address the message was delivered to).
- DKIM/SPF results are not a boolean — they ride in the ``Headers`` array. The
  trusted source is the MTA-stamped ``Authentication-Results`` header that the
  Cloudflare Worker forwards (with the trusted authserv-id); author-domain
  alignment is enforced against the From (KTD3/KTD5).
- ``MessageID`` is the replay/event id.

The exact spam-header content is confirmed against a real captured payload in U1;
this adapter targets the documented shape.
"""

from __future__ import annotations

import base64
import binascii

from app.email.inbound import split_secret_address
from app.models import Attachment, InboundEmail


def _header_value(headers: list[dict], name: str) -> str:
    """Return the first matching header value from Postmark's ``Headers`` array."""
    for header in headers:
        if not isinstance(header, dict):
            continue
        if str(header.get("Name", "")).lower() == name.lower():
            return str(header.get("Value", ""))
    return ""


def _author_domain(from_address: str) -> str:
    """The From author domain, lowercased (empty if unparseable)."""
    if "@" not in from_address:
        return ""
    return from_address.rsplit("@", 1)[-1].strip().lower().rstrip(".")


def _has_org_shape(domain: str) -> bool:
    """A registrable-domain heuristic: at least two labels (one dot).

    Without a public-suffix list this can't be exact, but it blocks the obvious
    over-permissive case — a bare TLD like ``com`` matching every ``*.com`` signer
    under relaxed alignment. Multi-label suffixes (e.g. ``co.uk``) are a known
    residual; the exact From-in-registry check independently authorizes.
    """
    return domain.count(".") >= 1


def _aligned(signing_domain: str, author_domain: str) -> bool:
    """Relaxed author-domain alignment: equal, or one is a subdomain of the other.

    Matches the ``DKIM_VALID_AU`` ("author-aligned") semantic without a public
    suffix list. Empty inputs never align (fail-closed). Subdomain alignment
    requires the shared parent to be a registrable domain (``_has_org_shape``), so
    a bare public suffix can't align an arbitrary signer.
    """
    d = signing_domain.strip().lower().rstrip(".")
    a = author_domain.strip().lower().rstrip(".")
    if not d or not a:
        return False
    if a == d:
        return True
    if a.endswith("." + d):  # signer is the parent org domain
        return _has_org_shape(d)
    if d.endswith("." + a):  # signer is a subdomain of the author org domain
        return _has_org_shape(a)
    return False


def _authentication_results_pass(ar_value: str, author_domain: str) -> bool:
    """True when an ``Authentication-Results`` value carries a ``dkim=pass`` whose
    signing domain (``header.d`` / ``header.i``) author-aligns with the From domain.

    Parses the RFC 8601 shape ``<authserv-id>; dkim=pass header.d=example.com ...;
    spf=...; dmarc=...``. A message may carry several DKIM results; any one aligned
    pass authenticates. The value is the MTA-stamped verdict the Worker selected
    (the app trusts the Worker to forward only the trusted authserv-id's results).
    """
    if not ar_value:
        return False
    for segment in ar_value.split(";"):
        tokens = segment.strip().split()
        if not tokens:
            continue
        method, _, result = tokens[0].partition("=")
        if method.strip().lower() != "dkim" or result.strip().strip('"').lower() != "pass":
            continue
        signing_domain = ""
        for tok in tokens[1:]:
            key, _, val = tok.partition("=")
            key = key.strip().lower()
            val = val.strip().strip('"')
            if key == "header.d":
                signing_domain = val
                break
            if key == "header.i":  # e.g. @example.com or local@example.com
                signing_domain = val.rsplit("@", 1)[-1]
                break
        if _aligned(signing_domain, author_domain):
            return True
    return False


def _dkim_aligned(headers: list[dict], from_address: str) -> bool:
    """True when the message carries a real, author-aligned DKIM pass.

    Source: the MTA-stamped ``Authentication-Results`` header (Cloudflare's
    verdict, forwarded by the Worker with the trusted authserv-id). Fail-closed:
    a missing, malformed, or non-pass verdict returns ``False``.
    """
    return _authentication_results_pass(
        _header_value(headers, "Authentication-Results"), _author_domain(from_address)
    )


def _recipient_address(payload: dict) -> str:
    """The address the message was delivered to (carries the server hash + tag)."""
    original = str(payload.get("OriginalRecipient") or "").strip()
    if original:
        return original
    to_full = payload.get("ToFull") or []
    if isinstance(to_full, list) and to_full and isinstance(to_full[0], dict):
        return str(to_full[0].get("Email") or "")
    return str(payload.get("To") or "")


def _from_address(payload: dict) -> str:
    """Return the sender address, preferring the structured ``FromFull`` field."""
    from_full = payload.get("FromFull") or {}
    if isinstance(from_full, dict) and from_full.get("Email"):
        return str(from_full["Email"])
    return str(payload.get("From") or "")


def adapt_postmark_inbound(payload: dict) -> InboundEmail:
    """Map a Postmark inbound webhook payload into an ``InboundEmail``."""
    secret_local, token_from_addr = split_secret_address(_recipient_address(payload))
    # MailboxHash is Postmark's canonical correlation tag; fall back to the tag
    # parsed off the recipient address if it is absent.
    # Stored correlation tokens are lowercase hex (secrets.token_hex); normalize
    # the echoed MailboxHash to match so revise lookups never miss on case.
    token = str(payload.get("MailboxHash") or "").strip().lower() or token_from_addr

    headers = payload.get("Headers")
    if not isinstance(headers, list):
        headers = []

    raw_attachments = payload.get("Attachments")
    if not isinstance(raw_attachments, list):
        raw_attachments = []
    attachments = []
    for a in raw_attachments:
        if not isinstance(a, dict) or not a.get("Name"):
            continue
        encoded = a.get("Content") or ""
        content: bytes | None = None
        if encoded:
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                content = None
        raw_len = a.get("ContentLength")
        size = (
            int(raw_len)
            if isinstance(raw_len, int)
            or (isinstance(raw_len, str) and raw_len.isdigit())
            else None
        )
        attachments.append(
            Attachment(
                filename=str(a.get("Name", "")),
                content_type=a.get("ContentType"),
                size=size,
                content=content,
            )
        )

    from_addr = _from_address(payload)
    return InboundEmail(
        event_id=str(payload.get("MessageID") or ""),
        secret_address_local=secret_local,
        from_address=from_addr,
        dkim_verified=_dkim_aligned(headers, from_addr),
        subject=str(payload.get("Subject") or ""),
        body=str(payload.get("TextBody") or ""),
        correlation_token=token or None,
        attachments=attachments,
    )
