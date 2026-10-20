"""OAuth (installed-app) Gmail access for a PERSONAL / consumer @gmail.com. A service account
can't impersonate a consumer account (domain-wide delegation is Workspace-only), so a personal
inbox is connected by a one-time browser consent that mints a refresh token; the poller then
builds a Gmail service from that token, non-interactively.

Scope is **gmail.modify only** — read + write drafts + labels, but NO send scope, so this
credential structurally cannot send mail (auto-draft only ever drafts). Lazy imports keep the
oauth libs optional unless this path is used."""

from __future__ import annotations

import os
from typing import Any

# gmail.modify = read + drafts + labels. Deliberately NO gmail.send — auto-draft never sends,
# and withholding the scope makes that a hard guarantee at the token level.
GMAIL_OAUTH_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def build_gmail_oauth_service(token_path: str, scopes: list[str] | None = None) -> Any:
    """Build a Gmail service from a saved OAuth token (written by ``oauth_login``). Refreshes the
    access token if expired. Raises FileNotFoundError with a clear hint when the token is missing
    (run the one-time login first)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not token_path or not os.path.exists(token_path):
        raise FileNotFoundError(
            f"Gmail OAuth token not found at {token_path!r} — run "
            "`uv run python -m scripts.gmail_oauth_login` once to authorize."
        )
    creds = Credentials.from_authorized_user_file(token_path, scopes or GMAIL_OAUTH_SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _write_token(token_path, creds)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def oauth_login(client_secret_path: str, token_path: str, scopes: list[str] | None = None) -> str:
    """One-time interactive consent: open a browser, let the user grant ``scopes`` (default
    gmail.modify), and save the resulting token to ``token_path`` (0600). Returns the token path.
    Run by the operator once."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secret_path or not os.path.exists(client_secret_path):
        raise FileNotFoundError(
            f"OAuth client secret not found at {client_secret_path!r} — download it from your "
            "Google Cloud OAuth client and set NORA_GMAIL_OAUTH_CLIENT."
        )
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes or GMAIL_OAUTH_SCOPES)
    # access_type=offline + prompt=consent force a REFRESH token into the saved credentials, so a
    # headless host (the Mac Mini) can refresh access tokens forever without a browser. Without
    # this the token expires in ~1h and the poller would die on the Mini.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    _write_token(token_path, creds)
    return token_path


def _write_token(token_path: str, creds: Any) -> None:
    with open(token_path, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(token_path, 0o600)  # a refresh token is a credential — keep it private
