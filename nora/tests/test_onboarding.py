from app.onboarding import (
    access_request_reply,
    provision_signup,
    signup_eligible,
    welcome_reply,
)
from app.users import FileUserStore, effective_registry


def test_effective_registry_merges_static_and_dynamic(tmp_path):
    s = FileUserStore(str(tmp_path / "u.json"))
    s.add_user("nora-dyn", "new@acme.com")
    merged = effective_registry({"ceo": "boss@x.com"}, s)
    assert merged["ceo"] == "boss@x.com" and merged["nora-dyn"] == ["new@acme.com"]


def test_signup_eligible_matches_address_and_domain():
    allow = ["@acme.com", "vip@globex.com"]
    assert signup_eligible("anyone@acme.com", allow) is True
    assert signup_eligible("vip@globex.com", allow) is True
    assert signup_eligible("stranger@evil.com", allow) is False
    assert signup_eligible("x@acme.com", []) is False  # empty allowlist => nobody


def test_provision_is_idempotent_per_sender(tmp_path):
    s = FileUserStore(str(tmp_path / "u.json"))
    local1, new1 = provision_signup(s, "founder@acme.com")
    local2, new2 = provision_signup(s, "Founder@acme.com")  # same sender, different case
    assert new1 is True and new2 is False and local1 == local2
    assert local1.startswith("nora-")


def test_welcome_and_decline_replies():
    w = welcome_reply("nora-abc123", "nora-bennett.com")
    assert "nora-abc123@nora-bennett.com" in w.body
    d = access_request_reply()
    assert "access" in d.body.lower()


def test_welcome_directs_help_to_the_private_address_not_a_reply():
    w = welcome_reply("nora-abc123", "nora-bennett.com")
    assert "email HELP to the address above" in w.body
    assert "reply HELP" not in w.body.lower()  # the old, ambiguous wording is gone


def test_access_request_is_logged(caplog):
    import logging
    from app.onboarding import access_request_reply
    app_logger = logging.getLogger("app")
    prev = app_logger.propagate
    app_logger.propagate = True  # the app logger is configured propagate=False; let caplog see it
    try:
        with caplog.at_level(logging.INFO, logger="app.onboarding"):
            access_request_reply("stranger@evil.com")
    finally:
        app_logger.propagate = prev
    assert "access requested" in caplog.text and "stranger@evil.com" in caplog.text
