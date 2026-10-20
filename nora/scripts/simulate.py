"""Local end-to-end simulator for the Nora v1.0 trust loop.

Drives the real pipeline (parse -> auth-context -> route -> draft -> receipt ->
send) on sample forwarded emails, with the outbound send captured instead of
mailed. Uses the real Claude drafting service when ANTHROPIC_API_KEY is set;
otherwise a clearly-labelled offline stand-in so the loop is fully runnable with
no provider and no network.

Run:  uv run python -m scripts.simulate
"""

from __future__ import annotations

from app.config import Settings
from app.email.inbound import build_secret_address
from app.email.outbound import OutboundSender
from app.models import InboundEmail, Intent, NoraResult, ParsedRequest, ResultKind
from app.pipeline import Pipeline
from app.state.store import InMemoryStateStore

SECRET_LOCAL = "nora-abc123"
CEO = "ceo@example.com"
DOMAIN = "nora.ai"

GMAIL_WIYOW = (
    "WIYOW\n\n---------- Forwarded message ----------\n"
    "From: Sarah Lee <sarah@acme.com>\nDate: Mon, 26 May 2026 at 10:00\n"
    "Subject: Partnership proposal\nTo: CEO <ceo@example.com>\n\n"
    "Hi — we'd love to explore a partnership. Could you share a time to talk?\n"
)


class _OfflineDrafter:
    """Stand-in for the Claude service when no API key is present.

    Produces a plausible, deterministic draft/summary so the receipt and
    composition layers are exercised realistically. Clearly labelled so output
    is never mistaken for a real model draft.
    """

    def produce(self, req: ParsedRequest, intent: Intent) -> NoraResult:
        sender = req.thread.original_sender or "there"
        if intent is Intent.DRAFT:
            text = (
                f"Hi {sender.split('@')[0].title()},\n\n"
                "Thanks for reaching out — this sounds worth exploring. "
                "Could you propose a couple of times next week?\n\n"
                "Best\n\n[offline stub draft — set ANTHROPIC_API_KEY for a real Claude draft]"
            )
            return NoraResult(
                kind=ResultKind.DRAFT,
                text=text,
                interpretation_readback="Reply to the sender, open to the proposal.",
                recipient=req.thread.original_sender,
                recipient_uncertain=req.recipient_uncertain,
            )
        return NoraResult(
            kind=ResultKind.SUMMARY,
            text="Sarah Lee proposes a partnership and is asking for a time to talk. "
            "[offline stub summary — set ANTHROPIC_API_KEY for a real Claude summary]",
            interpretation_readback="Summarize the thread.",
        )


def _build_pipeline() -> tuple[Pipeline, list[dict]]:
    # ANTHROPIC_API_KEY and NORA_CLAUDE_MODEL load from .env / the environment
    # (anything not passed explicitly is read by pydantic-settings). The provider
    # fields and registry are forced to local dummies so the simulator is
    # self-contained and needs no provider config.
    settings = Settings(
        external_llm_ack=True,
        provider_api_key="local",
        webhook_basic_auth="local",
        postmark_sender="nora@local",
        ceo_registry={SECRET_LOCAL: CEO},
        address_domain=DOMAIN,
    )
    if settings.anthropic_api_key:
        import anthropic

        from app.drafting.claude import DraftingService

        drafting = DraftingService(
            anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=30.0),
            settings.claude_model,
        )
        print(">> Using REAL Claude drafting service\n")
    else:
        drafting = _OfflineDrafter()
        print(">> No ANTHROPIC_API_KEY — using OFFLINE stub drafter\n")

    sent: list[dict] = []

    class _CapturingProvider:
        def send(self, *, to: str, subject: str, body: str, reply_to: str | None) -> None:
            sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to})

    pipe = Pipeline(
        settings=settings,
        drafting=drafting,
        store=InMemoryStateStore(settings.state_ttl_seconds),
    )
    # OutboundSender isn't used by the pipeline directly, but we exercise it to
    # mirror the real flow (main._run_pipeline calls it after pipeline.handle).
    pipe._outbound_for_demo = OutboundSender(_CapturingProvider())  # type: ignore[attr-defined]
    return pipe, sent


def _inbound(body: str, *, secret_local: str = SECRET_LOCAL, ceo: str = CEO,
             token: str | None = None) -> InboundEmail:
    return InboundEmail(
        event_id="sim",
        secret_address_local=secret_local,
        from_address=ceo,
        dkim_verified=True,
        body=body,
        correlation_token=token,
    )


def _run(pipe: Pipeline, label: str, email: InboundEmail) -> str | None:
    reply, reply_to = pipe.handle(email)
    pipe._outbound_for_demo.send_to_ceo(email.from_address, reply, reply_to=reply_to)  # type: ignore[attr-defined]
    print(f"========== {label} ==========")
    print(f"SUBJECT: {reply.subject}")
    print(f"REPLY-TO: {reply_to or '(none)'}")
    print("BODY:")
    print(reply.body)
    print()
    return reply_to


def main() -> None:
    pipe, sent = _build_pipeline()

    _run(pipe, "1. WIYOW forward -> draft", _inbound(GMAIL_WIYOW))
    _run(pipe, "2. S forward -> summary", _inbound(GMAIL_WIYOW.replace("WIYOW", "S", 1)))
    _run(pipe, "3. Plain language -> draft",
         _inbound(GMAIL_WIYOW.replace("WIYOW", "reply, we're keen", 1)))
    _run(pipe, "4. Ambiguous -> clarify",
         _inbound(GMAIL_WIYOW.replace("WIYOW", "handle this", 1)))

    # Revise loop: reuse the token Nora handed back in scenario 1's reply-to.
    reply_to = _run(pipe, "5. New draft (to be revised)", _inbound(GMAIL_WIYOW))
    token = reply_to.split("+", 1)[1].split("@", 1)[0]
    _run(pipe, "6. Revise -> 'make it warmer'", _inbound("make it warmer", token=token))

    # P0 guard: a different CEO presenting another CEO's token must be rejected.
    print(f"   (reply-to issued to {build_secret_address(SECRET_LOCAL, token, DOMAIN)})")
    _run(pipe, "7. Spoof: different CEO + stolen token -> rejected",
         _inbound("make it warmer", secret_local="nora-other", ceo="other@x.com", token=token))

    print(f"\n>> Captured {len(sent)} outbound replies — all addressed to the CEO:")
    for s in sent:
        print(f"   to={s['to']}  subject={s['subject']!r}")


if __name__ == "__main__":
    main()
