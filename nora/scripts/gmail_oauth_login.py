"""One-time Gmail OAuth consent for a personal @gmail.com. Opens a browser, you grant Nora
gmail.modify (read + drafts + labels; NO send), and the refresh token is saved locally so the
auto-draft poller can run non-interactively.

    NORA_GMAIL_OAUTH_CLIENT=~/nora-gmail-client.json \\
    uv run python -m scripts.gmail_oauth_login

Reads NORA_GMAIL_OAUTH_CLIENT (client secret path) and NORA_GMAIL_OAUTH_TOKEN (where to save the
token; default .nora-gmail-token.json). Revoke anytime at https://myaccount.google.com/permissions.
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.email.gmail_oauth import oauth_login


def main(argv: list[str]) -> int:
    settings = get_settings()
    client = settings.gmail_oauth_client
    if not client:
        print("FAIL: set NORA_GMAIL_OAUTH_CLIENT to your downloaded OAuth client secret JSON.")
        return 2
    path = oauth_login(client, settings.gmail_oauth_token)
    print(f"OK: saved Gmail OAuth token to {path}. You can now run scripts.autodraft_poll.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
