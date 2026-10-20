import pytest

from app.email.gmail_oauth import GMAIL_OAUTH_SCOPES, build_gmail_oauth_service


def test_scopes_are_modify_only_no_send():
    # gmail.modify covers read + drafts + labels; withholding send makes "never sends" a
    # token-level guarantee for auto-draft on a personal inbox.
    assert GMAIL_OAUTH_SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]
    assert not any("gmail.send" in s for s in GMAIL_OAUTH_SCOPES)


def test_build_service_raises_clear_error_without_token(tmp_path):
    with pytest.raises(FileNotFoundError, match="gmail_oauth_login|OAuth token not found"):
        build_gmail_oauth_service(str(tmp_path / "missing-token.json"))
