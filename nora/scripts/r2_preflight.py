"""R2 onboarding preflight: prove the in-tenant draft backend works before go-live.

Creates a throwaway draft in the configured tenant mailbox's Drafts, then deletes it —
exercising the exact path R2 uses. NO mail is ever sent. Dispatches on the configured
backend (Gmail = domain-wide delegation + compose scope; Graph = app-only Mail.ReadWrite):

    uv run python -m scripts.r2_preflight

Exit 0 = ready; non-zero = a clear diagnostic (delegation/scope/consent problem).
"""

from __future__ import annotations

import sys

from app.config import get_settings


def _preflight_gmail(settings) -> int:  # noqa: ANN001
    from app.tenant import GmailDraftTenantClient, build_gmail_service

    user, creds = settings.gmail_tenant_user, settings.gmail_credentials_json
    if not user or not creds:
        print("FAIL: NORA_GMAIL_TENANT_USER / NORA_GMAIL_CREDENTIALS_JSON not set.")
        return 2
    print(f"== R2 preflight (gmail): {user} ==")
    try:
        service = build_gmail_service(creds, user)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not build the Gmail service — check the key file + libs.\n  {exc}")
        return 1
    client = GmailDraftTenantClient(user, service=service)
    try:
        draft_id = client.save_draft(to="noreply@example.com",
                                     subject="Nora R2 preflight (safe to ignore)",
                                     body="Automated preflight draft — never sent; deleted now.")
        print(f"  PASS: created a draft in {user}'s Drafts (id={draft_id})")
    except Exception as exc:  # noqa: BLE001
        print("FAIL: could not create a draft — likely domain-wide delegation isn't granted "
              f"for gmail.compose, or the tenant user is wrong.\n  {exc}")
        return 1
    try:
        service.users().drafts().delete(userId=user, id=draft_id).execute()
        print("  PASS: deleted the preflight draft (cleanup ok)")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN: created but couldn't delete (id={draft_id}); remove it manually.\n  {exc}")
    return 0


def _preflight_graph(settings) -> int:  # noqa: ANN001
    from app.tenant import GraphDraftTenantClient, build_graph_session

    user = settings.graph_tenant_user
    if not (user and settings.graph_tenant_id and settings.graph_client_id and settings.graph_client_secret):
        print("FAIL: NORA_GRAPH_TENANT_ID/CLIENT_ID/CLIENT_SECRET/TENANT_USER not all set.")
        return 2
    print(f"== R2 preflight (graph): {user} ==")
    try:
        session = build_graph_session(
            settings.graph_tenant_id, settings.graph_client_id, settings.graph_client_secret
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not get a Graph token — check the app registration + secret.\n  {exc}")
        return 1
    client = GraphDraftTenantClient(user, session=session)
    try:
        draft_id = client.save_draft(to="noreply@example.com",
                                     subject="Nora R2 preflight (safe to ignore)",
                                     body="Automated preflight draft — never sent; deleted now.")
        print(f"  PASS: created a draft in {user}'s Outlook Drafts (id={draft_id})")
    except Exception as exc:  # noqa: BLE001
        print("FAIL: could not create a draft — likely Mail.ReadWrite isn't admin-consented, "
              f"or the tenant user is wrong.\n  {exc}")
        return 1
    try:
        session.delete(f"https://graph.microsoft.com/v1.0/users/{user}/messages/{draft_id}")
        print("  PASS: deleted the preflight draft (cleanup ok)")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN: created but couldn't delete (id={draft_id}); remove it manually.\n  {exc}")
    return 0


def main(argv: list[str]) -> int:
    settings = get_settings()
    backend = settings.tenant_backend
    if backend == "graph":
        rc = _preflight_graph(settings)
    elif backend in ("gmail", "none"):
        rc = _preflight_gmail(settings)  # default to gmail flow when unset
    else:
        print(f"FAIL: unknown NORA_TENANT_BACKEND '{backend}'.")
        return 2
    if rc == 0:
        print("\nR2 PREFLIGHT: PASS — in-tenant drafting is ready. Route the relationship "
              "with NORA_POSTURE_MAP and restart.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
